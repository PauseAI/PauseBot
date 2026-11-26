"""Which roles are country roles, and what onboarding says about each.

Any role whose name is a country in jobs/country_regions_table.py is a country role, so a new
chapter's role gets onboarding (the #onboarding-pipeline ping and the welcome DMs) as soon as it
exists, with no code change. Roles not in that table (e.g. 'California') are ignored. Matching is
by exact name, so renaming a country role away from its table name turns its onboarding off, and
a non-country role given a country's name (a US state called 'Georgia') would count as that country.
"""
from jobs.country_regions_table import COUNTRIES

FRANCE_DM = """Bienvenue {user} ! PauseAI France est animé par Pause IA, qui a son propre serveur Discord : https://discord.gg/vyXGd7AeGc
C'est là-bas que ça se passe pour agir en France. Tu peux bien sûr rester sur ce serveur aussi !

Welcome {user}! PauseAI France is run by Pause IA, which has its own Discord server: https://discord.gg/vyXGd7AeGc
That's the place to get involved in France. You're still very welcome here too!"""

# Only the exceptions, keyed by role name. Anything left out comes from the role name and its ISO code:
# - "name": what posts and DMs call the country, when not the role name
# - "msg": the country's own welcome DM, sent after the general one ({user} is the member's name)
# - "noPing": post nothing in #onboarding-pipeline for it
# - "email": a chapter address that MailerSend emails when someone gets the role
# - "flag": the flag emoji, when the ISO code has none ("" for no flag)
OVERRIDES = {
    "United Kingdom": {"name": "UK"},
    "United States": {"name": "US"},
    # France is run by Pause IA on its own Discord, so new members are pointed there instead of pinging anyone
    "France": {"msg": FRANCE_DM, "noPing": True},
    "Germany": {"email": "germany@pauseai.info"},
    "Canada": {"email": "canada@pauseai.info"},
    "India": {"email": "india@pauseai.info"},
    # XK is a user-assigned code, so there's no flag emoji for it
    "Kosovo": {"flag": ""},
}

ISO_CODES = {name: iso for countries in COUNTRIES.values() for name, iso in countries.items()}


def flag(iso):
    """The flag emoji for an ISO 3166 alpha-2 code ('GB' gives 🇬🇧)."""
    return "".join(chr(0x1F1E6 + ord(c) - ord("A")) for c in iso)


def info_for_name(role_name):
    """What onboarding needs for a country role with this name ({"name", "flag", "msg", and "noPing"
    and "email" if set}), or None if the name isn't a country."""
    iso = ISO_CODES.get(role_name)
    if iso is None:
        return None
    return {"name": role_name, "flag": flag(iso), "msg": "", **OVERRIDES.get(role_name, {})}


def info(role):
    """info_for_name for a role, or None if it isn't a country role."""
    return info_for_name(role.name)


def infos(roles):
    """(role, info) for each country role among roles, in order. Only the first role with a given
    name counts, so a duplicate role never doubles a ping, email or DM."""
    seen, result = set(), []
    for role in roles:
        i = info(role)
        if i is not None and role.name not in seen:
            seen.add(role.name)
            result.append((role, i))
    return result
