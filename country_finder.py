"""The country finder: lets members pick their country roles at any time, past onboarding's limits.

Discord onboarding fits only the countries that already have members, so everyone else picks
"Somewhere else in <region>". The finder lists every country in jobs/country_regions_table.py:

- In each other-<region> channel, where onboarding's "Somewhere else in <region>" sends people, a
  pinned message (/country_finder_post) with that region's countries in one or two dropdowns (Discord
  allows 25 options per dropdown). The message is shared, so picking there only adds: the reply,
  which only the member sees, is their own copy with their countries ticked.
- /country: privately, a button per region, each opening that member's own copy of the region's
  dropdowns. /country name:<country> gives one straight away.
- In a member's own copy, ticking a country gives its role and unticking takes it away. Members can
  hold more than one country, as with onboarding. Only roles named in the table are ever given or
  taken, and only those that pass is_role_safe_to_grant and sit below the bot's own role.

Every component is matched by its custom_id in on_interaction rather than by a stored View, so the
posted message and any open dropdowns keep working after the bot restarts.
"""
import collections
import math
import re
import time
import discord
from jobs.country_regions_table import REGIONS, COUNTRIES, ENDONYMS
from countries import flag

PREFIX = "country_finder"
MAX_OPTIONS = 25  # Discord's limit for one select menu
REGION_EMOJI = {"Europe": "🌍", "Americas": "🌎", "Africa": "🌍", "Middle East": "🌍", "Asia-Pacific": "🌏"}

# Each country added can ping its greeters and email its coordinator, so a member is held back after
# this many countries added or removed in this many seconds (a dropdown can tick up to 25 at once)
CHANGE_LIMIT = 10
CHANGE_WINDOW = 600

AUDIT_REASON = "Country finder: the member chose their countries"

POST_TEXT = ("**Not listed in onboarding?** Pick your country here to get its role and its channel. "
             "You can pick more than one. To change or remove yours later, use `/country`.")

INTRO = ("**Where are you?** Pick your country to get its role and its channel. Press your region, then "
         "tick your country. You can pick more than one, and change them any time.")


def slug(name):
    """A country's channel name, as jobs/country_regions.py makes it."""
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-")


def iso_of(country):
    for countries in COUNTRIES.values():
        if country in countries:
            return countries[country]
    return None


def region_pages(region):
    """The region's countries in alphabetical order, split evenly into pages of at most MAX_OPTIONS."""
    names = sorted(COUNTRIES[region])
    pages = max(1, math.ceil(len(names) / MAX_OPTIONS))
    size = math.ceil(len(names) / pages)
    return [names[i:i + size] for i in range(0, len(names), size)]


def page_label(region, page):
    """'Europe', or 'Europe A–L' when the region has more than one page."""
    pages = region_pages(region)
    if len(pages) == 1:
        return region
    names = pages[page]
    return f"{region} {names[0][0]}–{names[-1][0]}"


def all_countries():
    return {name for countries in COUNTRIES.values() for name in countries}


def country_roles(guild, is_safe):
    """{country name: role} for the table's countries whose role exists in the guild and that the bot
    may give: safe to grant and below the bot's own top role."""
    names = all_countries()
    return {role.name: role for role in guild.roles
            if role.name in names and is_safe(role) and role.is_assignable()}


def changes(held, page, chosen):
    """(to add, to remove) for a member holding the countries `held` who left `chosen` ticked in a
    dropdown listing `page`. Countries outside the page are never touched."""
    page, chosen = set(page), set(chosen) & set(page)
    return sorted(chosen - held), sorted((held & page) - chosen)


def region_buttons():
    view = discord.ui.View(timeout=None)
    for i, region in enumerate(REGIONS):
        view.add_item(discord.ui.Button(label=region, emoji=REGION_EMOJI.get(region),
                                        style=discord.ButtonStyle.primary, custom_id=f"{PREFIX}:region:{i}"))
    return unstored(view)


def region_view(region_index, roles, held, shared=False):
    """The dropdowns for one region, with the member's countries ticked. Countries without a role the
    bot can give are left out. shared: for the posted message everyone sees, so nothing is ticked and
    picking only adds."""
    region = REGIONS[region_index]
    kind = "add" if shared else "pick"
    view = discord.ui.View(timeout=None)
    for page, names in enumerate(region_pages(region)):
        options = []
        for name in names:
            if name not in roles:
                continue
            iso = iso_of(name)
            options.append(discord.SelectOption(
                label=name, value=name, description=ENDONYMS.get(name),
                emoji=flag(iso) if iso else None, default=not shared and name in held))
        if not options:
            continue
        view.add_item(discord.ui.Select(
            custom_id=f"{PREFIX}:{kind}:{region_index}:{page}",
            placeholder=f"{page_label(region, page)}: {'pick' if shared else 'tick'} your country",
            min_values=1 if shared else 0, max_values=len(options), options=options))
    return unstored(view)


def unstored(view):
    """A stopped view, so discord.py doesn't keep it to dispatch clicks to: on_interaction handles them."""
    view.stop()
    return view


