"""Restructures the PauseAI Discord's country channels into five regional categories, once.

Run by hand from the repo root with DISCORD_TOKEN set to a bot token whose role has (for now)
Manage Channels, Manage Roles, Manage Server and Mention @everyone. See the README's
"Country regions" section for the full run order.

    python jobs/country_regions.py                       # dry run: reads the server, prints every change
    python jobs/country_regions.py --apply               # makes the channel and role changes
    python jobs/country_regions.py --onboarding          # writes the new onboarding config to a file
    python jobs/country_regions.py --onboarding --apply-onboarding   # and sends it to Discord
    python jobs/country_regions.py --restore-layout country_regions_backup.json [--apply]
    python jobs/country_regions.py --restore-onboarding country_regions_backup.json [--apply]

Everything is computed from the live server each time, so it's safe to run again after a partial
failure: anything already done is skipped. Nothing is deleted except the two old country
categories, and those only once they're empty.
"""
import argparse
import datetime
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))  # So running this file directly can import jobs.*

from jobs.country_regions_table import (  # noqa: E402
    COUNTRIES, EXTRA_CHANNELS, KEEP_OWN_OVERWRITES, REGIONS, region_of,
)

API = "https://discord.com/api/v10"
PAUSEAI_GUILD_ID = "1100491867675709580"
AUDIT_REASON = "Country channels into regional categories (PauseBot jobs/country_regions.py)"

TEXT, CATEGORY = 0, 4
ADMINISTRATOR = 1 << 3
MANAGE_CHANNELS = 1 << 4
MANAGE_GUILD = 1 << 5
SEND_MESSAGES = 1 << 11
MENTION_EVERYONE = 1 << 17
MANAGE_ROLES = 1 << 28

# Discord's channel limits: 50 per category (in the channel docs), 500 per server.
MAX_PER_CATEGORY = 50
MAX_PER_GUILD = 500
# Discord's API docs (checked 22 Nov 2026) give no limit on onboarding questions or options per
# question; these are the limits Discord's own onboarding editor applies, as far as we know. If
# Discord's real limit is lower it rejects the whole PUT and onboarding stays as it was.
MAX_PROMPTS = 15
MAX_OPTIONS_PER_PROMPT = 50

OLD_CATEGORY_KEYS = ["COUNTRIES", "COUNTRIES+"]
REGION_CATEGORY_NAMES = {
    "Europe": "🌍 EUROPE",
    "Americas": "🌎 AMERICAS",
    "Africa": "🌍 AFRICA",
    "Middle East": "🌍 MIDDLE EAST",
    "Asia-Pacific": "🌏 ASIA-PACIFIC",
}
REGION_EMOJI = {"Europe": "🌍", "Americas": "🌎", "Africa": "🌍", "Middle East": "🌍", "Asia-Pacific": "🌏"}
PAUSED_ROLE_NAME = "Paused (Wick)"
NATIONAL_LEADER_ROLE_NAME = "National Leader"
ROLE_RENAMES = {"Dominian Republic": "Dominican Republic"}
REQUEST_CHANNEL_PREFIX = "request-a-country"
REQUEST_CHANNEL_DESTINATION = "SERVER GUIDE RESOURCES"
OTHER_TOPIC = "From a country without its own channel? Say hello here. A Server Manager will open one when needed."
REQUEST_NOTICE_START = "Country requests are no longer needed."
WELCOME_MARK = "this is your channel now"
PROMPT_TITLE_PREFIX = "Where are you? "
SOMEWHERE_ELSE = "Somewhere else in {region}"

# What the 21 Nov 2026 count expected to be created, to show differences in the dry run.
EXPECTED_NEW_CHANNELS = {
    "russia", "ecuador", "costa-rica", "dominican-republic", "venezuela", "bolivia", "chile", "panama",
    "kenya", "egypt", "morocco", "dr-congo", "algeria", "ivory-coast", "united-arab-emirates", "iran",
    "iraq", "vietnam", "thailand", "bangladesh", "pakistan",
}


class DiscordError(Exception):
    def __init__(self, method, path, status, body):
        super().__init__(f"{method} {path} failed: HTTP {status}: {body}")
        self.status = status


class Abort(Exception):
    """Something found before any change that means the script shouldn't go on."""


def slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def norm(name):
    """A category name without its emoji, for finding categories however they're decorated."""
    return " ".join(re.sub(r"[^A-Za-z0-9+\- ]", " ", name).upper().split())


def other_channel_name(region):
    return f"other-{slug(region)}"


def flag(iso):
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso)


def country_iso(country):
    for countries in COUNTRIES.values():
        if country in countries:
            return countries[country]
    return None


