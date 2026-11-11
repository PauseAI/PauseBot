"""Works out who to ping in #onboarding-pipeline when a member gets a country role."""

# Tried in this order: the first role with anyone in the country is the one pinged
CHAPTER_GREETER_ROLE_NAME = "Chapter Greeter"
NATIONAL_LEADER_ROLE_NAME = "National Leader"
TIER_ROLE_NAMES = (CHAPTER_GREETER_ROLE_NAME, NATIONAL_LEADER_ROLE_NAME)


def find_role_by_name(guild, name):
    """Looked up by name so test servers with their own roles work too."""
    for role in guild.roles:
        if role.name == name:
            return role
    return None


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

    A country's static onboarders list wins when it's non-empty. Otherwise each role in
    tier_roles is tried in order (a None role, one that doesn't exist yet, is skipped), and
    the first one with members who also hold country_role is used. The joining member is
    never included, and each ID appears once, in order.
    """
    if static_ids:
        return _without(static_ids, joining_member_id)
    for tier_role in tier_roles:
        if tier_role is None:
            continue
        tier_ids = {m.id for m in tier_role.members}
        ids = _without((m.id for m in country_role.members if m.id in tier_ids), joining_member_id)
        if ids:
            return ids
    return []


def onboarding_ping(info, country_role, tier_roles, joining_member_id, member_mention):
    """(message to post in #onboarding-pipeline, IDs it pings) for one country's COUNTRY_DATA entry.

    A country with "noPing" set posts nothing at all, not even the "nobody to ping" message,
    so the message is None.
    """
    if info.get("noPing"):
        return None, []
    ids = resolve_onboarders(info.get("onboarders"), country_role, tier_roles, joining_member_id)
    return onboarding_message(ids, member_mention, info["name"]), ids


def onboarding_message(onboarder_ids, member_mention, country_name):
    if onboarder_ids:
        mentions = " ".join(f"<@{uid}>" for uid in onboarder_ids)
        return f"{mentions} : {member_mention} has joined from {country_name}!"
    return (f"{member_mention} has joined from {country_name}, but there's no Chapter Greeter "
            f"or National Leader for {country_name} to ping.")