def held_countries(member, roles):
    ids = {role.id for role in roles.values()}
    return {role.name for role in member.roles if role.id in ids}


def region_text(region_index, roles, status=""):
    region = REGIONS[region_index]
    text = f"**{REGION_EMOJI.get(region, '')} {region}**: tick every country you're in, untick one to leave it."
    if not any(name in roles for name in COUNTRIES[region]):
        text = f"**{region}**: there are no country roles for this region yet. Ask in the server for one."
    return f"{status}\n\n{text}" if status else text


def region_for_channel(name):
    """The region index whose other-<region> channel this is, else None."""
    return next((i for i, region in enumerate(REGIONS) if name == f"other-{slug(region)}"), None)


def channel_mention(guild, country):
    channel = discord.utils.get(guild.text_channels, name=slug(country))
    return channel.mention if channel else None


def status_text(guild, added, removed):
    lines = []
    for name in added:
        where = channel_mention(guild, name)
        lines.append(f"✅ You're now in **{name}**" + (f": say hello in {where}." if where else "."))
    for name in removed:
        lines.append(f"Removed **{name}**.")
    return "\n".join(lines) or "No change."


class ChangeLimiter:
    """Remembers when each member's countries were added or removed, in memory."""

    def __init__(self, limit=CHANGE_LIMIT, window=CHANGE_WINDOW, clock=time.monotonic):
        self.limit, self.window, self.clock = limit, window, clock
        self.times = collections.defaultdict(collections.deque)

    def allow(self, member_id, cost=1):
        """Whether `cost` more changes fit in the member's window; if so, counts them."""
        now = self.clock()
        times = self.times[member_id]
        while times and now - times[0] > self.window:
            times.popleft()
        if len(times) + cost > self.limit:
            if not times:
                del self.times[member_id]
            return False
        times.extend([now] * cost)
        return True


limiter = ChangeLimiter()


def parse(custom_id):
    """('region', index), ('pick', index, page) for a member's own copy, or ('add', index, page) for
    the shared post, from a finder custom_id; else None. The page isn't checked against today's split:
    a post made before a table change still names the page it was split into then."""
    parts = (custom_id or "").split(":")
    if not parts or parts[0] != PREFIX:
        return None
    try:
        if parts[1] == "region" and len(parts) == 3:
            index = int(parts[2])
            return ("region", index) if 0 <= index < len(REGIONS) else None
        if parts[1] in ("pick", "add") and len(parts) == 4:
            index, page = int(parts[2]), int(parts[3])
            if 0 <= index < len(REGIONS) and 0 <= page < 5:  # at most 5 dropdowns in a message
                return (parts[1], index, page)
    except (IndexError, ValueError):
        pass
    return None


async def handle_interaction(interaction, is_safe):
    """Answers a click on a finder button or dropdown. Returns whether it was one."""
    if interaction.type != discord.InteractionType.component:
        return False
    parsed = parse((interaction.data or {}).get("custom_id"))
    if not parsed:
        return False
    guild, member = interaction.guild, interaction.user
    if guild is None or getattr(member, "roles", None) is None:  # a User, not a Member: not in a server
        await interaction.response.send_message("This only works in the server.", ephemeral=True)
        return True
    roles = country_roles(guild, is_safe)
    held = held_countries(member, roles)

    if parsed[0] == "region":
        await interaction.response.send_message(
            region_text(parsed[1], roles), view=region_view(parsed[1], roles, held), ephemeral=True)
        return True

    kind, index, page = parsed
    region = REGIONS[index]
    values = [v for v in (interaction.data or {}).get("values", []) if v in COUNTRIES[region]]
    chosen = [v for v in values if v in roles]
    unavailable = [v for v in values if v not in roles]
    if kind == "add":
        # The shared post: an unticked country there says nothing about the member, so nothing is
        # removed. Answered by redrawing it, which clears the member's ticks there and picks up any
        # role made since it was posted; the member's own copy follows as a private message.
        add, remove = sorted(set(chosen) - held), []
        await interaction.response.edit_message(view=region_view(index, roles, set(), shared=True))
    else:
        # The countries this dropdown lists, as it was drawn: a copy opened before a table change
        # still means its own options
        add, remove = changes(held, dropdown_options(interaction, roles) or
                              [n for n in (region_pages(region) + [[]] * 5)[page] if n in roles], chosen)
        # Each role is its own API call, which can outlast Discord's 3 seconds for a first answer
        await interaction.response.defer()
    try:
        if not add and not remove:
            already = [n for n in chosen if n in held]
            status = ("You already have " + ", ".join(f"**{n}**" for n in already) + "."
                      if kind == "add" and already else "No change.")
        elif not limiter.allow(member.id, len(add) + len(remove)):
            status = (f"That's more changes than I can make right now (up to {CHANGE_LIMIT} countries every "
                      f"{CHANGE_WINDOW // 60} minutes). Try fewer, or try again in a few minutes.")
        else:
            added, removed, failed = await apply(member, roles, add, remove)
            held = (held | set(added)) - set(removed)
            status = status_text(guild, added, removed)
            if failed:
                status = (status + "\n" if added or removed else "") + (
                    f"Sorry, I couldn't change {', '.join(f'**{n}**' for n in failed)}. Try again, or ask a moderator.")
        if unavailable:
            status += "\n" + ", ".join(f"**{n}**" for n in unavailable) + " isn't available to pick right now. Ask a moderator."
        content, view = region_text(index, roles, status), region_view(index, roles, held)
        if kind == "add":
            await interaction.followup.send(content, view=view, ephemeral=True)
        else:
            await interaction.edit_original_response(content=content, view=view)
    except Exception:
        # Answered already, so without this the member would see nothing at all
        try:
            await interaction.followup.send("Sorry, something went wrong. Try again, or ask a moderator.",
                                            ephemeral=True)
        except discord.HTTPException:
            pass
        raise
    return True