class DiscordHTTP:
    """Discord's REST API over urllib, waiting out rate limits. Never prints the token."""

    def __init__(self, token, sleep=time.sleep):
        self._auth = f"Bot {token}"
        self._sleep = sleep

    def request(self, method, path, body=None):
        headers = {
            "Authorization": self._auth,
            "User-Agent": "DiscordBot (https://github.com/PauseAI/PauseBot, 1.0)",
            "Content-Type": "application/json",
        }
        if method != "GET":
            headers["X-Audit-Log-Reason"] = AUDIT_REASON
        data = None if body is None else json.dumps(body).encode()
        for attempt in range(8):
            req = urllib.request.Request(API + path, data=data, method=method, headers=headers)
            try:
                with urllib.request.urlopen(req, timeout=60) as response:
                    raw = response.read()
                    if response.headers.get("X-RateLimit-Remaining") == "0":
                        self._sleep(float(response.headers.get("X-RateLimit-Reset-After", "1")))
                    return json.loads(raw) if raw else None
            except urllib.error.HTTPError as error:
                text = error.read().decode(errors="replace")
                if error.code == 429:
                    try:
                        wait = float(json.loads(text).get("retry_after", 1))
                    except ValueError:
                        wait = 5
                    print(f"  (rate limited, waiting {wait:.1f}s)")
                    self._sleep(wait + 0.5)
                    continue
                if error.code >= 500:
                    self._sleep(2 ** attempt)
                    continue
                raise DiscordError(method, path, error.code, text[:1000]) from None
            except urllib.error.URLError as error:
                print(f"  (network error {error.reason}, retrying)")
                self._sleep(2 ** attempt)
        raise DiscordError(method, path, "gave up", "too many retries")


class State:
    """The server as the script sees it, updated as changes are made (or would be, in a dry run)."""

    def __init__(self, guild_id, roles, channels, role_counts, leader_held):
        self.guild_id = str(guild_id)
        self.roles = {r["id"]: r for r in roles}
        self.channels = {c["id"]: c for c in channels}
        self.role_counts = role_counts  # role id -> members holding it
        self.leader_held = leader_held  # role ids held by someone with the National Leader role

    def role_named(self, name):
        return next((r for r in self.roles.values() if r["name"] == name), None)

    def categories(self):
        return sorted((c for c in self.channels.values() if c["type"] == CATEGORY),
                      key=lambda c: (c["position"], int_id(c["id"])))

    def category(self, key):
        return next((c for c in self.categories() if norm(c["name"]) == key), None)

    def children(self, category_id):
        return sorted((c for c in self.channels.values() if c.get("parent_id") == category_id),
                      key=lambda c: (c["position"], int_id(c["id"])))

    def country_roles(self):
        """{country name: role} for every role named in the region table."""
        return {r["name"]: r for r in self.roles.values() if region_of(r["name"])}


def int_id(value):
    return int(value) if str(value).isdigit() else 1 << 64  # Dry-run placeholders sort last


def load_state(http, guild_id):
    roles = http.request("GET", f"/guilds/{guild_id}/roles")
    channels = http.request("GET", f"/guilds/{guild_id}/channels")
    leader = next((r for r in roles if r["name"] == NATIONAL_LEADER_ROLE_NAME), None)
    counts, leader_held, after = {}, set(), "0"
    while True:
        page = http.request("GET", f"/guilds/{guild_id}/members?limit=1000&after={after}")
        for member in page:
            for role_id in member["roles"]:
                counts[role_id] = counts.get(role_id, 0) + 1
            if leader and leader["id"] in member["roles"]:
                leader_held.update(member["roles"])
        if len(page) < 1000:
            break
        after = max(page, key=lambda m: int(m["user"]["id"]))["user"]["id"]
    return State(guild_id, roles, channels, counts, leader_held)


def overwrites_key(overwrites):
    return sorted((str(o["id"]), int(o["type"]), str(o["allow"]), str(o["deny"])) for o in overwrites)


def clean_overwrites(overwrites):
    return [{"id": o["id"], "type": o["type"], "allow": str(o["allow"]), "deny": str(o["deny"])} for o in overwrites]


class Run:
    """Makes changes, or in a dry run only prints them. Either way it updates State to match."""

    def __init__(self, http, state, apply, out=print):
        self.http, self.state, self.apply, self.out = http, state, apply, out
        self.changes, self.warnings = [], []
        self._placeholder = 0
        self.can_mention_all = True  # Set by check_bot

    def warn(self, text):
        self.warnings.append(text)
        self.out(f"WARNING: {text}")

    def change(self, text, method, path, body=None):
        self.changes.append(text)
        self.out(("  " if self.apply else "  [dry run] ") + text)
        if self.apply:
            return self.http.request(method, path, body)
        self._placeholder += 1
        return None

    def placeholder_id(self):
        return f"dry-run-{self._placeholder}"


# ---- The restructure (--apply) ----

class Plan:
    def __init__(self):
        self.old = {}  # key -> category
        self.template = None  # overwrites for new regional categories
        self.regions = {}  # region -> list of (channel name, role or None)
        self.request_channel = None
        self.guide = None


