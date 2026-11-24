"""The country finder (country_finder.py): region buttons, country dropdowns and /country."""
import datetime
import itertools
from types import SimpleNamespace as NS
import discord
import pytest
import country_finder as cf
import db
import main
from jobs.country_regions_table import COUNTRIES, REGIONS

IDS = itertools.count(1000)
EUROPE = REGIONS.index("Europe")


class Role:
    def __init__(self, name, assignable=True, **perms):
        self.id, self.name, self._assignable = next(IDS), name, assignable
        self.permissions = discord.Permissions(**perms)

    def is_default(self):
        return False

    def is_bot_managed(self):
        return False

    def is_assignable(self):
        return self._assignable

    def __repr__(self):
        return f"Role({self.name})"


class Member:
    def __init__(self, *roles):
        self.id, self.name, self.roles = next(IDS), "pat", list(roles)
        self.added, self.removed, self.fail = [], [], False

    async def add_roles(self, *roles, reason=None):
        if self.fail:
            raise discord.HTTPException(NS(status=500, reason="oops"), "oops")
        self.added.append(([r.name for r in roles], reason))
        self.roles += roles

    async def remove_roles(self, *roles, reason=None):
        self.removed.append(([r.name for r in roles], reason))
        self.roles = [r for r in self.roles if r not in roles]


class Response:
    def __init__(self):
        self.sent, self.edited = [], []

    async def send_message(self, content=None, **kwargs):
        self.sent.append((content, kwargs))

    async def edit_message(self, **kwargs):
        self.edited.append(kwargs)


def make_guild(*extra_roles):
    roles = {name: Role(name) for countries in COUNTRIES.values() for name in countries}
    guild = NS(id=main.PAUSEAI_SERVER_ID, roles=[*roles.values(), *extra_roles],
               text_channels=[NS(name="united-kingdom", mention="<#77>")])
    return guild, roles


def interaction(guild, member, custom_id, values=None, kind=discord.InteractionType.component):
    data = {"custom_id": custom_id}
    if values is not None:
        data["values"] = values
    return NS(type=kind, data=data, guild=guild, guild_id=guild.id, user=member, response=Response())


@pytest.fixture(autouse=True)
def fresh_limiter(monkeypatch):
    monkeypatch.setattr(cf, "limiter", cf.ChangeLimiter())


def selects(view):
    return [item for item in view.children if isinstance(item, discord.ui.Select)]


# ── Pages ─────────────────────────────────────────────────────────────────────

def test_every_region_fits_in_dropdowns_of_25_and_lists_every_country_once():
    for region in REGIONS:
        pages = cf.region_pages(region)
        assert all(len(p) <= cf.MAX_OPTIONS for p in pages)
        assert [n for p in pages for n in p] == sorted(COUNTRIES[region])
        assert len(pages) <= 5  # a message holds at most 5 rows


def test_big_regions_split_alphabetically_small_ones_dont():
    assert len(cf.region_pages("Europe")) == 2
    assert cf.page_label("Europe", 0).startswith("Europe A–")
    assert cf.page_label("Europe", 1).endswith("–U")
    assert cf.page_label("Middle East", 0) == "Middle East"


def test_changes_only_touch_the_dropdowns_own_countries():
    assert cf.changes({"France", "Spain"}, ["France", "Germany"], ["Germany"]) == (["Germany"], ["France"])
    # A value from another page can't add a country
    assert cf.changes(set(), ["France"], ["Spain"]) == ([], [])


def test_only_safe_assignable_country_roles_are_offered():
    guild, roles = make_guild(Role("Moderators"))
    roles["France"].permissions = discord.Permissions(manage_messages=True)
    roles["Spain"]._assignable = False  # above the bot's role
    offered = cf.country_roles(guild, main.is_role_safe_to_grant)
    assert "Germany" in offered and "France" not in offered and "Spain" not in offered
    assert "Moderators" not in offered


@pytest.mark.parametrize("custom_id", [
    None, "", "other:region:0", "country_finder:region:9", "country_finder:region:x",
    "country_finder:pick:0:7", "country_finder:pick:0", "country_finder:region:-1"])
def test_unknown_custom_ids_are_ignored(custom_id):
    assert cf.parse(custom_id) is None


# ── Region buttons ────────────────────────────────────────────────────────────

async def test_the_posted_message_has_a_button_per_region():
    view = cf.region_buttons()
    assert [b.label for b in view.children] == REGIONS
    assert [cf.parse(b.custom_id) for b in view.children] == [("region", i) for i in range(len(REGIONS))]
    assert view.is_finished()  # not kept by discord.py: on_interaction answers its clicks


