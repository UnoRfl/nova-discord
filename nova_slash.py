"""The /promptnova command, and the approval that stands in front of it.

Anyone in the "ask" lane does not get their prompt run. It goes to Uno as a
direct message with two buttons, and nothing happens until he presses one. He
sees the exact text that would be sent, who sent it, and where -- so the thing
he is approving is the thing that runs.

Three things about slash commands that are easy to get wrong and fatal to miss:

  * Discord gives an interaction three seconds. Claude takes five to a hundred
    and fifty. Every path here defers immediately or the command dies with
    "application did not respond".
  * The bot must have been invited with the applications.commands scope, or the
    commands register silently and never appear. /novasetup reports this.
  * A guild sync appears instantly; a global sync can take an hour. This syncs
    per guild.
"""

import logging
from pathlib import Path

import discord
from discord import app_commands

import nova_agent
import nova_server

log = logging.getLogger("nova.slash")

BUSY = set()          # user ids with a job in flight -- one each, no queue jumping


async def deliver(target, text: str, files: list[Path], header: str = ""):
    """Post the answer and whatever it built, inside Discord's limits."""
    import nova_claude
    attachments = [discord.File(str(f), filename=f.name) for f in files]
    pieces = nova_claude.chunk(header + text if header else text)
    for i, piece in enumerate(pieces):
        last = i == len(pieces) - 1
        await target.send(piece, files=attachments if last else None)


async def execute(interaction_user, channel, prompt: str, jailed: bool, note: str = "",
                  is_owner: bool = False):
    """Run one job and put the result in `channel`. Never raises into the gateway."""
    job = nova_agent.new_job(interaction_user.display_name)
    try:
        # Identity comes from the bot, never from the message. A prompt that
        # claims to be Uno is just a prompt claiming to be Uno.
        who = nova_agent.identity(interaction_user.display_name, is_owner)
        framed = f"[Requested on Discord by {interaction_user.display_name}]\n\n{prompt}"
        import nova_claude
        text = await nova_agent.run(framed, job, jailed, who,
                                    nova_claude.model_for(is_owner))
        files = nova_agent.harvest(job)

        # Only Uno's own runs may speak into other channels. A jailed run is
        # never told the mechanism exists, and its actions file is ignored
        # even if it writes one anyway.
        if is_owner:
            actions = nova_agent.read_actions(job)
            if actions:
                results = await perform(channel.guild, actions)
                if results:
                    note = note + "*" + "; ".join(results) + "*\n"

            # Server changes: the safe ones happen now, the irreversible ones
            # are put in front of him with a button and a plain description.
            safe, risky = nova_agent.read_server_actions(job)
            for a in safe:
                done = await nova_server.run_action(channel.guild, a)
                note = note + "*" + done + "*\n"
            if risky:
                view = nova_server.Confirm(channel.guild, risky, nova_agent.describe)
                await channel.send(view.summary(), view=view)

        head = note
        if files:
            head += f"**{prompt[:120]}**\n"
        await deliver(channel, text, files, head)
        log.info("job for %s finished, %d file(s)", interaction_user, len(files))
    except Exception as exc:                       # a failed job must not kill the bot
        log.exception("job failed")
        try:
            await channel.send(f"That job fell over: `{exc}`")
        except discord.DiscordException:
            pass


def find_channel(guild, wanted: str):
    """Resolve 'general', '#general', an id, or a name buried in emoji."""
    if guild is None:
        return None
    if wanted.isdigit():
        return guild.get_channel(int(wanted))
    want = wanted.lower().strip()
    for ch in guild.text_channels:
        if ch.name.lower() == want:
            return ch
    # Channel names here look like "📣・announcements"; match the readable part.
    for ch in guild.text_channels:
        if want in ch.name.lower():
            return ch
    return None


async def perform(guild, actions: list[dict]) -> list[str]:
    """Carry out what the run asked to send. Owner runs only -- the caller
    decides that; by the time anything reaches here it has been decided."""
    done = []
    for act in actions:
        channel = find_channel(guild, act["channel"])
        if channel is None:
            done.append(f"no channel matching `{act['channel']}`")
            continue
        perms = channel.permissions_for(guild.me)
        if not perms.send_messages:
            done.append(f"cannot post in #{channel.name}")
            continue

        embed = None
        if act["embed"]:
            e = act["embed"]
            embed = discord.Embed(title=str(e.get("title") or "")[:256] or None,
                                  description=str(e.get("description") or "")[:4000] or None,
                                  colour=int(e.get("color") or 0x8b46e8))
        files = [discord.File(str(f), filename=f.name) for f in act["files"]]
        try:
            await channel.send(content=act["content"] or None, embed=embed,
                               files=files or None)
            bits = []
            if act["content"]: bits.append("message")
            if files: bits.append(f"{len(files)} file(s)")
            if embed: bits.append("embed")
            done.append(f"sent {' + '.join(bits)} to #{channel.name}")
        except discord.DiscordException as exc:
            done.append(f"#{channel.name} refused it: {exc}")
    return done