def plan_restructure(state, out=print):
    """Checks the server and works out the target layout, before anything changes. Raises Abort."""
    plan = Plan()
    for key in OLD_CATEGORY_KEYS:
        if (category := state.category(key)):
            plan.old[key] = category
    regional = {r: state.category(norm(REGION_CATEGORY_NAMES[r])) for r in REGIONS}
    if "COUNTRIES" in plan.old:
        paused = state.role_named(PAUSED_ROLE_NAME)
        plan.template = clean_overwrites(
            o for o in plan.old["COUNTRIES"]["permission_overwrites"] if not (paused and o["id"] == paused["id"]))
    elif not all(regional.values()):
        raise Abort("Neither the old COUNTRIES category nor all five regional categories exist, "
                    "so there are no permission overwrites to copy. Nothing changed.")

    pool_categories = {c["id"] for c in plan.old.values()} | {c["id"] for c in regional.values() if c}
    pool = {c["name"]: c for c in state.channels.values() if c.get("parent_id") in pool_categories}

    # Roles by the name they'll have after the renames
    roles = state.country_roles()
    for old_name, new_name in ROLE_RENAMES.items():
        if state.role_named(old_name) and new_name not in roles:
            roles[new_name] = state.role_named(old_name)

    problems = []
    for category in plan.old.values():
        for channel in state.children(category["id"]):
            name = channel["name"]
            if name.startswith(REQUEST_CHANNEL_PREFIX):
                plan.request_channel = channel
            elif channel["type"] != TEXT:
                problems.append(f"#{name} in {category['name']} isn't a text channel")
            elif name not in EXTRA_CHANNELS and not region_of(COUNTRY_BY_SLUG.get(name, "")):
                problems.append(f"#{name} in {category['name']} isn't in the region table")
    if problems:
        raise Abort("Some channels can't be placed, so nothing changed:\n  " + "\n  ".join(problems))

    if plan.request_channel is None:
        plan.request_channel = next((c for c in state.channels.values()
                                     if c["name"].startswith(REQUEST_CHANNEL_PREFIX)), None)
    plan.guide = state.category(REQUEST_CHANNEL_DESTINATION)

    outside = {c["name"] for c in state.channels.values()
               if c.get("parent_id") not in pool_categories and c["type"] != CATEGORY}
    for region in REGIONS:
        entries = []
        for country, role in roles.items():
            if region_of(country) != region:
                continue
            name = slug(country)
            wanted = (name in pool or state.role_counts.get(role["id"], 0) > 0
                      or role["id"] in state.leader_held)
            if not wanted:
                continue
            if name not in pool and name in outside:
                out(f"WARNING: a #{name} channel exists outside the country categories, so none is created "
                    f"for {country}. Move it into {REGION_CATEGORY_NAMES[region]} by hand if it's the country's.")
                continue
            entries.append((name, role))
        for name, extra_region in EXTRA_CHANNELS.items():
            if extra_region == region and name in pool:
                entries.append((name, None))
        entries.sort(key=lambda e: e[0])
        entries.append((other_channel_name(region), None))
        if len(entries) > MAX_PER_CATEGORY:
            raise Abort(f"{region} would have {len(entries)} channels, over Discord's {MAX_PER_CATEGORY} "
                        "per category. Nothing changed.")
        plan.regions[region] = entries

    new_channels = sum(1 for entries in plan.regions.values() for name, _ in entries if name not in pool)
    new_categories = sum(1 for c in regional.values() if not c)
    peak = len(state.channels) + new_channels + new_categories
    if peak > MAX_PER_GUILD:
        raise Abort(f"The server would reach {peak} channels, over Discord's {MAX_PER_GUILD}. Nothing changed.")
    return plan


COUNTRIES_FLAT = {country for countries in COUNTRIES.values() for country in countries}
COUNTRY_BY_SLUG = {slug(country): country for country in COUNTRIES_FLAT}


def restructure(run, plan, original_channel_ids):
    """Makes the changes in order. original_channel_ids: channel IDs from before the first run,
    to tell which country channels this script created (and so should get a welcome)."""
    state, g = run.state, run.state.guild_id

    # 1. Role renames
    for old_name, new_name in ROLE_RENAMES.items():
        role = state.role_named(old_name)
        if role and not state.role_named(new_name):
            run.change(f"Rename role '{old_name}' to '{new_name}'", "PATCH", f"/guilds/{g}/roles/{role['id']}",
                       {"name": new_name})
            role["name"] = new_name

    # 2. Regional categories
    categories = {}
    for region in REGIONS:
        name = REGION_CATEGORY_NAMES[region]
        category = state.category(norm(name))
        if not category:
            created = run.change(f"Create category {name}", "POST", f"/guilds/{g}/channels",
                                 {"name": name, "type": CATEGORY, "permission_overwrites": plan.template})
            category = created or {"id": run.placeholder_id(), "name": name, "type": CATEGORY, "position": 999,
                                   "parent_id": None, "permission_overwrites": plan.template}
            state.channels[category["id"]] = category
        categories[region] = category
    order_categories(run, plan, categories)

    # 3. Country channels, other-<region>, european-union: create, move, sync, sort
    for region in REGIONS:
        for name, _ in plan.regions[region]:
            place_channel(run, plan, categories, region, name)
        sort_children(run, categories[region], [name for name, _ in plan.regions[region]])

    # 4. request-a-country: move to the server guide, post the notice, then lock it
    retire_request_channel(run, plan, categories)

    # 5. A welcome in each country channel this script created
    if original_channel_ids is None:
        run.warn("No backup from before the first run, so no welcome messages are posted (can't tell which "
                 "channels are new). Post them by hand.")
    else:
        for region in REGIONS:
            for name, role in plan.regions[region]:
                channel = find_child(state, categories[region]["id"], name)
                if (role and channel and channel["id"] not in original_channel_ids
                        and not channel.get("last_message_id")):
                    content = f"<@&{role['id']}>: {WELCOME_MARK}. Say hello to everyone else from {role['name']}!"
                    run.change(f"Post welcome in #{name} mentioning @{role['name']}", "POST",
                               f"/channels/{channel['id']}/messages",
                               {"content": content, "allowed_mentions": {"parse": [], "roles": [role["id"]]}})
                    channel["last_message_id"] = "posted"
                    if not role.get("mentionable") and not run.can_mention_all:
                        run.warn(f"@{role['name']} isn't mentionable and the bot lacks Mention @everyone, "
                                 f"so the welcome in #{name} won't ping anyone.")

    # 6. Delete the old categories once they're empty
    for key, category in plan.old.items():
        children = state.children(category["id"])
        if run.apply:  # Check Discord's own view, not just ours, before deleting anything
            live = run.http.request("GET", f"/guilds/{g}/channels")
            children = [c for c in live if c.get("parent_id") == category["id"]]
        if children:
            run.warn(f"{category['name']} still has {len(children)} channel(s) "
                     f"({', '.join('#' + c['name'] for c in children)}), so it isn't deleted.")
            continue
        run.change(f"Delete the empty category {category['name']}", "DELETE", f"/channels/{category['id']}")
        del state.channels[category["id"]]


