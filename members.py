"""Helpers for the discord_members records."""
import datetime


def roles_to_restore(guild, saved_role_ids, saved_role_names):
    """The guild's roles matching a member's saved roles: by id, then by name for a role that was
    deleted and recreated. The caller still checks each one is safe to grant."""
    roles = []
    for role_id in saved_role_ids:
        role = guild.get_role(int(role_id))
        if role and role not in roles:
            roles.append(role)
    for name in saved_role_names:
        if not any(r.name == name for r in roles):
            role = next((r for r in guild.roles if r.name == name), None)
            if role and role not in roles:
                roles.append(role)
    return roles


def _list(value):
    """A list from Airtable, which sends these fields as a list or as one comma-separated string."""
    if isinstance(value, str):
        return [v.strip() for v in value.split(",") if v.strip()]
    return [str(v).strip() for v in value or [] if str(v).strip()]


def _time(value):
    return datetime.datetime.fromisoformat(value.replace("Z", "+00:00")) if value else None


def from_airtable(fields):
    """The save fields of a record from Airtable's Discord table, or None if it has no Discord ID."""
    member_id = str(fields.get("id") or "").strip()
    if not member_id.isdigit():
        return None
    return {
        "id": int(member_id),
        "username": fields.get("username") or "",
        "nick": fields.get("nick") or None,
        "global_name": fields.get("global_name") or None,
        "joined_at": _time(fields.get("joined_at")),
        "left_at": _time(fields.get("left_at")),
        "role_ids": [int(r) for r in _list(fields.get("role_ids")) if r.isdigit()],
        "role_names": _list(fields.get("role_names")),
        "onboarding_notification": bool(fields.get("onboarding_notification")),
        "discord_reaction": bool(fields.get("discord_reaction")),
    }
