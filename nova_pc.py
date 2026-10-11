"""Owner-only shortcuts that never touch Claude: fetch a file, launch an app.

These are the two things Uno actually asks Nova for most, and neither needs a
model to think about it. A file index and a Start Menu index answer the
autocomplete; the command itself is a plain upload or os.startfile. Zero tokens.

Everything here answers only to NOVA_OWNER_ID. For anyone else the commands
refuse and the autocomplete returns nothing -- a list of Uno's file names is
itself a leak.
"""

import asyncio
import hashlib
import logging
import os
import shutil
import tempfile
import time
from pathlib import Path

import discord
from discord import app_commands

log = logging.getLogger("nova.pc")

HOME = Path.home()
OWNER_ID = os.getenv("NOVA_OWNER_ID", "").strip()

FILE_ROOTS = [HOME / d for d in ("Desktop", "Documents", "Downloads", "Pictures",
                                 "Videos", "Music")]
SKIP_DIRS = {"node_modules", "__pycache__", "venv", ".venv", "site-packages",
             "$RECYCLE.BIN", "AppData"}
MAX_DEPTH = 5
MAX_ENTRIES = 60000
REINDEX_EVERY = 900          # seconds

APP_ROOTS = [
    Path(os.getenv("APPDATA", HOME / "AppData/Roaming")) / "Microsoft/Windows/Start Menu/Programs",
    Path(os.getenv("PROGRAMDATA", "C:/ProgramData")) / "Microsoft/Windows/Start Menu/Programs",
    HOME / "Desktop",
    Path(os.getenv("PUBLIC", "C:/Users/Public")) / "Desktop",
]
APP_EXTS = {".lnk", ".url", ".exe", ".bat", ".cmd", ".vbs"}

# (lowercased path, Path, mtime, is_dir)
_files: list[tuple[str, Path, float, bool]] = []
_apps: dict[str, Path] = {}          # display name -> shortcut
_long: dict[str, Path] = {}          # autocomplete value for paths over 100 chars
_state = {"built": 0.0, "building": False}


def _walk(root: Path, depth: int, out: list):
    if depth > MAX_DEPTH or len(out) >= MAX_ENTRIES:
        return
    try:
        entries = list(os.scandir(root))
    except OSError:
        return
    for e in entries:
        if e.name.startswith((".", "~$")):
            continue
        try:
            is_dir = e.is_dir(follow_symlinks=False)
            mtime = e.stat(follow_symlinks=False).st_mtime
        except OSError:
            continue
        if is_dir and e.name in SKIP_DIRS:
            continue
        p = Path(e.path)
        out.append((str(p).lower(), p, mtime, is_dir))
        if is_dir:
            _walk(p, depth + 1, out)


def _build():
    files: list = []
    for root in FILE_ROOTS:
        if root.is_dir():
            _walk(root, 1, files)
    apps: dict[str, Path] = {}
    for root in APP_ROOTS:
        if not root.is_dir():
            continue
        for p in root.rglob("*"):
            if p.suffix.lower() in APP_EXTS and p.is_file():
                name = p.stem
                if "uninstall" in name.lower():
                    continue
                apps.setdefault(name, p)
    global _files, _apps
    _files, _apps = files, apps
    _state["built"] = time.time()
    log.info("pc index: %d files/folders, %d apps", len(files), len(apps))


async def refresh(force: bool = False):
    if _state["building"]:
        return
    if not force and time.time() - _state["built"] < REINDEX_EVERY:
        return
    _state["building"] = True
    try:
        await asyncio.to_thread(_build)
    finally:
        _state["building"] = False


def is_owner(user) -> bool:
    return bool(OWNER_ID) and str(user.id) == OWNER_ID


def _value_for(p: Path) -> str:
    s = str(p)
    if len(s) <= 100:
        return s
    key = "@" + hashlib.sha1(s.encode()).hexdigest()[:16]
    _long[key] = p
    return key


def _label(p: Path, is_dir: bool) -> str:
    try:
        rel = str(p.relative_to(HOME))
    except ValueError:
        rel = str(p)
    rel = ("📁 " if is_dir else "") + rel
    return rel if len(rel) <= 100 else "…" + rel[-99:]


