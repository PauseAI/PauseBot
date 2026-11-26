import datetime
import itertools
from types import SimpleNamespace as NS
import pytest
import countries
import main

# What the hard-coded COUNTRY_DATA said for each country before countries became automatic, keyed by
# the role's name in the server. Posts and DMs must call each one the same, with the same flag.
# Czechia is the exception: COUNTRY_DATA said "Czech", but the role and the country finder say Czechia.
BEFORE = {
    "United Kingdom": ("UK", "🇬🇧"), "United States": ("US", "🇺🇸"), "France": ("France", "🇫🇷"),
    "Germany": ("Germany", "🇩🇪"), "Netherlands": ("Netherlands", "🇳🇱"), "Italy": ("Italy", "🇮🇹"),
    "Spain": ("Spain", "🇪🇸"), "Canada": ("Canada", "🇨🇦"), "Nigeria": ("Nigeria", "🇳🇬"),
    "Romania": ("Romania", "🇷🇴"), "Australia": ("Australia", "🇦🇺"), "Poland": ("Poland", "🇵🇱"),
    "Serbia": ("Serbia", "🇷🇸"), "Sweden": ("Sweden", "🇸🇪"), "Czechia": ("Czechia", "🇨🇿"),
    "Kenya": ("Kenya", "🇰🇪"), "Israel": ("Israel", "🇮🇱"), "South Korea": ("South Korea", "🇰🇷"),
    "India": ("India", "🇮🇳"), "Japan": ("Japan", "🇯🇵"), "Philippines": ("Philippines", "🇵🇭"),
}


@pytest.mark.parametrize("role_name", BEFORE)
def test_previously_listed_countries_keep_their_name_and_flag(role_name):
    info = countries.info_for_name(role_name)
    assert (info["name"], info["flag"]) == BEFORE[role_name]
    # Only France has its own DM or skips the ping
    assert bool(info["msg"]) == info.get("noPing", False) == (role_name == "France")


def test_france_keeps_its_own_dm_and_no_ping():
    france = countries.info_for_name("France")
    assert france == {"name": "France", "flag": "🇫🇷", "msg": countries.FRANCE_DM, "noPing": True}


@pytest.mark.parametrize("name", ["California", "Chapter Greeter", "National Leader", "Wants to volunteer",
                                  "UK", "Czech", "france", ""])
def test_roles_not_named_after_a_country_in_the_table_are_ignored(name):
    assert countries.info_for_name(name) is None


def test_every_override_is_for_a_country_in_the_table():
    assert set(countries.OVERRIDES) <= set(countries.ISO_CODES)


def test_every_country_in_the_table_has_a_two_letter_flag():
    for name, iso in countries.ISO_CODES.items():
        flag = countries.info_for_name(name)["flag"]
        assert len(flag) == 2 and all(0x1F1E6 <= ord(c) <= 0x1F1FF for c in flag), name


# ── on_member_update, end to end ──────────────────────────────────────────────

_ids = itertools.count(1000)
NOW = datetime.datetime.now(datetime.UTC)


class Role:
    def __init__(self, name, members=()):
        self.id, self.name, self.members = next(_ids), name, list(members)


class Member:
    def __init__(self, guild, roles, joined_at):
        self.id, self.name, self.mention = 99, "pat", "<@99>"
        self.guild, self.roles, self.joined_at, self.pending = guild, list(roles), joined_at, False
        self.dms = []

    async def send(self, content):
        self.dms.append(content)


class Channel:
    def __init__(self):
        self.posts = []

    async def send(self, content):
        self.posts.append(content)


class Accepted:
    status = 202

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False


class RecordingHTTP:
    """Records outgoing HTTP posts, such as the MailerSend email, and accepts them."""
    def __init__(self):
        self.posts = []

    def post(self, url, **kwargs):
        self.posts.append((url, kwargs))
        return Accepted()


@pytest.fixture
def bot(monkeypatch):
    channel, synced, http = Channel(), [], RecordingHTTP()

    async def sync(member, onboarding_notification=False, **kwargs):
        synced.append(onboarding_notification)

    monkeypatch.setattr(main, "DEVELOPER_MODE", False)
    monkeypatch.setattr(main.bot, "get_channel", lambda channel_id: channel)
    monkeypatch.setattr(main, "http_session", http)
    monkeypatch.setattr(main, "sync_member_record", sync)
    return NS(channel=channel, synced=synced, http=http)


