"""Works out who to ping in #onboarding-pipeline when a member gets a country role."""
import datetime

# When role routing switches on: Fri 13 Nov 2026, 17:00 UK time (UTC in November).
# Before it, a country's static "onboarders" list in COUNTRY_DATA wins whenever it's non-empty,
# and a country's "beforeRoleRouting" values replace its own (so France keeps its old onboarder
# and no Pause IA DM). From it, static lists are ignored: every country pings its Chapter Greeters,
# then its National Leaders, and "beforeRoleRouting" no longer applies.
# To postpone the switch, change this and redeploy.
ROLE_ROUTING_FROM = datetime.datetime(2026, 11, 13, 17, 0, tzinfo=datetime.UTC)

# Tried in this order: the first role with anyone in the country is the one pinged
CHAPTER_GREETER_ROLE_NAME = "Chapter Greeter"
NATIONAL_LEADER_ROLE_NAME = "National Leader"
TIER_ROLE_NAMES = (CHAPTER_GREETER_ROLE_NAME, NATIONAL_LEADER_ROLE_NAME)

# Granted (at CiviCRM's request) once someone has done /join: verified email, signed the
# Volunteer Agreement and linked Discord. Not "Wants to help", which is only an onboarding answer.
JOINED_ROLE_NAME = "Wants to volunteer"

# A member who joined the server longer ago than this and gains a country role is an existing
# member adding a country, not a new member joining. New members pick their countries in
# onboarding within minutes of joining (or of passing rules screening); a day leaves room for
# someone who joins and only gets round to it later that day, while anyone adding a country
# days or months later is clearly an existing member.
EXISTING_MEMBER_AFTER = datetime.timedelta(days=1)


def role_routing_active(now=None):
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    return now >= ROLE_ROUTING_FROM


def is_existing_member(joined_at, now=None):
    """Whether a member who joined the server at joined_at is an existing member rather than a
    new one (see EXISTING_MEMBER_AFTER). An unknown joined_at counts as new, as it always did."""
    if joined_at is None:
        return False
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    return now - joined_at > EXISTING_MEMBER_AFTER


def country_settings(info, now=None):
    """A COUNTRY_DATA entry as it applies at `now`: before the switch, its "beforeRoleRouting"
    values replace its own."""
    if role_routing_active(now):
        return info
    return {**info, **info.get("beforeRoleRouting", {})}


def _without(ids, joining_member_id):
    """IDs in order, once each, leaving out the joining member."""
    seen = set()
    result = []
    for uid in ids:
        if uid != joining_member_id and uid not in seen:
            seen.add(uid)
            result.append(uid)
    return result


def resolve_onboarders(static_ids, country_role, tier_roles, joining_member_id):
    """User IDs to ping for a member who just got country_role.

    A country's static onboarders list wins when anyone is left on it once the joining
    member is removed. Otherwise each role in tier_roles is tried in order (a None role, one
    that doesn't exist yet, is skipped), and the first one with members who also hold
    country_role is used. The joining member is never included, and each ID appears once,
    in order.
    """
    ids = _without(static_ids or (), joining_member_id)
    if ids:
        return ids
    for tier_role in tier_roles:
        if tier_role is None:
            continue
        tier_ids = {m.id for m in tier_role.members}
        ids = _without((m.id for m in country_role.members if m.id in tier_ids), joining_member_id)
        if ids:
            return ids
    return []


def _holds(role, member_id):
    return role is not None and any(m.id == member_id for m in role.members)


def onboarder_title(info, tier_roles, member_id, now=None):
    """What makes this member an onboarder for this country (COUNTRY_DATA entry), if anything:
    the name of the first tier role they hold ("Chapter Greeter", then "National Leader"), or
    "onboarder" if they're on its static list while static lists apply. None if neither.

    tier_roles are the roles named in TIER_ROLE_NAMES, in that order (None if missing).
    Holding the country role is assumed: this is asked about a member who has it.
    """
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    for name, tier_role in zip(TIER_ROLE_NAMES, tier_roles):
        if _holds(tier_role, member_id):
            return name
    if not role_routing_active(now) and member_id in country_settings(info, now).get("onboarders", []):
        return "onboarder"
    return None