def _resolve(value: str) -> Path | None:
    if value.startswith("@"):
        return _long.get(value)
    p = Path(os.path.expandvars(os.path.expanduser(value.strip().strip('"'))))
    if p.exists():
        return p
    # A bare name typed without picking from the list: take the best match.
    hits = search_files(value, 1)
    return hits[0][0] if hits else None


def search_files(query: str, limit: int = 25):
    words = query.lower().split()
    if not words:
        recent = sorted((f for f in _files if not f[3]), key=lambda f: -f[2])
        return [(f[1], f[3]) for f in recent[:limit]]
    hits = []
    for low, p, mtime, is_dir in _files:
        if all(w in low for w in words):
            name_hit = all(w in p.name.lower() for w in words)
            hits.append((not name_hit, -mtime, p, is_dir))
    hits.sort(key=lambda h: (h[0], h[1]))
    return [(h[2], h[3]) for h in hits[:limit]]


def search_apps(query: str, limit: int = 25):
    q = query.lower().strip()
    names = sorted(_apps)
    if q:
        names = [n for n in names if q in n.lower()]
        names.sort(key=lambda n: (not n.lower().startswith(q), len(n)))
    return names[:limit]


def _zip_folder(folder: Path) -> Path:
    out = Path(tempfile.mkdtemp(prefix="nova-")) / folder.name
    return Path(shutil.make_archive(str(out), "zip", root_dir=folder))


def setup(bot):
    @bot.tree.command(name="getfile", description="Owner only: send a file or folder from the PC")
    @app_commands.describe(path="Start typing a file name, or paste a full path")
    async def getfile(interaction: discord.Interaction, path: str):
        if not is_owner(interaction.user):
            await interaction.response.send_message("That one is not for you.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        p = _resolve(path)
        if p is None or not p.exists():
            await interaction.followup.send(f"I cannot find `{path}`.", ephemeral=True)
            return
        limit = interaction.guild.filesize_limit if interaction.guild else 10 * 1024 * 1024
        tmp = None
        try:
            send = p
            if p.is_dir():
                tmp = await asyncio.to_thread(_zip_folder, p)
                send = tmp
            size = send.stat().st_size
            if size > limit:
                await interaction.followup.send(
                    f"`{p.name}` is {size / 1048576:.1f} MB; Discord caps this at "
                    f"{limit / 1048576:.0f} MB.", ephemeral=True)
                return
            await interaction.followup.send(f"`{p}`", file=discord.File(str(send)),
                                            ephemeral=True)
            log.info("getfile: sent %s (%d bytes)", p, size)
        finally:
            if tmp:
                shutil.rmtree(tmp.parent, ignore_errors=True)

    @getfile.autocomplete("path")
    async def getfile_complete(interaction: discord.Interaction, current: str):
        if not is_owner(interaction.user):
            return []
        asyncio.create_task(refresh())
        return [app_commands.Choice(name=_label(p, d), value=_value_for(p))
                for p, d in search_files(current)]

    @bot.tree.command(name="launch", description="Owner only: open an app or shortcut on the PC")
    @app_commands.describe(app="Start typing the app's name")
    async def launch(interaction: discord.Interaction, app: str):
        if not is_owner(interaction.user):
            await interaction.response.send_message("That one is not for you.", ephemeral=True)
            return
        target = _apps.get(app)
        if target is None:
            names = search_apps(app, 1)
            target = _apps.get(names[0]) if names else None
        if target is None:
            await interaction.response.send_message(f"No app called `{app}`.", ephemeral=True)
            return
        try:
            os.startfile(str(target))
        except OSError as exc:
            await interaction.response.send_message(f"It would not start: `{exc}`", ephemeral=True)
            return
        log.info("launch: %s", target)
        await interaction.response.send_message(f"Launched **{target.stem}**.", ephemeral=True)

    @launch.autocomplete("app")
    async def launch_complete(interaction: discord.Interaction, current: str):
        if not is_owner(interaction.user):
            return []
        asyncio.create_task(refresh())
        return [app_commands.Choice(name=n[:100], value=n[:100]) for n in search_apps(current)]