def order_categories(run, plan, categories):
    """Puts the five regional categories, in order, where the old country categories are."""
    state, g = run.state, run.state.guild_id
    ordered = state.categories()
    regional_ids = [categories[r]["id"] for r in REGIONS]
    old_ids = [c["id"] for c in plan.old.values()]
    if not old_ids:
        return  # Old categories gone: an earlier run already placed the regional ones
    rest = [c for c in ordered if c["id"] not in regional_ids]
    last_old = max(i for i, c in enumerate(rest) if c["id"] in old_ids)
    desired = rest[:last_old + 1] + [categories[r] for r in REGIONS] + rest[last_old + 1:]
    if [c["id"] for c in desired] == [c["id"] for c in ordered] and \
            all(c["position"] == i for i, c in enumerate(desired)):
        return
    body = [{"id": c["id"], "position": i} for i, c in enumerate(desired)]
    run.change(f"Order categories: {', '.join(c['name'] for c in desired)}", "PATCH",
               f"/guilds/{g}/channels", body)
    for i, c in enumerate(desired):
        c["position"] = i


def find_child(state, category_id, name):
    return next((c for c in state.children(category_id) if c["name"] == name), None)


def find_pool_channel(run, plan, categories, name):
    ids = {c["id"] for c in plan.old.values()} | {c["id"] for c in categories.values()}
    return next((c for c in run.state.channels.values()
                 if c["name"] == name and c.get("parent_id") in ids and c["type"] == TEXT), None)


def place_channel(run, plan, categories, region, name):
    state, g = run.state, run.state.guild_id
    category = categories[region]
    channel = find_pool_channel(run, plan, categories, name)
    keep_own = name in KEEP_OWN_OVERWRITES
    if channel is None:
        body = {"name": name, "type": TEXT, "parent_id": category["id"],
                "permission_overwrites": clean_overwrites(category["permission_overwrites"])}
        if name.startswith("other-"):
            body["topic"] = OTHER_TOPIC
        created = run.change(f"Create #{name} in {category['name']}", "POST", f"/guilds/{g}/channels", body)
        channel = created or {"id": run.placeholder_id(), "name": name, "type": TEXT, "position": 999,
                              "parent_id": category["id"], "last_message_id": None,
                              "permission_overwrites": body["permission_overwrites"], "topic": body.get("topic")}
        state.channels[channel["id"]] = channel
        return
    if channel.get("parent_id") != category["id"]:
        how = "keeping its own permissions" if keep_own else "syncing permissions"
        run.change(f"Move #{name} into {category['name']}, {how}", "PATCH", f"/guilds/{g}/channels",
                   [{"id": channel["id"], "parent_id": category["id"], "lock_permissions": not keep_own}])
        channel["parent_id"] = category["id"]
        channel["position"] = 999
        if not keep_own:
            channel["permission_overwrites"] = clean_overwrites(category["permission_overwrites"])
    elif not keep_own and overwrites_key(channel["permission_overwrites"]) != \
            overwrites_key(category["permission_overwrites"]):
        overwrites = clean_overwrites(category["permission_overwrites"])
        run.change(f"Sync #{name}'s permissions with {category['name']}", "PATCH", f"/channels/{channel['id']}",
                   {"permission_overwrites": overwrites})
        channel["permission_overwrites"] = overwrites


