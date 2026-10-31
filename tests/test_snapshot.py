import json
from types import SimpleNamespace
import discord
import snapshot


class NS(SimpleNamespace):
    # Roles and members are dict keys in channel.overwrites, so fakes need to be hashable
    __hash__ = object.__hash__


def role(id, name, permissions=0, position=0, default=False):
    return NS(
        id=id, name=name, color=discord.Colour(0x112233), permissions=discord.Permissions(permissions),
        position=position, hoist=False, mentionable=True, managed=False, is_default=lambda: default,
    )


def overwrite(allow, deny):
    return NS(pair=lambda: (discord.Permissions(allow), discord.Permissions(deny)))


def make_guild():
    everyone = role(1, "@everyone", default=True)
    mod = role(2, "Moderator", permissions=discord.Permissions(kick_members=True).value, position=5)
    member = NS(id=10, nick="Pat", roles=[everyone, mod])
    category = NS(id=20, name="Info", type=discord.ChannelType.category, position=0, overwrites={})
    text = NS(
        id=21, name="rules", type=discord.ChannelType.text, position=1, category_id=20,
        topic="Read me", nsfw=False, slowmode_delay=5,
        overwrites={everyone: overwrite(0, 2048), member: overwrite(2048, 0)},
    )
    return NS(id=100, name="PauseAI", roles=[everyone, mod], channels=[category, text], members=[member])


def test_serialize_guild_captures_structure():
    bans = [NS(user=NS(id=99), reason="spam")]
    data = snapshot.serialize_guild(make_guild(), bans)

    assert data["guild"] == {"id": 100, "name": "PauseAI"}
    assert data["roles"][1] == {
        "id": 2, "name": "Moderator", "color": 0x112233, "permissions": 2,
        "position": 5, "hoist": False, "mentionable": True, "managed": False,
    }
    category, text = data["channels"]
    assert category["type"] == "category" and category["category_id"] is None and category["topic"] is None
    assert text["type"] == "text" and text["category_id"] == 20 and text["slowmode_delay"] == 5
    assert text["overwrites"] == [
        {"target_id": 1, "target_type": "role", "allow": 0, "deny": 2048},
        {"target_id": 10, "target_type": "member", "allow": 2048, "deny": 0},
    ]
    # @everyone is implied, so it's left out of member roles
    assert data["members"] == [{"id": 10, "nick": "Pat", "role_ids": [2]}]
    assert data["bans"] == [{"user_id": 99, "reason": "spam"}]
    json.dumps(data)  # Must be storable as jsonb as-is


def test_serialize_guild_without_ban_access():
    assert snapshot.serialize_guild(make_guild(), None)["bans"] is None


async def test_take_snapshot_skips_bans_when_forbidden():
    guild = make_guild()

    async def bans(limit=None):
        raise discord.Forbidden(NS(status=403, reason="Forbidden"), "Missing Permissions")
        yield

    guild.bans = bans
    data = await snapshot.take_snapshot(guild)
    assert data["bans"] is None and len(data["roles"]) == 2


async def test_take_snapshot_reads_bans():
    guild = make_guild()

    async def bans(limit=None):
        yield NS(user=NS(id=7), reason=None)

    guild.bans = bans
    assert (await snapshot.take_snapshot(guild))["bans"] == [{"user_id": 7, "reason": None}]
