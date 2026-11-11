from types import SimpleNamespace as NS
import onboarding

ALICE, BOB, CAROL, DAVE, NEWBIE = 1, 2, 3, 4, 99


def role(name="role", member_ids=()):
    return NS(name=name, members=[NS(id=i) for i in member_ids])


def resolve(static_ids, country, greeters, leaders, joining=NEWBIE):
    return onboarding.resolve_onboarders(static_ids, country, [greeters, leaders], joining)


def test_greeters_take_precedence_over_national_leaders():
    country = role(member_ids=[ALICE, BOB, CAROL, NEWBIE])
    greeters = role(member_ids=[ALICE, DAVE])
    leaders = role(member_ids=[BOB])

    assert resolve([], country, greeters, leaders) == [ALICE]


def test_falls_back_to_national_leaders_when_no_greeter_in_country():
    country = role(member_ids=[ALICE, BOB, NEWBIE])
    greeters = role(member_ids=[DAVE])
    leaders = role(member_ids=[BOB, CAROL])

    assert resolve([], country, greeters, leaders) == [BOB]


def test_missing_greeter_role_falls_back_to_national_leaders():
    country = role(member_ids=[ALICE, BOB])
    leaders = role(member_ids=[BOB])

    assert resolve([], country, None, leaders) == [BOB]


def test_both_roles_missing_gives_no_onboarders():
    assert resolve([], role(member_ids=[ALICE]), None, None) == []


def test_static_onboarders_override_both_tiers():
    country = role(member_ids=[ALICE, BOB])
    greeters = role(member_ids=[ALICE])
    leaders = role(member_ids=[BOB])

    assert resolve([CAROL, DAVE], country, greeters, leaders) == [CAROL, DAVE]


def test_nobody_in_either_tier_still_posts_a_message():
    country = role(member_ids=[ALICE, NEWBIE])
    ids = resolve(None, country, role(member_ids=[CAROL]), role(member_ids=[DAVE]))
    assert ids == []
    msg = onboarding.onboarding_message(ids, "<@99>", "Israel")
    assert msg == ("<@99> has joined from Israel, but there's no Chapter Greeter "
                   "or National Leader for Israel to ping.")


def test_joining_member_is_never_pinged():
    # A greeter joining their own country doesn't count as a greeter for it, so it falls through
    country = role(member_ids=[NEWBIE, BOB])
    greeters = role(member_ids=[NEWBIE])
    leaders = role(member_ids=[NEWBIE, BOB])
    assert resolve([], country, greeters, leaders) == [BOB]
    # Nor when they're on the static list
    assert resolve([NEWBIE, ALICE], country, greeters, leaders) == [ALICE]


def test_onboarders_are_deduplicated():
    assert resolve([ALICE, BOB, ALICE], role(), None, None) == [ALICE, BOB]


def test_message_mentions_each_onboarder():
    assert onboarding.onboarding_message([ALICE, BOB], "<@99>", "UK") == "<@1> <@2> : <@99> has joined from UK!"


def test_find_role_by_name():
    leader = role("National Leader")
    guild = NS(roles=[role("Moderator"), leader])
    assert onboarding.find_role_by_name(guild, "National Leader") is leader
    assert onboarding.find_role_by_name(guild, "Chapter Greeter") is None


def test_greeter_tier_is_tried_first():
    assert onboarding.TIER_ROLE_NAMES == ("Chapter Greeter", "National Leader")


def test_no_ping_country_posts_nothing():
    info = {"name": "France", "onboarders": [ALICE], "noPing": True}
    country = role(member_ids=[BOB, NEWBIE])
    tiers = [role(member_ids=[BOB]), role(member_ids=[BOB])]

    assert onboarding.onboarding_ping(info, country, tiers, NEWBIE, "<@99>") == (None, [])


def test_no_ping_country_skips_nobody_to_ping_message():
    info = {"name": "France", "onboarders": [], "noPing": True}
    assert onboarding.onboarding_ping(info, role(), [None, None], NEWBIE, "<@99>") == (None, [])


def test_country_without_no_ping_still_pings():
    info = {"name": "Israel", "onboarders": []}
    country = role(member_ids=[BOB, NEWBIE])
    msg, ids = onboarding.onboarding_ping(info, country, [None, role(member_ids=[BOB])], NEWBIE, "<@99>")
    assert ids == [BOB]
    assert msg == "<@2> : <@99> has joined from Israel!"


def test_country_without_no_ping_posts_nobody_to_ping_message():
    info = {"name": "Israel", "onboarders": []}
    msg, ids = onboarding.onboarding_ping(info, role(), [None, None], NEWBIE, "<@99>")
    assert ids == []
    assert "no Chapter Greeter or National Leader" in msg


def test_france_is_no_ping_with_bilingual_dm():
    import main
    france = main.COUNTRY_DATA[1188719426552479824]
    assert france["noPing"] is True
    assert france["onboarders"] == []
    dm = france["msg"].format(user="pat")
    assert dm.index("Bienvenue pat") < dm.index("Welcome pat")
    assert dm.count("https://discord.gg/vyXGd7AeGc") == 2
