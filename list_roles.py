"""Print the server's roles with their IDs.

Run this once during setup: the .env names roles by ID rather than by name,
because a name can be renamed or duplicated by anyone with Manage Roles.

    python list_roles.py
"""
import asyncio, os
import discord
from dotenv import load_dotenv
load_dotenv()
GUILD = int(os.getenv("NOVA_GUILD_ID", "0"))

async def main():
    intents = discord.Intents.default()
    intents.members = True
    client = discord.Client(intents=intents)

    @client.event
    async def on_ready():
        try:
            g = client.get_guild(GUILD)
            if not g:
                print("bot is not in guild", GUILD); return
            print(f"{g.name} — {len(g.roles)} roles\n")
            for r in sorted(g.roles, key=lambda r: r.position, reverse=True):
                print(f"  {r.id}  {'@' + r.name!r:<28} members={len(r.members)}")
        finally:
            await client.close()

    await client.start(os.getenv("DISCORD_TOKEN"))

asyncio.run(main())