def sort_children(run, category, order):
    """Puts a category's channels in the given order (any others after them)."""
    children = run.state.children(category["id"])
    rank = {name: i for i, name in enumerate(order)}
    desired = sorted(children, key=lambda c: (rank.get(c["name"], len(order)), c["name"]))
    if [c["id"] for c in desired] == [c["id"] for c in children] and \
            all(c["position"] == i for i, c in enumerate(desired)):
        return
    run.change(f"Sort the {len(desired)} channels in {category['name']}", "PATCH",
               f"/guilds/{run.state.guild_id}/channels",
               [{"id": c["id"], "position": i} for i, c in enumerate(desired)])
    for i, c in enumerate(desired):
        c["position"] = i


def request_notice(categories, state):
    mentions = []
    for region in REGIONS:
        channel = find_child(state, categories[region]["id"], other_channel_name(region))
        mentions.append(f"<#{channel['id']}>" if channel else f"#{other_channel_name(region)}")
    return (f"{REQUEST_NOTICE_START} Every country now has a role: pick yours in <id:customize> "
            f"(Channels & Roles). If your country doesn't have its own channel yet, say hello in your region's "
            f"channel: {', '.join(mentions)}. A Server Manager will open a country channel when it's needed.")


def retire_request_channel(run, plan, categories):
    state, g = run.state, run.state.guild_id
    channel = plan.request_channel
    if channel is None:
        run.warn("No request-a-country channel found; nothing to lock.")
        return
    name = channel["name"]
    if plan.guide is None:
        run.warn(f"No {REQUEST_CHANNEL_DESTINATION} category, so #{name} stays where it is "
                 "(and its category can't be deleted).")
    elif channel.get("parent_id") != plan.guide["id"]:
        siblings = state.children(plan.guide["id"])
        position = max((c["position"] for c in siblings), default=-1) + 1
        run.change(f"Move #{name} to the end of {plan.guide['name']}, keeping its own permissions", "PATCH",
                   f"/guilds/{g}/channels",
                   [{"id": channel["id"], "parent_id": plan.guide["id"], "lock_permissions": False,
                     "position": position}])
        channel["parent_id"], channel["position"] = plan.guide["id"], position

    if run.apply:
        recent = run.http.request("GET", f"/channels/{channel['id']}/messages?limit=50")
        posted = any(m.get("content", "").startswith(REQUEST_NOTICE_START) for m in recent)
    else:
        posted = False
    if not posted:
        run.change(f"Post the 'requests are no longer needed' notice in #{name}", "POST",
                   f"/channels/{channel['id']}/messages",
                   {"content": request_notice(categories, state), "allowed_mentions": {"parse": []}})

    everyone = state.guild_id
    current = next((o for o in channel["permission_overwrites"] if o["id"] == everyone), None)
    allow, deny = (int(current["allow"]), int(current["deny"])) if current else (0, 0)
    if not deny & SEND_MESSAGES:
        new = {"id": everyone, "type": 0, "allow": str(allow & ~SEND_MESSAGES), "deny": str(deny | SEND_MESSAGES)}
        run.change(f"Lock #{name}: deny Send Messages for @everyone", "PUT",
                   f"/channels/{channel['id']}/permissions/{everyone}",
                   {"type": 0, "allow": new["allow"], "deny": new["deny"]})
        channel["permission_overwrites"] = [o for o in channel["permission_overwrites"] if o["id"] != everyone] + [new]


def check_bot(http, state, run, onboarding=False):
    """Warns about permissions the bot's roles are missing (best effort: channel overwrites aside)."""
    me = http.request("GET", "/users/@me")
    member = http.request("GET", f"/guilds/{state.guild_id}/members/{me['id']}")
    roles = [state.roles[r] for r in member["roles"] if r in state.roles] + [state.roles[state.guild_id]]
    perms = 0
    for role in roles:
        perms |= int(role["permissions"])
    if perms & ADMINISTRATOR:
        run.can_mention_all = True
        return
    needed = {"Manage Channels": MANAGE_CHANNELS, "Manage Roles": MANAGE_ROLES}
    if onboarding:
        needed["Manage Server"] = MANAGE_GUILD
    missing = [name for name, bit in needed.items() if not perms & bit]
    if missing:
        run.warn(f"The bot is missing {', '.join(missing)}; --apply will fail until its role has them.")
    run.can_mention_all = bool(perms & MENTION_EVERYONE)
    top = max((r["position"] for r in roles), default=0)
    for old_name in ROLE_RENAMES:
        role = state.role_named(old_name)
        if role and role["position"] >= top:
            run.warn(f"The bot's highest role is below '{old_name}', so it can't rename it.")


# ---- Onboarding ----

def snowflakes():
    """New IDs for onboarding prompts and options, shaped like Discord's (time << 22 | counter)."""
    counter = 0
    while True:
        counter += 1
        ms = int(time.time() * 1000) - 1420070400000
        yield str((ms << 22) | (counter & 0x3FFFFF))


