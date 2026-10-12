"""Targeted message purges: by person, from a point, between two points, by text.

Discord's own channel.purge only knows "the last N". Uno asks for things like
"remove everything from this message down" or "all of his messages in here",
so this scans history with a filter and deletes what matches -- bulk-deleting
anything under 14 days old, one by one for older messages (a Discord rule).

Used two ways:
  - by Nova, from a purge action she writes (chat or /promptnova), shown to
    Uno with a confirm button first;
  - directly, by the owner-only /purge command and the right-click message
    menus, which cost no tokens.
"""

import datetime as dt
import logging
import os
import re

import discord
from discord import app_commands

log = logging.getLogger("nova.purge")

OWNER_ID = os.getenv("NOVA_OWNER_ID", "").strip()
SCAN_LIMIT = 2000          # messages looked at per purge
MAX_DELETE = 1000          # messages deleted per purge
FOURTEEN_DAYS = dt.timedelta(days=14)

_LINK = re.compile(r"discord(?:app)?\.com/channels/(\d+|@me)/(\d+)/(\d+)")


def msg_ref(value) -> tuple[int | None, int | None]:
    """(channel_id, message_id) from a message link or a bare id."""
    if value in (None, ""):
        return None, None
    s = str(value).strip()
    m = _LINK.search(s)
    if m:
        return int(m.group(2)), int(m.group(3))
    return (None, int(s)) if s.isdigit() else (None, None)


def _when(value) -> dt.datetime | None:
    if not value:
        return None
    try:
        t = dt.datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    except ValueError:
        return None
    return t if t.tzinfo else t.astimezone()   # naive = PC local time


def resolve_users(guild, names, bot_user) -> set[int]:
    """IDs from ids, mentions, "me" (the owner), "nova" (the bot) or names."""
    out: set[int] = set()
    for raw in names or []:
        s = str(raw).strip().lower().strip("<@!>")
        if s.isdigit():
            out.add(int(s))
        elif s in ("me", "owner", "uno") and OWNER_ID:
            out.add(int(OWNER_ID))
        elif s in ("nova", "you", "bot") and bot_user:
            out.add(bot_user.id)
        elif guild:
            for m in guild.members:
                if s in (m.name.lower(), m.display_name.lower(), (m.global_name or "").lower()):
                    out.add(m.id)
    return out


def normalise(spec: dict, here_id: int | None) -> dict:
    """Clean a purge spec from a model or a command into one shape."""
    ch_id = None
    ch = str(spec.get("channel") or "").strip().lstrip("#")
    if ch.isdigit():
        ch_id = int(ch)
    elif ch.lower() in ("", "here", "this", "current") and here_id:
        ch_id = here_id
    after_ch, after = msg_ref(spec.get("after") or spec.get("from"))
    before_ch, before = msg_ref(spec.get("before") or spec.get("to"))
    users = spec.get("users") or spec.get("user") or []
    if isinstance(users, (str, int)):
        users = [users]
    try:
        count = int(spec.get("count") or MAX_DELETE)
    except (TypeError, ValueError):
        count = MAX_DELETE
    return {
        "group": "purge",
        "channel": ch if not ch_id else "",
        "channel_id": ch_id or after_ch or before_ch,
        "count": max(1, min(MAX_DELETE, count)),
        "users": [str(u) for u in users][:10],
        "after": after,
        "before": before,
        # "from this message" means that message too, unless told otherwise
        "inclusive": spec.get("inclusive", True) is not False,
        "since": spec.get("since"),
        "until": spec.get("until"),
        "contains": str(spec.get("contains") or "")[:100],
    }


def describe(spec: dict, guild=None) -> str:
    where = f"<#{spec['channel_id']}>" if spec.get("channel_id") else f"#{spec.get('channel')}"
    bits = [f"delete up to {spec['count']} messages in {where}"]
    if spec.get("users"):
        names = []
        for u in spec["users"]:
            m = guild.get_member(int(u)) if (guild and str(u).isdigit()) else None
            label = {"me": "Uno", "owner": "Uno", "uno": "Uno",
                     "nova": "Nova", "you": "Nova", "bot": "Nova"}.get(str(u).lower(), str(u))
            names.append(m.display_name if m else label)
        bits.append("from " + ", ".join(names))
    if spec.get("after"):
        bits.append(f"from message {spec['after']}" + (" (including it)" if spec.get("inclusive") else "") + " onward")
    if spec.get("before"):
        bits.append(f"up to message {spec['before']}" + (" (including it)" if spec.get("inclusive") else ""))
    if spec.get("since"):
        bits.append(f"since {spec['since']}")
    if spec.get("until"):
        bits.append(f"until {spec['until']}")
    if spec.get("contains"):
        bits.append(f"containing “{spec['contains']}”")
    return ", ".join(bits)


def channel_for(guild, spec):
    if spec.get("channel_id"):
        return guild.get_channel_or_thread(int(spec["channel_id"]))
    import nova_server
    return nova_server.find_channel(guild, spec.get("channel") or "")


