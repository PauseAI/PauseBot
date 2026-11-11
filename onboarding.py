"""Works out who to ping in #onboarding-pipeline when a member gets a country role."""

NATIONAL_LEADER_ROLE_NAME = "National Leader"


def find_national_leader_role(guild):
    """The guild's National Leader role, looked up by name so test servers work too."""
    for role in guild.roles:
        if role.name == NATIONAL_LEADER_ROLE_NAME:
            return role
    return None


def resolve_onboarders(static_ids, country_role, leader_role, joining_member_id):
    """User IDs to ping for a member who just got country_role.

    A country's static onboarders list wins when it's non-empty. Otherwise it's everyone
    holding both the country role and the National Leader role. The joining member is
    never included, and each ID appears once, in order.
    """
    if static_ids:
        candidates = list(static_ids)
    elif leader_role is None:
        candidates = []
    else:
        leader_ids = {m.id for m in leader_role.members}
        candidates = [m.id for m in country_role.members if m.id in leader_ids]

    seen = set()
    result = []
    for uid in candidates:
        if uid != joining_member_id and uid not in seen:
            seen.add(uid)
            result.append(uid)
    return result


def onboarding_message(onboarder_ids, member_mention, country_name):
    if onboarder_ids:
        mentions = " ".join(f"<@{uid}>" for uid in onboarder_ids)
        return f"{mentions} : {member_mention} has joined from {country_name}!"
    return (f"{member_mention} has joined from {country_name}, but there's no National Leader "
            f"for {country_name} to ping.")