def to_put_option(option):
    """An option as PUT takes it: GET returns an emoji object, PUT wants emoji_id/emoji_name/emoji_animated."""
    result = {k: option[k] for k in ("id", "title", "description", "channel_ids", "role_ids") if k in option}
    emoji = option.get("emoji")
    if "emoji_name" in option or "emoji_id" in option:
        for key in ("emoji_id", "emoji_name", "emoji_animated"):
            if option.get(key) is not None:
                result[key] = option[key]
    elif emoji and (emoji.get("id") or emoji.get("name")):
        if emoji.get("id"):
            result["emoji_id"] = emoji["id"]
        if emoji.get("name"):
            result["emoji_name"] = emoji["name"]
        result["emoji_animated"] = bool(emoji.get("animated"))
    return result


def to_put_prompt(prompt):
    result = {k: prompt[k] for k in ("id", "type", "title", "single_select", "required", "in_onboarding")
              if k in prompt}
    result["options"] = [to_put_option(o) for o in prompt["options"]]
    return result


def onboarding_payload(onboarding, prompts):
    payload = {"prompts": prompts, "default_channel_ids": onboarding.get("default_channel_ids", []),
               "enabled": onboarding.get("enabled", False)}
    if "mode" in onboarding:
        payload["mode"] = onboarding["mode"]
    return payload


def is_country_prompt(prompt, country_role_ids):
    role_ids = [r for o in prompt["options"] for r in o.get("role_ids", [])]
    return bool(role_ids) and all(r in country_role_ids for r in role_ids)


def build_onboarding(state, onboarding, ids=None):
    """The new onboarding config: every question kept as it was except the country ones, which are
    replaced by five regional questions. Returns (payload, summary lines). Raises Abort."""
    ids = ids or snowflakes()
    countries = state.country_roles()
    country_role_ids = {r["id"] for r in countries.values()}
    categories = {r: state.category(norm(REGION_CATEGORY_NAMES[r])) for r in REGIONS}
    if not all(categories.values()):
        raise Abort("The regional categories don't exist yet: run --apply before --onboarding.")
    others = {r: find_child(state, categories[r]["id"], other_channel_name(r)) for r in REGIONS}
    if not all(others.values()):
        raise Abort("The other-<region> channels don't exist yet: run --apply before --onboarding.")

    prompts = onboarding.get("prompts", [])
    country_prompts = [p for p in prompts if is_country_prompt(p, country_role_ids)]
    old_options = {}  # role id -> option, from the old country questions
    for prompt in country_prompts:
        for option in prompt["options"]:
            if len(option.get("role_ids", [])) == 1:
                old_options.setdefault(option["role_ids"][0], option)
    existing_regional = {p["title"]: p for p in country_prompts if p["title"].startswith(PROMPT_TITLE_PREFIX)}
    template = next((p for p in country_prompts if not p["title"].startswith(PROMPT_TITLE_PREFIX)),
                    country_prompts[0] if country_prompts else {})

    summary = []
    regional_prompts = []
    for region in REGIONS:
        title = PROMPT_TITLE_PREFIX + region
        existing = existing_regional.get(title, {})
        existing_by_title = {o["title"]: o for o in existing.get("options", [])}
        options = []
        no_channel = []
        for country in sorted(c for c in countries if region_of(c) == region):
            role = countries[country]
            channel = find_child(state, categories[region]["id"], slug(country))
            if not channel:
                no_channel.append(country)
            old = old_options.get(role["id"], {})
            option = {
                "id": old.get("id") or next(ids),
                "title": country,
                "description": old.get("description"),
                "channel_ids": [(channel or others[region])["id"]],
                "role_ids": [role["id"]],
            }
            emoji = to_put_option(old) if old else {}
            if "emoji_name" in emoji or "emoji_id" in emoji:
                option.update({k: v for k, v in emoji.items() if k.startswith("emoji_")})
            elif country_iso(country):
                option.update(emoji_name=flag(country_iso(country)), emoji_animated=False)
            options.append(option)
        somewhere = SOMEWHERE_ELSE.format(region=region)
        options.append({
            "id": existing_by_title.get(somewhere, {}).get("id") or next(ids),
            "title": somewhere, "description": None, "channel_ids": [others[region]["id"]], "role_ids": [],
            "emoji_name": REGION_EMOJI[region], "emoji_animated": False,
        })
        if len(options) > MAX_OPTIONS_PER_PROMPT:
            raise Abort(f"'{title}' would have {len(options)} options, over {MAX_OPTIONS_PER_PROMPT}.")
        regional_prompts.append({
            "id": existing.get("id") or next(ids),
            "type": template.get("type", 0),
            "title": title,
            "single_select": False,
            "required": False,
            "in_onboarding": template.get("in_onboarding", True),
            "options": options,
        })
        summary.append(f"  Question '{title}': {len(options)} options"
                       + (f" (role only, pointing to #{other_channel_name(region)}: {', '.join(no_channel)})"
                          if no_channel else ""))

    first = next((i for i, p in enumerate(prompts) if p in country_prompts), len(prompts))
    kept_before = [to_put_prompt(p) for p in prompts[:first] if p not in country_prompts]
    kept_after = [to_put_prompt(p) for p in prompts[first:] if p not in country_prompts]
    new_prompts = kept_before + regional_prompts + kept_after
    if len(new_prompts) > MAX_PROMPTS:
        raise Abort(f"Onboarding would have {len(new_prompts)} questions, over {MAX_PROMPTS}.")

    lines = [f"  Remove question '{p['title']}' ({len(p['options'])} options)" for p in country_prompts
             if p["title"] not in existing_regional]
    lines += [f"  Rebuild question '{p['title']}' (from an earlier run)" for p in country_prompts
              if p["title"] in existing_regional]
    lines += summary
    lines += [f"  Keep question '{p['title']}' unchanged" for p in kept_before + kept_after]
    lines.append(f"  Keep {len(onboarding.get('default_channel_ids', []))} default channels, "
                 f"enabled={onboarding.get('enabled')}, mode={onboarding.get('mode')}")
    if not country_prompts:
        lines.insert(0, "  WARNING: no existing country questions found; the regional ones are added at the end")
    return onboarding_payload(onboarding, new_prompts), lines


