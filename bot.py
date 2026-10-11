"""Nova — a persistent Discord bot.

Unlike the one-off REST calls that came before, this holds an open gateway
connection, so the bot stays green in the member list until you stop it.
"""

import asyncio
import json
import logging
import os
import re
import sys
from pathlib import Path

import discord
from discord.ext import commands
from dotenv import load_dotenv

load_dotenv()

TOKEN = os.getenv("DISCORD_TOKEN")
if not TOKEN:
    sys.exit(
        "No DISCORD_TOKEN found.\n"
        "Open the .env file in this folder and paste your token after "
        "DISCORD_TOKEN=  then run this again."
    )

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[
        logging.FileHandler("nova-bot.log", encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("nova")

# Imported after logging is configured so its own logger inherits the handlers.
import nova_claude  # noqa: E402
import nova_agent   # noqa: E402
import nova_slash   # noqa: E402
import nova_pc      # noqa: E402

intents = discord.Intents.default()
intents.message_content = True  # needs "Message Content Intent" on in the portal
intents.members = True          # needs "Server Members Intent" on in the portal

bot = commands.Bot(command_prefix="!", intents=intents)

SLASH_READY = False


@bot.event
async def on_ready():
    log.info("Connected as %s (id %s)", bot.user, bot.user.id)
    for guild in bot.guilds:
        log.info("  in guild: %s (%s members)", guild.name, guild.member_count)
    log.info("claude exe: %s", nova_claude.CLAUDE or "NOT FOUND — chat replies disabled")
    log.info("join-to-create hubs: %s",
             ", ".join(getattr(bot.get_channel(h), "name", str(h)) for h in HUBS)
             or "none set (use /novahub)")
    await sweep_rooms()
    await bot.change_presence(
        activity=discord.Game(name="watching over Ulupongs")
    )

    # Slash commands are registered per guild: a guild sync shows up instantly,
    # a global one can take an hour. If nothing appears in Discord the bot was
    # invited without the applications.commands scope -- re-invite it with that
    # ticked, nothing here can work around it.
    # on_ready fires again on every reconnect, and registering the same command
    # twice raises. Once per process is the correct number.
    global SLASH_READY
    if not SLASH_READY:
        try:
            nova_slash.setup(bot)
            nova_pc.setup(bot)
            SLASH_READY = True
        except Exception:
            log.exception("could not register slash commands")
    for guild in bot.guilds:
        try:
            # tree.command() registers GLOBALLY, and a guild sync only pushes
            # what is assigned to that guild -- so a plain guild sync uploads
            # nothing and reports zero. Copy the globals across first.
            scope = discord.Object(id=guild.id)
            bot.tree.copy_global_to(guild=scope)
            synced = await bot.tree.sync(guild=scope)
            log.info("  synced %d slash command(s) to %s", len(synced), guild.name)
        except discord.DiscordException as exc:
            log.warning("  slash sync failed for %s: %s", guild.name, exc)

    nova_claude.NOTIFY = notify_owner
    asyncio.create_task(nova_pc.refresh(force=True))

    swept = nova_agent.sweep()
    if swept:
        log.info("cleared %d old job folder(s)", swept)


async def notify_owner(text: str):
    """DM Uno directly -- for failures he would otherwise only see in the log."""
    if not nova_claude.OWNER_ID:
        return
    user = bot.get_user(int(nova_claude.OWNER_ID)) or await bot.fetch_user(int(nova_claude.OWNER_ID))
    await user.send(f"Sir, something needs your attention: {text}")


# --- join to create -------------------------------------------------------
# A hub channel that hands each arrival their own room. This cannot live in the
# actions file: it is not a change to make once, it is a rule that has to be
# there every time somebody joins.
HUBS_FILE = Path.home() / ".nova" / "discord-hubs.json"
SPAWNED_FILE = Path.home() / ".nova" / "discord-rooms.json"


def _load_hubs() -> set:
    """Which channels are hubs. On disk, because an in-memory set forgets the
    whole arrangement every time the bot restarts -- and the bot restarts."""
    try:
        return set(json.loads(HUBS_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def _save_hubs() -> None:
    try:
        HUBS_FILE.parent.mkdir(parents=True, exist_ok=True)
        HUBS_FILE.write_text(json.dumps(sorted(HUBS)), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save hubs: %s", exc)


def _load_rooms() -> set:
    try:
        return set(json.loads(SPAWNED_FILE.read_text(encoding="utf-8")))
    except (OSError, json.JSONDecodeError, TypeError):
        return set()


def _save_rooms() -> None:
    try:
        SPAWNED_FILE.parent.mkdir(parents=True, exist_ok=True)
        SPAWNED_FILE.write_text(json.dumps(sorted(SPAWNED)), encoding="utf-8")
    except OSError as exc:
        log.warning("could not save rooms: %s", exc)


HUBS: set = _load_hubs()
# Which rooms are ours to delete. Also on disk: forgetting this list does not
# lose a room, it strands one -- an empty channel nobody owns, left behind by
# every restart that happened while somebody was in a call.
SPAWNED: set[int] = _load_rooms()


async def sweep_rooms():
    """Bin the rooms that emptied while we were down."""
    for rid in sorted(SPAWNED):
        ch = bot.get_channel(rid)
        if ch is None:                      # already gone
            SPAWNED.discard(rid)
        elif not ch.members:
            SPAWNED.discard(rid)
            try:
                await ch.delete()
                log.info("swept the empty room %s", ch.name)
            except discord.DiscordException as exc:
                log.warning("could not sweep %s: %s", ch.name, exc)
    _save_rooms()


@bot.event
async def on_voice_state_update(member, before, after):
    try:
        if after.channel and after.channel.id in HUBS:
            # A hub sitting outside any category has category None, and asking
            # None to make a channel is how this used to fall over.
            where = after.channel.category or member.guild
            room = await where.create_voice_channel(
                f"{member.display_name}'s VC",
                bitrate=after.channel.bitrate,
                user_limit=after.channel.user_limit)
            SPAWNED.add(room.id)
            _save_rooms()
            await member.move_to(room)
            log.info("made a room for %s", member.display_name)

        # Last one out turns off the lights.
        if before.channel and before.channel.id in SPAWNED and not before.channel.members:
            SPAWNED.discard(before.channel.id)
            _save_rooms()
            await before.channel.delete()
    except discord.DiscordException as exc:
        log.warning("join-to-create: %s", exc)


@bot.event
async def on_disconnect():
    # discord.py reconnects on its own; this just makes gaps visible in the log.
    log.warning("Gateway dropped — reconnecting")


@bot.command(name="ping")
async def ping(ctx: commands.Context):
    """Confirms the bot is alive and shows gateway latency."""
    await ctx.send(f"Awake. {round(bot.latency * 1000)}ms")


@bot.command(name="whereami")
async def whereami(ctx: commands.Context):
    """Reports which server and channel the command came from."""
    await ctx.send(f"#{ctx.channel.name} in {ctx.guild.name}")


@bot.command(name="announce")
@commands.has_permissions(administrator=True)
async def announce(ctx: commands.Context, *, message: str):
    """Send an announcement to the announcements channel."""
    guild = ctx.guild
    announce_channel = None
    for channel in guild.text_channels:
        if "announce" in channel.name.lower():
            announce_channel = channel
            break
    if not announce_channel:
        announce_channel = discord.utils.get(guild.text_channels, name="general")

    if announce_channel:
        await announce_channel.send(message)
        await ctx.send(f"✓ Announcement sent to {announce_channel.mention}")
    else:
        await ctx.send("No announcement channel found.")


@bot.command(name="reset")
async def reset(ctx: commands.Context):
    """Forget this channel's conversation and begin again from nothing."""
    nova_claude.SESSIONS.pop(conversation_key(ctx.message), None)
    await ctx.send("Cleared. We start from nothing.")


def conversation_key(message: discord.Message) -> str:
    """A DM is per person; a channel is shared by everyone talking in it."""
    if message.guild is None:
        return f"dm:{message.author.id}"
    return f"ch:{message.channel.id}"


def addressed_to_nova(message: discord.Message) -> str | None:
    """The prompt if this message is for Nova, otherwise None.

    Three ways to get her attention: mention her, DM her, or open with her
    name. Anything else in a busy channel is none of her business.
    """
    content = (message.content or "").strip()
    if not content:
        return None

    # A real command (!ping, !announce) is handled by the command framework.
    if content.startswith("!") and not content.lower().startswith("!ask "):
        return None

    mentioned = bot.user in message.mentions
    is_dm = message.guild is None
    lead = next((p for p in ("!ask ", "nova, ", "nova ", "nova,")
                 if content.lower().startswith(p)), None)

    if not (mentioned or is_dm or lead):
        return None

    prompt = re.sub(rf"<@!?{bot.user.id}>", "", content).strip()
    if lead and prompt.lower().startswith(lead):
        prompt = prompt[len(lead):].strip()
    return prompt


@bot.event
async def on_message(message: discord.Message):
    if message.author.bot:
        return

    # Commands keep priority; this handles only what they leave behind.
    await bot.process_commands(message)

    prompt = addressed_to_nova(message)
    if prompt is None:
        return

    # First contact goes to Uno, not to the bin: allow them, answer once, or
    # never ask again. His answer is remembered, so this happens once per
    # person rather than once per message.
    state = nova_agent.chat_state(message.author.id)
    if state == "blocked":
        return
    if state == "unknown":
        ok, why = nova_agent.quota_check(message.author.id)
        if not ok:
            log.info("ignoring %s (%s)", message.author, why)
            return
        nova_agent.quota_touch(message.author.id)
        if await nova_slash.ask_to_chat(bot, message, prompt):
            log.info("asked Uno whether %s may talk to her", message.author)
        return
    if not prompt:
        await message.reply("You have my attention.", mention_author=False)
        return

    if nova_claude.CLAUDE is None:
        await message.reply(
            "I cannot reach Claude on this machine, so I have nothing to answer with.",
            mention_author=False)
        return

    # Tools that can change this PC are for the owner alone, and only in a DM.
    privileged = (bool(nova_claude.OWNER_ID)
                  and str(message.author.id) == nova_claude.OWNER_ID
                  and message.guild is None)

    who = message.author.display_name
    framed = f"[Discord message from {who}]\n{prompt}"

    log.info("asking claude for %s: %s", who, prompt[:120])
    async with message.channel.typing():
        is_owner = str(message.author.id) == nova_agent.OWNER_ID
        answer = await nova_claude.ask(framed, conversation_key(message), privileged,
                                       nova_claude.model_for(is_owner), owner=is_owner)

    for index, piece in enumerate(nova_claude.chunk(answer)):
        if index == 0:
            await message.reply(piece, mention_author=False)
        else:
            await message.channel.send(piece)


if __name__ == "__main__":
    # reconnect=True is the default, but being explicit: this is what keeps the
    # bot online through brief network drops instead of exiting.
    bot.run(TOKEN, reconnect=True, log_handler=None)
