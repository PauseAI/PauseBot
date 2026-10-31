"""Turns a Discord server's structure into plain JSON for server_snapshots (replacing Xenon)."""
import discord


def _overwrites(channel, role_ids):
    result = []
    for target, overwrite in channel.overwrites.items():
        allow, deny = overwrite.pair()
        result.append({
            "target_id": target.id,
            "target_type": "role" if target.id in role_ids else "member",
            "allow": allow.value,
            "deny": deny.value,
        })
    return result


def serialize_guild(guild, bans):
    """`bans` is a list of discord.BanEntry, or None if the bot can't read bans."""
    role_ids = {role.id for role in guild.roles}

    roles = [
        {
            "id": role.id,
            "name": role.name,
            "color": role.color.value,
            "permissions": role.permissions.value,
            "position": role.position,
            "hoist": role.hoist,
            "mentionable": role.mentionable,
            "managed": role.managed,
        }
        for role in guild.roles
    ]

    # guild.channels includes categories; category_id ties the rest back to them
    channels = [
        {
            "id": channel.id,
            "name": channel.name,
            "type": str(channel.type),
            "position": channel.position,
            "category_id": getattr(channel, "category_id", None),
            "topic": getattr(channel, "topic", None),
            "nsfw": getattr(channel, "nsfw", False),
            "slowmode_delay": getattr(channel, "slowmode_delay", 0),
            "overwrites": _overwrites(channel, role_ids),
        }
        for channel in guild.channels
    ]

    members = [
        {
            "id": member.id,
            "nick": member.nick,
            "role_ids": [role.id for role in member.roles if not role.is_default()],
        }
        for member in guild.members
    ]

    return {
        "guild": {"id": guild.id, "name": guild.name},
        "roles": roles,
        "channels": channels,
        "members": members,
        "bans": None if bans is None else [
            {"user_id": entry.user.id, "reason": entry.reason} for entry in bans
        ],
    }


async def take_snapshot(guild):
    try:
        bans = [entry async for entry in guild.bans(limit=None)]
    except discord.Forbidden:
        print(f"Snapshot of {guild.name}: missing Ban Members permission, skipping bans.")
        bans = None
    return serialize_guild(guild, bans)