async def test_region_button_opens_its_countries_privately_with_held_ones_ticked():
    guild, roles = make_guild()
    member = Member(roles["United Kingdom"], roles["Brazil"])
    i = interaction(guild, member, f"country_finder:region:{EUROPE}")
    assert await cf.handle_interaction(i, main.is_role_safe_to_grant)
    (content, kwargs), = i.response.sent
    assert kwargs["ephemeral"] is True and "Europe" in content
    first, second = selects(kwargs["view"])
    options = {o.value: o for s in (first, second) for o in s.options}
    assert set(options) == set(COUNTRIES["Europe"])
    assert [n for n, o in options.items() if o.default] == ["United Kingdom"]
    assert str(options["France"].emoji) == "🇫🇷"
    assert options["Kosovo"].emoji is None  # not a Unicode flag
    assert options["Germany"].description == "Deutschland"
    assert first.min_values == 0 and first.max_values == len(first.options)


async def test_every_regions_dropdowns_are_within_discords_limits():
    guild, roles = make_guild()
    held = set(roles)  # every country ticked: the longest a view gets
    for index, region in enumerate(REGIONS):
        rows = cf.region_view(index, cf.country_roles(guild, main.is_role_safe_to_grant), held).to_components()
        assert 1 <= len(rows) <= 5
        for row in rows:
            select, = row["components"]
            assert len(select["custom_id"]) <= 100 and len(select["placeholder"]) <= 150
            assert len(select["options"]) <= 25 and select["max_values"] == len(select["options"])
            for option in select["options"]:
                assert len(option["label"]) <= 100 and len(option["value"]) <= 100
                assert len(option.get("description") or "") <= 100


async def test_a_region_without_roles_says_so():
    guild = NS(id=main.PAUSEAI_SERVER_ID, roles=[], text_channels=[])
    i = interaction(guild, Member(), "country_finder:region:0")
    await cf.handle_interaction(i, main.is_role_safe_to_grant)
    (content, kwargs), = i.response.sent
    assert "no country roles" in content and selects(kwargs["view"]) == []


# ── Picking countries ─────────────────────────────────────────────────────────

def pick_id(region, country):
    page = next(n for n, p in enumerate(cf.region_pages(region)) if country in p)
    return f"country_finder:pick:{REGIONS.index(region)}:{page}"


async def test_ticking_gives_the_role_and_points_to_its_channel():
    guild, roles = make_guild()
    member = Member()
    i = interaction(guild, member, pick_id("Europe", "United Kingdom"), ["United Kingdom"])
    await cf.handle_interaction(i, main.is_role_safe_to_grant)
    assert member.added == [(["United Kingdom"], cf.AUDIT_REASON)] and member.removed == []
    edit, = i.response.edited
    assert "**United Kingdom**" in edit["content"] and "<#77>" in edit["content"]
    ticked = [o.value for s in selects(edit["view"]) for o in s.options if o.default]
    assert ticked == ["United Kingdom"]


async def test_unticking_removes_only_that_pages_countries_and_keeps_other_roles():
    volunteer = Role("Wants to volunteer")
    guild, roles = make_guild(volunteer)
    page = pick_id("Europe", "Austria")
    other_page_country = next(n for n in COUNTRIES["Europe"] if pick_id("Europe", n) != page)
    member = Member(roles["Austria"], roles["Belgium"], roles[other_page_country], roles["Brazil"], volunteer)
    i = interaction(guild, member, page, ["Belgium", "Albania"])
    await cf.handle_interaction(i, main.is_role_safe_to_grant)
    assert member.added == [(["Albania"], cf.AUDIT_REASON)]
    assert member.removed == [(["Austria"], cf.AUDIT_REASON)]
    assert {r.name for r in member.roles} == {"Albania", "Belgium", other_page_country, "Brazil", "Wants to volunteer"}


async def test_a_forged_value_for_an_unsafe_or_unknown_role_gives_nothing():
    guild, roles = make_guild(Role("Admins", administrator=True))
    roles["France"].permissions = discord.Permissions(administrator=True)
    member = Member()
    i = interaction(guild, member, pick_id("Europe", "France"), ["France", "Admins", "Brazil"])
    await cf.handle_interaction(i, main.is_role_safe_to_grant)
    assert member.added == [] and member.roles == []
    assert "No change." in i.response.edited[0]["content"]


async def test_an_unsafe_country_role_a_member_holds_is_never_removed():
    guild, roles = make_guild()
    roles["France"].permissions = discord.Permissions(manage_roles=True)
    member = Member(roles["France"])
    await cf.handle_interaction(interaction(guild, member, pick_id("Europe", "France"), []), main.is_role_safe_to_grant)
    assert member.removed == []