def dropdown_options(interaction, roles):
    """The values of the dropdown that was used, read from the message it's on, else None."""
    custom_id = (interaction.data or {}).get("custom_id")
    message = getattr(interaction, "message", None)
    for row in getattr(message, "components", None) or []:
        for item in getattr(row, "children", []):
            if getattr(item, "custom_id", None) == custom_id and getattr(item, "options", None):
                return [o.value for o in item.options if o.value in roles]
    return None


async def post(interaction, is_safe, region_index=None):
    """/country_finder_post: in other-<region> (or for the region given), that region's dropdowns,
    pinned; anywhere else without a region, the region buttons. Answers the admin privately."""
    channel = interaction.channel
    if region_index is None:
        # In a thread, the channel it's in names the region
        named = getattr(channel, "parent", None) if isinstance(channel, discord.Thread) else channel
        region_index = region_for_channel(getattr(named, "name", None))
    await interaction.response.defer(ephemeral=True)
    if region_index is None:
        content, view = INTRO, region_buttons()
    else:
        roles = country_roles(interaction.guild, is_safe)
        view = region_view(region_index, roles, set(), shared=True)
        if not view.children:
            await interaction.followup.send(f"❌ No country roles I can give in {REGIONS[region_index]}.", ephemeral=True)
            return
        content = f"**{REGION_EMOJI.get(REGIONS[region_index], '')} {REGIONS[region_index]}**\n{POST_TEXT}"
    try:
        message = await channel.send(content, view=view)
    except discord.HTTPException as e:
        await interaction.followup.send(f"❌ Couldn't post here: {e.text or e}", ephemeral=True)
        return
    done = "✅ Posted. It keeps working after the bot restarts." + (
        "" if region_index is not None else " No region in this channel's name, so it's the region buttons.")
    if region_index is not None:
        try:
            await message.pin(reason="Country finder")
            done += " Pinned."
        except discord.HTTPException as e:
            done += (f" I couldn't pin it ({e.text or e}): give the PauseBot role Pin Messages here, or pin it"
                     " yourself.")
    await interaction.followup.send(done, ephemeral=True)


async def apply(member, roles, add, remove):
    """Adds and removes each country's role on its own, so a failure leaves the rest done and
    known. Returns (added, removed, failed) country names."""
    added, removed, failed = [], [], []
    for names, change, done in ((add, member.add_roles, added), (remove, member.remove_roles, removed)):
        for name in names:
            try:
                await change(roles[name], reason=AUDIT_REASON)
                done.append(name)
            except discord.HTTPException as e:
                print(f"Country finder: couldn't change {name} for {member.name}: {e}")
                failed.append(name)
    return added, removed, failed


async def add_country(interaction, is_safe, name):
    """/country <name>: gives that one country's role. Answers privately."""
    member, guild = interaction.user, interaction.guild
    roles = country_roles(guild, is_safe)
    match = next((n for n in roles if n.casefold() == (name or "").strip().casefold()), None)
    if not match:
        await interaction.response.send_message(
            f"I don't know a country called {discord.utils.escape_markdown(name or '')!s}. "
            "Pick your region instead:", view=region_buttons(), ephemeral=True)
        return
    if match in held_countries(member, roles):
        await interaction.response.send_message(f"You're already in **{match}**.", ephemeral=True)
        return
    if not limiter.allow(member.id):
        await interaction.response.send_message(
            "You've changed your countries a lot just now. Try again in a few minutes.", ephemeral=True)
        return
    await interaction.response.defer(ephemeral=True, thinking=True)
    added, _, _ = await apply(member, roles, [match], [])
    await interaction.followup.send(
        status_text(guild, added, []) if added else "Sorry, that didn't work. Try again, or ask a moderator.",
        ephemeral=True)


def autocomplete(roles, current, limit=25):
    """Country names the bot can give, matching what's typed: names starting with it first."""
    typed = (current or "").strip().casefold()
    names = sorted(roles)
    starts = [n for n in names if n.casefold().startswith(typed)]
    contains = [n for n in names if typed in n.casefold() and n not in starts]
    endonym = [n for n in names if typed and typed in ENDONYMS.get(n, "").casefold()
               and n not in starts and n not in contains]
    return (starts + contains + endonym)[:limit]
