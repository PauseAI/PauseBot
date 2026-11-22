"""An in-memory stand-in for the parts of Discord's REST API that jobs/country_regions.py uses."""
import copy
import json
import re
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from jobs.country_regions import DiscordError

FIXTURE = Path(__file__).parent / "fixtures" / "country_regions_guild.json"


def load_fixture():
    return json.loads(FIXTURE.read_text())


class FakeDiscord:
    def __init__(self, data=None, fail_on_write=None):
        data = copy.deepcopy(data or load_fixture())
        self.guild_id = data["guild_id"]
        self.bot_id = data["bot_user_id"]
        self.roles = {r["id"]: r for r in data["roles"]}
        self.channels = {c["id"]: c for c in data["channels"]}
        self.members = data["members"]
        self.onboarding = data["onboarding"]
        self.messages = {}  # channel id -> list of messages, oldest first
        self.writes = []  # (method, path, body)
        self.fail_on_write = fail_on_write  # Raise on this write number (1-based), to test reruns
        self._next_id = 900000

    def new_id(self):
        self._next_id += 1
        return str(self._next_id)

    def request(self, method, path, body=None):
        if method != "GET":
            self.writes.append((method, path, copy.deepcopy(body)))
            if self.fail_on_write == len(self.writes):
                raise DiscordError(method, path, 500, "injected failure")
        url = urlparse(path)
        query = {k: v[0] for k, v in parse_qs(url.query).items()}
        for pattern, handler in self.routes():
            match = re.fullmatch(pattern, f"{method} {url.path}")
            if match:
                return copy.deepcopy(handler(body, query, *match.groups()))
        raise AssertionError(f"FakeDiscord has no route for {method} {path}")

    def routes(self):
        g = self.guild_id
        return [
            (rf"GET /guilds/{g}/roles", lambda b, q: list(self.roles.values())),
            (rf"GET /guilds/{g}/channels", lambda b, q: list(self.channels.values())),
            (rf"GET /guilds/{g}/members", self.list_members),
            (rf"GET /guilds/{g}/members/(\d+)", lambda b, q, i: next(m for m in self.members if m["user"]["id"] == i)),
            (r"GET /users/@me", lambda b, q: {"id": self.bot_id}),
            (rf"PATCH /guilds/{g}/roles/(\d+)", self.edit_role),
            (rf"POST /guilds/{g}/channels", self.create_channel),
            (rf"PATCH /guilds/{g}/channels", self.move_channels),
            (r"PATCH /channels/(\d+)", self.edit_channel),
            (r"PUT /channels/(\d+)/permissions/(\d+)", self.put_overwrite),
            (r"DELETE /channels/(\d+)", self.delete_channel),
            (r"POST /channels/(\d+)/messages", self.post_message),
            (r"GET /channels/(\d+)/messages", lambda b, q, i: list(reversed(self.messages.get(i, [])))[:int(q["limit"])]),
            (rf"GET /guilds/{g}/onboarding", lambda b, q: self.onboarding),
            (rf"PUT /guilds/{g}/onboarding", self.put_onboarding),
        ]

    def list_members(self, body, query):
        after = int(query.get("after", 0))
        page = sorted((m for m in self.members if int(m["user"]["id"]) > after), key=lambda m: int(m["user"]["id"]))
        return page[:int(query.get("limit", 1))]

    def edit_role(self, body, query, role_id):
        self.roles[role_id].update(body)
        return self.roles[role_id]

    def create_channel(self, body, query):
        assert len(self.channels) < 500
        if body.get("parent_id"):
            assert body["parent_id"] in self.channels
            assert len([c for c in self.channels.values() if c.get("parent_id") == body["parent_id"]]) < 50
        channel = {
            "id": self.new_id(), "name": body["name"], "type": body["type"], "guild_id": self.guild_id,
            "position": len(self.channels), "parent_id": body.get("parent_id"), "flags": 0,
            "permission_overwrites": body.get("permission_overwrites", []),
        }
        if body["type"] == 0:
            channel.update(topic=body.get("topic"), last_message_id=None)
        self.channels[channel["id"]] = channel
        return channel

    def move_channels(self, body, query):
        moving = [e for e in body if "parent_id" in e and e["parent_id"] != self.channels[e["id"]].get("parent_id")]
        if len(moving) > 1:  # Discord: only one channel's parent can change per request (40009)
            raise DiscordError("PATCH", "channels", 400, "40009")
        for entry in body:
            channel = self.channels[entry["id"]]
            if "parent_id" in entry:
                channel["parent_id"] = entry["parent_id"]
                if entry.get("lock_permissions"):
                    channel["permission_overwrites"] = copy.deepcopy(
                        self.channels[entry["parent_id"]]["permission_overwrites"])
            if "position" in entry:
                channel["position"] = entry["position"]
        return None

    def edit_channel(self, body, query, channel_id):
        self.channels[channel_id].update(body)
        return self.channels[channel_id]

    def put_overwrite(self, body, query, channel_id, target_id):
        channel = self.channels[channel_id]
        channel["permission_overwrites"] = [o for o in channel["permission_overwrites"] if o["id"] != target_id]
        channel["permission_overwrites"].append({"id": target_id, **body})
        return None

    def delete_channel(self, body, query, channel_id):
        channel = self.channels[channel_id]
        assert channel["type"] == 4, "the script must only delete categories"
        assert not [c for c in self.channels.values() if c.get("parent_id") == channel_id], "category not empty"
        del self.channels[channel_id]
        return channel

    def post_message(self, body, query, channel_id):
        message = {"id": self.new_id(), "content": body["content"], "author": {"id": self.bot_id},
                   "allowed_mentions": body.get("allowed_mentions")}
        self.messages.setdefault(channel_id, []).append(message)
        self.channels[channel_id]["last_message_id"] = message["id"]
        return message

    def put_onboarding(self, body, query):
        for prompt in body["prompts"]:
            for option in prompt["options"]:
                assert "emoji" not in option, "PUT takes emoji_id/emoji_name, not an emoji object"
                for channel_id in option["channel_ids"]:
                    assert channel_id in self.channels, f"unknown channel {channel_id}"
                for role_id in option["role_ids"]:
                    assert role_id in self.roles, f"unknown role {role_id}"
        stored = copy.deepcopy(body)
        for prompt in stored["prompts"]:  # Store it the way GET returns it
            for option in prompt["options"]:
                name, emoji_id = option.pop("emoji_name", None), option.pop("emoji_id", None)
                animated = option.pop("emoji_animated", False)
                option["emoji"] = {"id": emoji_id, "name": name, "animated": animated} if (name or emoji_id) else None
        self.onboarding = {"guild_id": self.guild_id, **stored}
        return self.onboarding

    # Helpers for tests
    def channel(self, name):
        return next(c for c in self.channels.values() if c["name"] == name)

    def category_of(self, name):
        parent = self.channel(name).get("parent_id")
        return self.channels[parent]["name"] if parent else None

    def children(self, category_name):
        category = self.channel(category_name)
        return [c["name"] for c in sorted((c for c in self.channels.values() if c.get("parent_id") == category["id"]),
                                          key=lambda c: (c["position"], int(c["id"])))]

    def structure(self):
        """Everything a rerun should leave the same, without IDs that depend on creation order."""
        return sorted(
            (c["name"], self.channels[c["parent_id"]]["name"] if c.get("parent_id") else None, c["position"],
             str(sorted((o["id"], o["allow"], o["deny"]) for o in c["permission_overwrites"])))
            for c in self.channels.values())