# ---- Backups and rollback ----

def backup_data(http, state, onboarding):
    return {
        "saved_at": datetime.datetime.now(datetime.timezone.utc).isoformat(),
        "guild_id": state.guild_id,
        "roles": [{"id": r["id"], "name": r["name"], "position": r["position"]} for r in state.roles.values()],
        "channels": list(state.channels.values()),
        "onboarding": onboarding,
    }


def save_backup(path, data, out=print):
    """Writes the backup. An existing file is the layout from before the first run, so it's kept and
    this one goes next to it with the time in its name. Returns the original's channel IDs."""
    path = Path(path)
    if path.exists():
        original = json.loads(path.read_text())
        stamped = path.with_name(f"{path.stem}-{datetime.datetime.now():%Y%m%d-%H%M%S}{path.suffix}")
        stamped.write_text(json.dumps(data, indent=2, ensure_ascii=False))
        out(f"Saved the current layout and onboarding to {stamped} ({path} kept: it's from before the first run)")
        return {c["id"] for c in original["channels"]}
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False))
    out(f"Saved the current layout and onboarding to {path}")
    return {c["id"] for c in data["channels"]}


def restore_layout(run, backup):
    """Puts the old country categories back and moves their channels back with their old overwrites.
    Leaves the new channels and regional categories in place (delete those by hand if wanted)."""
    state, g = run.state, run.state.guild_id
    old_categories = [c for c in backup["channels"] if c["type"] == CATEGORY and norm(c["name"]) in OLD_CATEGORY_KEYS]
    for old in sorted(old_categories, key=lambda c: c["position"]):
        category = state.channels.get(old["id"]) or state.category(norm(old["name"]))
        if not category:
            created = run.change(f"Recreate category {old['name']}", "POST", f"/guilds/{g}/channels",
                                 {"name": old["name"], "type": CATEGORY,
                                  "permission_overwrites": clean_overwrites(old["permission_overwrites"])})
            category = created or dict(old, id=run.placeholder_id())
            state.channels[category["id"]] = category
        children = sorted((c for c in backup["channels"] if c.get("parent_id") == old["id"]),
                          key=lambda c: c["position"])
        for i, child in enumerate(children):
            channel = state.channels.get(child["id"])
            if not channel:
                run.warn(f"#{child['name']} no longer exists; skipped.")
                continue
            if channel.get("parent_id") != category["id"]:
                run.change(f"Move #{child['name']} back into {old['name']}", "PATCH", f"/guilds/{g}/channels",
                           [{"id": channel["id"], "parent_id": category["id"], "lock_permissions": False}])
                channel["parent_id"] = category["id"]
            if overwrites_key(channel["permission_overwrites"]) != overwrites_key(child["permission_overwrites"]):
                overwrites = clean_overwrites(child["permission_overwrites"])
                run.change(f"Restore #{child['name']}'s own permissions", "PATCH", f"/channels/{channel['id']}",
                           {"permission_overwrites": overwrites})
                channel["permission_overwrites"] = overwrites
        current = state.children(category["id"])
        rank = {c["id"]: i for i, c in enumerate(children)}
        desired = sorted(current, key=lambda c: (rank.get(c["id"], len(rank)), c["name"]))
        if any(c["position"] != i for i, c in enumerate(desired)):
            run.change(f"Restore the order of {old['name']}", "PATCH", f"/guilds/{g}/channels",
                       [{"id": c["id"], "position": i} for i, c in enumerate(desired)])
            for i, c in enumerate(desired):
                c["position"] = i
    for old_name, new_name in ROLE_RENAMES.items():
        role = state.role_named(new_name)
        if role and any(r["id"] == role["id"] and r["name"] == old_name for r in backup["roles"]):
            run.out(f"  (role '{new_name}' keeps its corrected name)")


# ---- Command line ----