def onboarding_ping(info, country_role, tier_roles, joining_member_id, member_mention, now=None,
                    added_tier_names=(), joined_at=None):
    """(message to post in #onboarding-pipeline, IDs it pings) for one country's COUNTRY_DATA entry.

    A country with "noPing" set posts nothing at all, not even the "nobody to ping" message,
    so the message is None. Static onboarders lists only count before ROLE_ROUTING_FROM.

    A member who is themselves an onboarder for the country (see onboarder_title) isn't a new
    member joining: the post just notes they picked up the role, and pings nobody. If only tier
    roles in added_tier_names (gained in the same update, and announced by tier_role_message)
    make them an onboarder, nothing is posted here.

    Otherwise the country's onboarders are pinged, with the same routing for a new member and
    for an existing one (see is_existing_member, from joined_at) adding a country: only the
    wording differs.
    """
    if now is None:
        # Read the clock once, so every check agrees right at the switch
        now = datetime.datetime.now(datetime.UTC)
    routing = role_routing_active(now)
    settings = country_settings(info, now)
    if settings.get("noPing"):
        return None, []
    # What made them an onboarder before this update: tier roles it added don't count
    held_before = [None if name in added_tier_names else r for name, r in zip(TIER_ROLE_NAMES, tier_roles)]
    title = onboarder_title(info, held_before, joining_member_id, now)
    if not title and onboarder_title(info, tier_roles, joining_member_id, now):
        return None, []
    if title:
        return picked_up_message(member_mention, settings["name"], title), []
    static_ids = None if routing else settings.get("onboarders")
    ids = resolve_onboarders(static_ids, country_role, tier_roles, joining_member_id)
    if is_existing_member(joined_at, now):
        return added_country_message(ids, member_mention, settings["name"], joined_at), ids
    return onboarding_message(ids, member_mention, settings["name"]), ids


def added_country_message(onboarder_ids, member_mention, country_name, joined_at):
    """The post for an existing member (one who joined at joined_at) adding a country."""
    member = f"{member_mention} (member since {joined_at.strftime('%b %Y')}) added {country_name}"
    if onboarder_ids:
        mentions = " ".join(f"<@{uid}>" for uid in onboarder_ids)
        return f"{mentions} : {member}"
    return f"{member}, but there's no Chapter Greeter or National Leader for {country_name} to ping."


def picked_up_message(member_mention, country_name, title):
    return f"{member_mention} ({country_name} {title}) picked up the {country_name} role"


def tier_role_message(added_tier_names, country_infos, member_mention, now=None):
    """The post for a member who just gained tier roles (names from TIER_ROLE_NAMES), given the
    COUNTRY_DATA entries for every country role they hold: one line per tier role, naming those
    countries. noPing countries are left out, and None means nothing to post (no tier role
    gained, or no country left to name)."""
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    names = [s["name"] for s in (country_settings(info, now) for info in country_infos) if not s.get("noPing")]
    if not names:
        return None
    if len(names) > 1:
        countries = ", ".join(names[:-1]) + " and " + names[-1]
    else:
        countries = names[0]
    lines = [f"{member_mention} has become a {tier} for {countries}"
             for tier in TIER_ROLE_NAMES if tier in added_tier_names]
    return "\n".join(lines) or None


def onboarding_message(onboarder_ids, member_mention, country_name):
    if onboarder_ids:
        mentions = " ".join(f"<@{uid}>" for uid in onboarder_ids)
        return f"{mentions} : {member_mention} has joined from {country_name}!"
    return (f"{member_mention} has joined from {country_name}, but there's no Chapter Greeter "
            f"or National Leader for {country_name} to ping.")


# Discord rejects messages longer than this
DISCORD_MESSAGE_LIMIT = 2000


def country_dm_prefix(info):
    return f"Welcome to PauseAI in {info['name']} {info.get('flag', '')}".rstrip() + ":"


def has_joined(member):
    """Whether the member holds the JOINED_ROLE_NAME role, looked up by name in their guild.
    False if the guild has no such role."""
    joined_role = next((r for r in member.guild.roles if r.name == JOINED_ROLE_NAME), None)
    return joined_role is not None and any(r.id == joined_role.id for r in member.roles)


def welcome_dms(country_infos, general_msg, user, now=None, skip_paragraph=None):
    """The welcome DMs to send a member who picked these countries (COUNTRY_DATA entries), in order.

    Always the general welcome first, then one DM per picked country that has its own "msg",
    each headed with that country's name and flag. skip_paragraph, if given, is left out of the
    general welcome along with the blank line after it.
    """
    if skip_paragraph:
        general_msg = general_msg.replace(skip_paragraph + "\n \n", "", 1)
    return [general_msg.format(user=user), *country_dms(country_infos, user, now)]


def country_dms(country_infos, user, now=None):
    """One DM per country (COUNTRY_DATA entry) that has its own "msg", in order, each headed with
    that country's name and flag. On its own, this is what an existing member adding a country
    is sent: no general welcome."""
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    settings = [country_settings(info, now) for info in country_infos]
    return [f"{country_dm_prefix(s)}\n\n{s['msg'].format(user=user)}" for s in settings if s.get("msg")]


def added_country_dms(country_infos, tier_roles, member_id, user, joined_at, now=None):
    """The DMs for a member who just gained these country roles (COUNTRY_DATA entries) outside
    the delayed welcome: only an existing member (see is_existing_member) gets any, and then only
    the countries' own DMs (see country_dms), leaving out countries they're an onboarder for.
    New members get theirs from the delayed welcome instead."""
    if now is None:
        now = datetime.datetime.now(datetime.UTC)
    if not is_existing_member(joined_at, now):
        return []
    infos = [info for info in country_infos if not onboarder_title(info, tier_roles, member_id, now)]
    return country_dms(infos, user, now)