class Approval(discord.ui.View):
    """Two buttons in Uno's DMs. Nothing runs until one is pressed."""

    def __init__(self, requester, channel, prompt: str):
        super().__init__(timeout=3600)
        self.requester, self.channel, self.prompt = requester, channel, prompt
        self.done = False

    async def _close(self, interaction, verdict: str):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"{verdict}\n\n>>> {self.prompt}", view=self)

    @discord.ui.button(label="Approve", style=discord.ButtonStyle.success, emoji="✅")
    async def approve(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._close(interaction, f"✅ Approved — running for **{self.requester.display_name}**")
        nova_agent.pending_clear(self.requester.id)
        nova_agent.quota_spend(self.requester.id)     # a denial costs them nothing
        # Approved requests are still jailed. Approval says "this prompt is
        # fine", not "this person may run commands on my PC".
        await execute(self.requester, self.channel, self.prompt, jailed=True,
                      note=f"{self.requester.mention} — approved.\n")

    @discord.ui.button(label="Deny", style=discord.ButtonStyle.danger, emoji="✖")
    async def deny(self, interaction: discord.Interaction, _button: discord.ui.Button):
        await self._close(interaction, f"✖ Denied — **{self.requester.display_name}**")
        nova_agent.pending_clear(self.requester.id)
        try:
            await self.channel.send(f"{self.requester.mention} — that one was declined.")
        except discord.DiscordException:
            pass

    async def on_timeout(self):
        nova_agent.pending_clear(self.requester.id)
        if not self.done:
            try:
                await self.channel.send(
                    f"{self.requester.mention} — that request expired without an answer.")
            except discord.DiscordException:
                pass


class ChatApproval(discord.ui.View):
    """First time someone speaks to her, Uno decides -- once, and it sticks.

    Three answers rather than two: yes forever, yes this once, and never ask
    again. Without the third, a persistent stranger means a permanent stream
    of DMs, which is the same spam problem wearing a different hat.
    """

    def __init__(self, author, message, prompt: str):
        super().__init__(timeout=86400)
        self.author, self.message, self.prompt = author, message, prompt
        self.done = False

    async def _close(self, interaction, verdict: str):
        self.done = True
        for child in self.children:
            child.disabled = True
        await interaction.response.edit_message(
            content=f"{verdict}\n\n>>> {self.prompt[:900]}", view=self)

    async def _answer(self):
        import nova_claude
        who = f"[Discord message from {self.author.display_name}]\n{self.prompt}"
        async with self.message.channel.typing():
            answer = await nova_claude.ask(who, f"ch:{self.message.channel.id}", False,
                                           nova_claude.GUEST_MODEL)
        for i, piece in enumerate(nova_claude.chunk(answer)):
            if i == 0:
                await self.message.reply(piece, mention_author=False)
            else:
                await self.message.channel.send(piece)

    @discord.ui.button(label="Allow", style=discord.ButtonStyle.success, emoji="✅")
    async def allow(self, interaction: discord.Interaction, _b):
        nova_agent.chat_set(self.author.id, "allowed")
        await self._close(interaction, f"✅ **{self.author.display_name}** may talk to her from now on")
        await self._answer()

    @discord.ui.button(label="Just this once", style=discord.ButtonStyle.secondary, emoji="➤")
    async def once(self, interaction: discord.Interaction, _b):
        await self._close(interaction, f"➤ Answering **{self.author.display_name}** this once")
        await self._answer()

    @discord.ui.button(label="Never", style=discord.ButtonStyle.danger, emoji="⛔")
    async def never(self, interaction: discord.Interaction, _b):
        nova_agent.chat_set(self.author.id, "blocked")
        await self._close(interaction, f"⛔ She will not answer **{self.author.display_name}** again")


async def ask_to_chat(bot, message, prompt: str) -> bool:
    """Put a first-contact request in front of Uno. True if he was asked."""
    if not nova_agent.OWNER_ID:
        return False
    try:
        owner = await bot.fetch_user(int(nova_agent.OWNER_ID))
    except (discord.DiscordException, ValueError):
        return False
    where = getattr(message.channel, "mention", "a channel")
    view = ChatApproval(message.author, message, prompt)
    try:
        await owner.send(
            f"**{message.author.display_name}** spoke to Nova in {where}:\n"
            f">>> {prompt[:900]}", view=view)
        return True
    except discord.Forbidden:
        return False


def setup(bot):
    @bot.tree.command(name="promptnova", description="Ask Nova to build something and send it back")
    @app_commands.describe(prompt="What you want made, in plain words")
    async def promptnova(interaction: discord.Interaction, prompt: str):
        # Three seconds. Defer before anything else can be slow.
        await interaction.response.defer(thinking=True)

        user = interaction.user
        roles = {str(r.id) for r in getattr(user, "roles", [])}
        tier = nova_agent.tier_for(user.id, roles)

        if tier == "blocked":
            await interaction.followup.send(
                "You do not have access to this. Ask Uno for the role.", ephemeral=True)
            return

        ok, why = nova_agent.quota_check(user.id)
        if not ok:
            await interaction.followup.send(why, ephemeral=True)
            return

        if user.id in BUSY:
            await interaction.followup.send(
                "You already have one running. Let it finish.", ephemeral=True)
            return

        if tier == "ask":
            owner = None
            if nova_agent.OWNER_ID:
                try:
                    owner = await bot.fetch_user(int(nova_agent.OWNER_ID))
                except (discord.DiscordException, ValueError):
                    owner = None
            if owner is None:
                await interaction.followup.send(
                    "This needs approval, but I cannot reach Uno to ask.", ephemeral=True)
                return
            where = getattr(interaction.channel, "mention", "a channel")
            view = Approval(user, interaction.channel, prompt)
            try:
                await owner.send(
                    f"**{user.display_name}** asked in {where}:\n>>> {prompt}", view=view)
            except discord.Forbidden:
                await interaction.followup.send(
                    "This needs approval and Uno's DMs are closed.", ephemeral=True)
                return
            # The cooldown starts when they ASK, not when it is approved, so a
            # stream of denied requests cannot be used to spam his DMs either.
            nova_agent.pending_add(user.id)
            nova_agent.quota_touch(user.id)
            await interaction.followup.send("Sent to Uno for approval. I will post it here.")
            return

        # owner and trusted run straight away; only the owner runs unjailed
        BUSY.add(user.id)
        try:
            await interaction.followup.send("Working on it.")
            nova_agent.quota_spend(user.id)
            await execute(user, interaction.channel, prompt,
                          jailed=(tier != "owner"), is_owner=(tier == "owner"))
        finally:
            BUSY.discard(user.id)

    @bot.tree.command(name="novahub", description="Make a voice channel hand out private rooms")
    @app_commands.describe(channel="The voice channel to use as the hub")
    async def novahub(interaction: discord.Interaction, channel: discord.VoiceChannel):
        if str(interaction.user.id) != nova_agent.OWNER_ID:
            await interaction.response.send_message("Not yours to set.", ephemeral=True)
            return
        import bot as botmod
        if channel.id in botmod.HUBS:
            botmod.HUBS.discard(channel.id)
            await interaction.response.send_message(
                f"**{channel.name}** is an ordinary voice channel again.", ephemeral=True)
            return
        botmod.HUBS.add(channel.id)
        await interaction.response.send_message(
            f"**{channel.name}** now gives everyone who joins their own room. "
            f"Run this again to turn it off.", ephemeral=True)

    @bot.tree.command(name="novachat", description="Decide who Nova may talk to")
    @app_commands.describe(user="Who", action="allow, block, or forget (ask me again)")
    @app_commands.choices(action=[
        app_commands.Choice(name="allow", value="allowed"),
        app_commands.Choice(name="block", value="blocked"),
        app_commands.Choice(name="forget", value="forget"),
    ])
    async def novachat(interaction: discord.Interaction, user: discord.User,
                       action: app_commands.Choice[str]):
        if str(interaction.user.id) != nova_agent.OWNER_ID:
            await interaction.response.send_message("Not yours to set.", ephemeral=True)
            return
        nova_agent.chat_set(user.id, action.value)
        allowed, blocked = nova_agent.chat_roster()
        await interaction.response.send_message(
            f"**{user.display_name}** -> `{action.value}`  "
            f"(allowed: {len(allowed)}, blocked: {len(blocked)})", ephemeral=True)

    @bot.tree.command(name="novasetup", description="Show who can use /promptnova and how")
    async def novasetup(interaction: discord.Interaction):
        roles = {str(r.id) for r in getattr(interaction.user, "roles", [])}
        lines = [
            f"**your tier:** `{nova_agent.tier_for(interaction.user.id, roles)}`",
            f"owner set: `{bool(nova_agent.OWNER_ID)}`",
            f"trusted roles: `{len(nova_agent.TRUSTED_ROLES)}`  "
            f"approval roles: `{len(nova_agent.ASK_ROLES)}`",
            f"claude found: `{nova_claude_found()}`",
            f"your model: `{_model_label(interaction.user.id)}`",
            f"your limits: `{nova_agent.DAILY_CAP}/day`, "
            f"`{nova_agent.COOLDOWN}s` between asks (Uno is exempt)",
            "",
            "owner runs unjailed. everyone else is confined to a fresh empty "
            "folder with Bash disabled: it can research and write files, it "
            "cannot run commands or see anything of Uno's.",
        ]
        await interaction.response.send_message("\n".join(lines), ephemeral=True)


def _model_label(user_id) -> str:
    import nova_claude
    return nova_claude.model_for(str(user_id) == nova_agent.OWNER_ID)


def nova_claude_found() -> bool:
    import nova_claude
    return nova_claude.CLAUDE is not None
