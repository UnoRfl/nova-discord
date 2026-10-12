"""Carrying out server changes, and refusing to do the irreversible ones quietly.

Everything here is split in two. Creating a channel, making a role, handing
someone a colour: done on the spot, because a mistake costs one click to undo.
Deleting a channel, deleting a role, purging messages: proposed only, shown to
Uno as a plain sentence with a button, and performed when he presses it.

Discord has no undo. "Tidy up general" is not a clear enough instruction to
delete anything on, and a model that is 95% sure what he meant is not a good
enough reason to destroy a year of conversation.
"""

import logging

import discord

log = logging.getLogger("nova.server")

# Only permissions worth handing out from a chat message. Administrator is not
# on the list and never will be: it is every other permission at once, granted
# by a sentence someone typed.
SAFE_PERMS = {
    "send_messages", "read_messages", "read_message_history", "attach_files",
    "embed_links", "add_reactions", "use_external_emojis", "connect", "speak",
    "stream", "change_nickname", "create_public_threads", "send_messages_in_threads",
    "manage_messages", "move_members", "mute_members", "priority_speaker",
}


def find_channel(guild, wanted: str):
    if guild is None or not wanted:
        return None
    w = str(wanted).lstrip("#").lower().strip()
    if w.isdigit():
        return guild.get_channel(int(w))
    for ch in guild.channels:
        if ch.name.lower() == w:
            return ch
    for ch in guild.channels:          # names here carry emoji: "📣・announcements"
        if w in ch.name.lower():
            return ch
    return None


def find_role(guild, wanted: str):
    if guild is None or not wanted:
        return None
    w = str(wanted).lstrip("@").lower().strip()
    if w.isdigit():
        return guild.get_role(int(w))
    return next((r for r in guild.roles if r.name.lower() == w), None)


def perms_from(names) -> discord.Permissions:
    p = discord.Permissions.none()
    for name in names or []:
        key = str(name).lower().strip()
        if key in SAFE_PERMS and hasattr(p, key):
            setattr(p, key, True)
        else:
            log.info("ignoring permission %r (not on the safe list)", key)
    return p


def colour_from(value) -> discord.Colour:
    try:
        return discord.Colour(int(str(value).lstrip("#"), 16))
    except (TypeError, ValueError):
        return discord.Colour.default()


async def run_action(guild, act: dict) -> str:
    """Perform one action and report it in a sentence. Never raises."""
    group, do = act["group"], act.get("do", "")
    try:
        if group == "purge":
            import nova_purge
            return await nova_purge.run(guild, act, guild.me)

        if group == "channels":
            if do == "create":
                name = str(act.get("name") or "").strip()
                if not name:
                    return "a channel needs a name"
                category = None
                if act.get("category"):
                    category = discord.utils.get(guild.categories,
                                                 name=str(act["category"]))
                voice = str(act.get("kind") or act.get("type") or "text").lower() == "voice"
                if voice:
                    ch = await guild.create_voice_channel(name, category=category)
                    if act.get("jointocreate"):
                        return (f"created voice channel **{ch.name}** — set it as the "
                                f"join-to-create hub with `/novahub channel:{ch.name}`")
                    return f"created voice channel **{ch.name}**"
                ch = await guild.create_text_channel(
                    name, category=category, topic=str(act.get("topic") or "") or None)
                return f"created {ch.mention}"

            ch = find_channel(guild, act.get("channel"))
            if not ch:
                return f"no channel `{act.get('channel')}`"
            if do == "rename":
                old = ch.name
                await ch.edit(name=str(act.get("name")))
                return f"renamed #{old} to #{ch.name}"
            if do == "topic":
                await ch.edit(topic=str(act.get("topic") or ""))
                return f"set the topic of #{ch.name}"
            if do == "delete":
                name = ch.name
                await ch.delete()
                return f"deleted #{name}"

        if group == "roles":
            if do == "create":
                role = await guild.create_role(
                    name=str(act.get("name") or "new role"),
                    colour=colour_from(act.get("color")),
                    permissions=perms_from(act.get("permissions")),
                    hoist=bool(act.get("hoist")),
                    mentionable=bool(act.get("mentionable")))
                return f"created the role **{role.name}**"

            role = find_role(guild, act.get("role") or act.get("name"))
            if not role:
                return f"no role `{act.get('role') or act.get('name')}`"
            if do == "delete":
                name = role.name
                await role.delete()
                return f"deleted the role **{name}**"
            if do == "edit":
                kwargs = {}
                if act.get("color"):       kwargs["colour"] = colour_from(act["color"])
                if act.get("permissions"): kwargs["permissions"] = perms_from(act["permissions"])
                if act.get("name"):        kwargs["name"] = str(act["name"])
                if "hoist" in act:         kwargs["hoist"] = bool(act["hoist"])
                await role.edit(**kwargs)
                return f"edited the role **{role.name}**"
            if do in ("give", "take"):
                raw = str(act.get("user") or "").strip("<@!>")
                member = guild.get_member(int(raw)) if raw.isdigit() else \
                    discord.utils.find(lambda m: m.display_name.lower() == raw.lower(),
                                       guild.members)
                if not member:
                    return f"no member `{act.get('user')}`"
                if do == "give":
                    await member.add_roles(role)
                    return f"gave **{role.name}** to {member.display_name}"
                await member.remove_roles(role)
                return f"took **{role.name}** from {member.display_name}"

        return f"did not understand `{group}: {do}`"
    except discord.Forbidden:
        return f"Discord refused that ({group}: {do}) — I lack the permission"
    except discord.DiscordException as exc:
        return f"{group}: {do} failed — {exc}"


class Confirm(discord.ui.View):
    """The irreversible ones, shown plainly, waiting on a button."""

    def __init__(self, guild, actions: list[dict], describe):
        super().__init__(timeout=600)
        self.guild, self.actions, self.describe = guild, actions, describe
        self.done = False

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        """Only Uno presses these. Anyone in the channel can see the buttons."""
        import nova_purge
        if nova_purge.is_owner(interaction.user):
            return True
        await interaction.response.send_message("Only Uno can approve this.", ephemeral=True)
        return False

    def summary(self) -> str:
        import nova_purge
        lines = "\n".join(
            f"• {nova_purge.describe(a, self.guild) if a['group'] == 'purge' else self.describe(a)}"
            for a in self.actions)
        return f"**This cannot be undone.** Nova proposes:\n{lines}"

    async def _close(self, interaction, verdict):
        self.done = True
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content=verdict, view=self)

    @discord.ui.button(label="Do it", style=discord.ButtonStyle.danger, emoji="⚠")
    async def go(self, interaction: discord.Interaction, _b):
        await self._close(interaction, "⚠ Running…")
        results = [await run_action(self.guild, a) for a in self.actions]
        await interaction.followup.send("\n".join(f"• {r}" for r in results),
                                        ephemeral=True)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, emoji="✖")
    async def stop_it(self, interaction: discord.Interaction, _b):
        await self._close(interaction, "✖ Cancelled — nothing was changed.")