def summarise(run, plan=None, state=None):
    lines = ["", "=" * 60, "Summary" + ("" if run.apply else " (dry run: nothing was changed)")]
    created = [c for c in run.changes if c.startswith("Create #")]
    for label, prefix in [("Roles renamed", "Rename role"), ("Categories created", "Create category"),
                          ("Channels created", "Create #"), ("Channels moved", "Move #"),
                          ("Channels synced", "Sync #"), ("Categories sorted", "Sort the"),
                          ("Messages posted", "Post "), ("Channels locked", "Lock #"),
                          ("Categories deleted", "Delete the empty"), ("Onboarding replaced", "Replace onboarding"),
                          ("Onboarding restored", "Put onboarding back"), ("Categories recreated", "Recreate"),
                          ("Permissions restored", "Restore #")]:
        count = sum(1 for c in run.changes if c.startswith(prefix))
        if count:
            lines.append(f"  {label}: {count}")
    if not run.changes:
        lines.append("  No changes needed: the server already matches.")
    if plan is not None and state is not None:
        new_country = {c.split()[1][1:] for c in created} - {other_channel_name(r) for r in REGIONS}
        if new_country:
            lines.append(f"  New country channels: {', '.join(sorted(new_country))}")
        if not run.apply and new_country != EXPECTED_NEW_CHANNELS and plan.old:
            extra, missing = new_country - EXPECTED_NEW_CHANNELS, EXPECTED_NEW_CHANNELS - new_country
            if extra:
                lines.append(f"  Not in the 21 Nov list, created anyway: {', '.join(sorted(extra))}")
            if missing:
                lines.append(f"  In the 21 Nov list but not created now: {', '.join(sorted(missing))}")
    for warning in run.warnings:
        lines.append(f"  WARNING: {warning}")
    run.out("\n".join(lines))


def main(argv=None, http=None, out=print, env=os.environ):
    parser = argparse.ArgumentParser(description="Restructure country channels into regional categories.")
    parser.add_argument("--apply", action="store_true", help="make the changes (default: dry run)")
    parser.add_argument("--onboarding", action="store_true", help="build the new onboarding config")
    parser.add_argument("--onboarding-file", default="onboarding_new.json")
    parser.add_argument("--apply-onboarding", action="store_true", help="with --onboarding: send it to Discord")
    parser.add_argument("--restore-layout", metavar="BACKUP", help="move channels back as in BACKUP")
    parser.add_argument("--restore-onboarding", metavar="BACKUP", help="put onboarding back as in BACKUP")
    parser.add_argument("--backup", default="country_regions_backup.json")
    parser.add_argument("--guild", default=PAUSEAI_GUILD_ID)
    args = parser.parse_args(argv)

    if http is None:
        token = env.get("DISCORD_TOKEN")
        if not token:
            out("Set DISCORD_TOKEN to the bot token first.")
            return 2
        http = DiscordHTTP(token)

    try:
        state = load_state(http, args.guild)
        if args.restore_onboarding:
            backup = json.loads(Path(args.restore_onboarding).read_text())
            run = Run(http, state, args.apply, out)
            payload = onboarding_payload(backup["onboarding"],
                                         [to_put_prompt(p) for p in backup["onboarding"].get("prompts", [])])
            run.change(f"Put onboarding back as saved at {backup['saved_at']} "
                       f"({len(payload['prompts'])} questions)", "PUT", f"/guilds/{args.guild}/onboarding", payload)
            summarise(run)
            return 0
        if args.restore_layout:
            backup = json.loads(Path(args.restore_layout).read_text())
            run = Run(http, state, args.apply, out)
            restore_layout(run, backup)
            summarise(run)
            return 0
        if args.onboarding:
            return run_onboarding(http, state, args, out)

        run = Run(http, state, args.apply, out)
        check_bot(http, state, run)
        onboarding = http.request("GET", f"/guilds/{args.guild}/onboarding")
        plan = plan_restructure(state, out)
        out(("Applying" if args.apply else "Dry run: these are the changes --apply would make") + ":")
        if args.apply:
            original_ids = save_backup(args.backup, backup_data(http, state, onboarding), out)
        elif Path(args.backup).exists():
            original_ids = {c["id"] for c in json.loads(Path(args.backup).read_text())["channels"]}
        else:
            original_ids = set(state.channels)
        restructure(run, plan, original_ids)
        summarise(run, plan, state)
        return 0
    except Abort as error:
        out(f"Stopped: {error}")
        return 1
    except DiscordError as error:
        out(f"Stopped: {error}\nEverything above this line was done. Fix the cause and run it again: "
            "finished steps are skipped.")
        return 1


def run_onboarding(http, state, args, out):
    run = Run(http, state, args.apply_onboarding, out)
    check_bot(http, state, run, onboarding=True)
    onboarding = http.request("GET", f"/guilds/{args.guild}/onboarding")
    payload, lines = build_onboarding(state, onboarding)
    Path(args.onboarding_file).write_text(json.dumps(payload, indent=2, ensure_ascii=False))
    out(f"Wrote the new onboarding config to {args.onboarding_file}. Changes from the current one:")
    out("\n".join(lines))
    if not args.apply_onboarding:
        out("\nRead it, then run again with --apply-onboarding to send it to Discord.")
        return 0
    save_backup(args.backup, backup_data(http, state, onboarding), out)
    result = run.change(f"Replace onboarding ({len(payload['prompts'])} questions)", "PUT",
                        f"/guilds/{args.guild}/onboarding", payload)
    out(f"Done: Discord now has {len(result.get('prompts', []))} onboarding questions.")
    summarise(run)
    return 0


if __name__ == "__main__":
    sys.exit(main())