async def collect(ch, spec, bot_user) -> list[discord.Message]:
    users = resolve_users(ch.guild, spec.get("users"), bot_user)
    if spec.get("users") and not users:
        return []                       # named someone we cannot find: delete nothing
    since, until = _when(spec.get("since")), _when(spec.get("until"))
    after = before = None
    if spec.get("after"):
        after = discord.Object(id=spec["after"] - (1 if spec.get("inclusive") else 0))
    elif since:
        after = since
    if spec.get("before"):
        before = discord.Object(id=spec["before"] + (1 if spec.get("inclusive") else 0))
    elif until:
        before = until
    needle = spec.get("contains", "").lower()

    found = []
    async for m in ch.history(limit=SCAN_LIMIT, after=after, before=before, oldest_first=False):
        if m.pinned:
            continue
        if users and m.author.id not in users:
            continue
        if needle and needle not in (m.content or "").lower():
            continue
        found.append(m)
        if len(found) >= spec["count"]:
            break
    return found


async def delete(ch, messages: list[discord.Message]) -> int:
    now = discord.utils.utcnow()
    young = [m for m in messages if now - m.created_at < FOURTEEN_DAYS - dt.timedelta(minutes=5)]
    old = [m for m in messages if m not in young]
    gone = 0
    for i in range(0, len(young), 100):
        batch = young[i:i + 100]
        try:
            if len(batch) == 1:
                await batch[0].delete()
            else:
                await ch.delete_messages(batch)
            gone += len(batch)
        except discord.NotFound:
            pass
    for m in old:                       # Discord will not bulk-delete these
        try:
            await m.delete()
            gone += 1
        except discord.NotFound:
            pass
    return gone


async def run(guild, spec: dict, bot_user) -> str:
    ch = channel_for(guild, spec)
    if ch is None:
        return f"no channel `{spec.get('channel') or spec.get('channel_id')}`"
    try:
        gone = await delete(ch, await collect(ch, spec, bot_user))
    except discord.Forbidden:
        return f"I lack Manage Messages in #{ch.name}"
    log.info("purge in #%s: %d deleted (%s)", ch.name, gone, describe(spec, guild))
    return f"deleted {gone} message(s) in #{ch.name}"


def is_owner(user) -> bool:
    return bool(OWNER_ID) and str(user.id) == OWNER_ID


class ConfirmPurge(discord.ui.View):
    """For the zero-token menus: show the count, delete on one press."""

    def __init__(self, guild, spec, messages, bot_user):
        super().__init__(timeout=300)
        self.guild, self.spec, self.messages, self.bot_user = guild, spec, messages, bot_user

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        return is_owner(interaction.user)

    @discord.ui.button(label="Delete them", style=discord.ButtonStyle.danger, emoji="🗑")
    async def go(self, interaction: discord.Interaction, _b):
        for c in self.children:
            c.disabled = True
        await interaction.response.edit_message(content="Deleting…", view=self)
        ch = channel_for(self.guild, self.spec)
        gone = await delete(ch, self.messages)
        await interaction.edit_original_response(content=f"Deleted {gone} message(s).", view=None)

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, _b):
        await interaction.response.edit_message(content="Cancelled — nothing deleted.", view=None)


async def _preview(interaction, spec):
    if not is_owner(interaction.user):
        await interaction.response.send_message("That one is not for you.", ephemeral=True)
        return
    if interaction.guild is None:
        await interaction.response.send_message("Purges work in server channels only.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    ch = channel_for(interaction.guild, spec)
    msgs = await collect(ch, spec, interaction.client.user)
    if not msgs:
        await interaction.followup.send("Nothing matches — nothing to delete.", ephemeral=True)
        return
    view = ConfirmPurge(interaction.guild, spec, msgs, interaction.client.user)
    await interaction.followup.send(
        f"**{len(msgs)}** message(s) match: {describe(spec, interaction.guild)}.", view=view,
        ephemeral=True)


def setup(bot):
    @bot.tree.command(name="purge", description="Owner only: delete messages here by person, point, or text")
    @app_commands.describe(
        user="Only this person's messages",
        after="Message link or ID: delete from this message on (it included)",
        before="Message link or ID: delete up to this message (it included)",
        count="Most messages to delete (default 100)",
        contains="Only messages containing this text")
    async def purge_cmd(interaction: discord.Interaction, user: discord.Member | None = None,
                        after: str = "", before: str = "", count: int = 100, contains: str = ""):
        spec = normalise({"channel": "here", "users": [user.id] if user else [],
                          "after": after, "before": before, "count": count,
                          "contains": contains}, interaction.channel_id)
        await _preview(interaction, spec)

    async def from_here(interaction: discord.Interaction, message: discord.Message):
        spec = normalise({"channel": "here", "after": message.id}, interaction.channel_id)
        await _preview(interaction, spec)

    async def theirs(interaction: discord.Interaction, message: discord.Message):
        spec = normalise({"channel": "here", "users": [message.author.id]}, interaction.channel_id)
        await _preview(interaction, spec)

    bot.tree.add_command(app_commands.ContextMenu(name="Purge from here down", callback=from_here))
    bot.tree.add_command(app_commands.ContextMenu(name="Purge their messages here", callback=theirs))