async def test_changes_stop_after_the_limit():
    guild, roles = make_guild()
    member = Member()
    for n in range(cf.CHANGE_LIMIT + 1):
        values = ["Germany"] if n % 2 == 0 else []
        i = interaction(guild, member, pick_id("Europe", "Germany"), values)
        member.roles = [roles["Germany"]] if n % 2 else []
        await cf.handle_interaction(i, main.is_role_safe_to_grant)
    assert len(member.added) + len(member.removed) == cf.CHANGE_LIMIT
    assert "a lot just now" in i.response.edited[0]["content"]


def test_the_limit_lifts_after_the_window():
    now = [0]
    limiter = cf.ChangeLimiter(limit=1, window=10, clock=lambda: now[0])
    assert limiter.allow(1) and not limiter.allow(1) and limiter.allow(2)
    now[0] = 11
    assert limiter.allow(1)


async def test_a_failed_role_change_says_so():
    guild, roles = make_guild()
    member = Member()
    member.fail = True
    i = interaction(guild, member, pick_id("Europe", "Germany"), ["Germany"])
    await cf.handle_interaction(i, main.is_role_safe_to_grant)
    edit, = i.response.edited
    assert "didn't work" in edit["content"]
    assert not any(o.default for s in selects(edit["view"]) for o in s.options)


async def test_other_interactions_are_left_alone():
    guild, _ = make_guild()
    for i in (interaction(guild, Member(), "something_else:1"),
              interaction(guild, Member(), "country_finder:region:0", kind=discord.InteractionType.application_command)):
        assert not await cf.handle_interaction(i, main.is_role_safe_to_grant)
        assert i.response.sent == [] and i.response.edited == []


async def test_on_interaction_ignores_other_servers(monkeypatch):
    guild, _ = make_guild()
    guild.id = 999
    i = interaction(guild, Member(), "country_finder:region:0")
    await main.on_interaction(i)
    assert i.response.sent == []
    guild.id = main.PAUSEAI_SERVER_ID
    i = interaction(guild, Member(), "country_finder:region:0")
    i.guild_id = guild.id
    await main.on_interaction(i)
    assert len(i.response.sent) == 1


# ── /country ──────────────────────────────────────────────────────────────────

async def test_country_command_gives_a_named_country():
    guild, _ = make_guild()
    member = Member()
    i = interaction(guild, member, None)
    await cf.add_country(i, main.is_role_safe_to_grant, " united kingdom ")
    assert member.added == [(["United Kingdom"], cf.AUDIT_REASON)]
    assert "<#77>" in i.response.sent[0][0] and i.response.sent[0][1]["ephemeral"]


async def test_country_command_with_an_unknown_or_held_country_changes_nothing():
    guild, roles = make_guild()
    member = Member(roles["Spain"])
    i = interaction(guild, member, None)
    await cf.add_country(i, main.is_role_safe_to_grant, "Atlantis")
    await cf.add_country(i, main.is_role_safe_to_grant, "spain")
    assert member.added == []
    (unknown, kwargs), (held, _) = i.response.sent
    assert "Atlantis" in unknown and kwargs["view"].children
    assert "already" in held


def test_autocomplete_puts_prefix_matches_first_and_knows_endonyms():
    guild, _ = make_guild()
    roles = cf.country_roles(guild, main.is_role_safe_to_grant)
    assert cf.autocomplete(roles, "ger") == ["Germany", "Algeria", "Niger", "Nigeria"]
    assert cf.autocomplete(roles, "deutsch") == ["Germany"]
    assert len(cf.autocomplete(roles, "")) == 25


# ── Saved roles aren't given back outside joins ───────────────────────────────

def record_member(guild, *roles):
    m = Member(*roles)
    m.guild, m.nick, m.global_name = guild, None, "pat"
    m.joined_at = datetime.datetime(2025, 1, 1, tzinfo=datetime.UTC)
    return m


async def test_a_country_dropped_in_the_finder_isnt_given_back_by_a_record_sync(pool, monkeypatch):
    monkeypatch.setattr(main, "db_pool", pool)
    uk, germany = Role("United Kingdom"), Role("Germany")
    guild = NS(roles=[uk, germany], get_role=lambda rid: next((r for r in (uk, germany) if r.id == rid), None))
    member = record_member(guild, uk)
    await main.sync_member_record(member)
    # Unticks the UK, then ticks Germany: on_member_update saves the record again
    member.roles = [germany]
    await main.sync_member_record(member, onboarding_notification=True)
    assert member.added == []
    assert (await db.saved_roles(pool, member.id))[1] == ["Germany"]


async def test_a_rejoining_member_still_gets_their_saved_roles_back(pool, monkeypatch):
    monkeypatch.setattr(main, "db_pool", pool)
    uk = Role("United Kingdom")
    guild = NS(roles=[uk], get_role=lambda rid: uk if rid == uk.id else None)
    member = record_member(guild, uk)
    await main.sync_member_record(member)
    member.roles = []
    await main.sync_member_record(member, restore_roles=True)
    assert member.added == [(["United Kingdom"], None)]