async def gain(role_name, greeters=(), joined_at=None):
    """A member gaining the role named role_name; greeters are member IDs holding it and Chapter Greeter."""
    country = Role(role_name, [NS(id=i) for i in (*greeters, 99)])
    greeter = Role("Chapter Greeter", [NS(id=i) for i in greeters])
    guild = NS(id=main.PAUSEAI_SERVER_ID, roles=[country, greeter])
    before = Member(guild, [], joined_at)
    after = Member(guild, [country], joined_at)
    await main.on_member_update(before, after)
    return after


async def test_a_country_that_was_never_hard_coded_gets_onboarding(bot):
    await gain("Portugal", greeters=[7])
    assert bot.channel.posts == ["<@7> : <@99> has joined from Portugal!"]
    assert bot.synced == [True]


async def test_a_country_with_nobody_to_ping_says_so(bot):
    await gain("Portugal")
    assert bot.channel.posts == ["<@99> has joined from Portugal, but there's no Chapter Greeter "
                                 "or National Leader for Portugal to ping."]
    assert bot.synced == [False]


async def test_a_role_not_named_after_a_country_gets_nothing(bot):
    member = await gain("California", greeters=[7])
    assert bot.channel.posts == [] and bot.synced == [] and member.dms == []


async def test_the_uk_is_still_called_uk(bot):
    await gain("United Kingdom", greeters=[7])
    assert bot.channel.posts == ["<@7> : <@99> has joined from UK!"]


async def test_france_posts_nothing_and_an_existing_member_gets_its_dm(bot):
    member = await gain("France", greeters=[7], joined_at=NOW - datetime.timedelta(days=30))
    assert bot.channel.posts == []
    dm, = member.dms
    assert dm.startswith("Welcome to PauseAI in France 🇫🇷:\n\nBienvenue pat")


EMAILS = {"Germany": "germany@pauseai.info", "Canada": "canada@pauseai.info", "India": "india@pauseai.info"}


def test_only_germany_canada_and_india_have_a_chapter_email():
    assert {name: info["email"] for name in countries.ISO_CODES
            if (info := countries.info_for_name(name)).get("email")} == EMAILS


@pytest.mark.parametrize("name", EMAILS)
async def test_germany_canada_and_india_still_email_their_chapter(bot, monkeypatch, name):
    monkeypatch.setattr(main, "MAILERSEND_API_KEY", "key")
    await gain(name, greeters=[7])
    (url, request), = bot.http.posts
    assert url == "https://api.mailersend.com/v1/email"
    assert request["headers"]["Authorization"] == "Bearer key"
    payload = request["json"]
    assert payload["to"] == [{"email": EMAILS[name], "name": f"{name} Onboarder"}]
    assert payload["subject"] == "pat has joined PauseAI Discord"
    assert payload["text"] == (f"A new user just joined the Discord server and received the {name} role!"
                               "\n\nUser: pat")
    assert bot.channel.posts == [f"<@7> : <@99> has joined from {name}!"]


async def test_other_countries_send_no_email(bot):
    for name in ("Portugal", "United Kingdom", "France", "Spain"):
        await gain(name, greeters=[7])
    assert bot.http.posts == []


async def test_the_delayed_welcome_includes_a_never_hard_coded_countrys_dm(monkeypatch):
    # The delayed welcome finds country roles the same way: any country, and France's own DM
    monkeypatch.setattr(main.asyncio, "sleep", lambda seconds: _noop())
    france, portugal, california = Role("France"), Role("Portugal"), Role("California")
    guild = NS(id=main.PAUSEAI_SERVER_ID, roles=[france, portugal, california])
    member = Member(guild, [portugal, california, france], None)
    guild.get_member = lambda member_id: member
    await main._send_delayed_welcome_dm(member)
    general, france_dm = member.dms
    assert general.startswith("Welcome to the PauseAI Global Discord, pat!")
    assert france_dm.startswith("Welcome to PauseAI in France 🇫🇷:")


async def _noop():
    return None
