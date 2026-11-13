"""Helpers for the discord_members records."""


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
