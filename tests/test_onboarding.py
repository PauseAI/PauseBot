import datetime
from types import SimpleNamespace as NS
import main
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


def test_greeter_tier_is_tried_first():
    assert onboarding.TIER_ROLE_NAMES == ("Chapter Greeter", "National Leader")


BEFORE = onboarding.ROLE_ROUTING_FROM - datetime.timedelta(seconds=1)
AFTER = onboarding.ROLE_ROUTING_FROM
FRANCE_ID = 1188719426552479824
FRANCE_OLD_ONBOARDER = 456868543225528331


def ping(info, country, tiers, now):
    return onboarding.onboarding_ping(info, country, tiers, NEWBIE, "<@99>", now=now)


def test_switch_is_fri_13_nov_17_00_uk():
    assert onboarding.ROLE_ROUTING_FROM.isoformat() == "2026-11-13T17:00:00+00:00"
    assert not onboarding.role_routing_active(BEFORE)
    assert onboarding.role_routing_active(AFTER)


def test_before_switch_static_list_wins():
    info = {"name": "UK", "onboarders": [ALICE]}
    country = role(member_ids=[BOB, NEWBIE])
    msg, ids = ping(info, country, [role(member_ids=[BOB]), None], BEFORE)
    assert ids == [ALICE]


def test_after_switch_static_list_is_ignored():
    info = {"name": "UK", "onboarders": [ALICE]}
    country = role(member_ids=[BOB, CAROL, NEWBIE])
    tiers = [role(member_ids=[BOB]), role(member_ids=[CAROL])]
    assert ping(info, country, tiers, AFTER)[1] == [BOB]
    # No greeter in the country: National Leaders
    assert ping(info, country, [None, role(member_ids=[CAROL])], AFTER)[1] == [CAROL]


def test_after_switch_static_list_with_nobody_by_role_posts_nobody_to_ping():
    info = {"name": "UK", "onboarders": [ALICE]}
    msg, ids = ping(info, role(member_ids=[NEWBIE]), [None, None], AFTER)
    assert ids == []
    assert "no Chapter Greeter or National Leader for UK" in msg


def test_countries_without_static_list_use_roles_on_both_sides_of_switch():
    info = {"name": "Israel", "onboarders": []}
    country = role(member_ids=[BOB, CAROL, NEWBIE])
    tiers = [role(member_ids=[BOB]), role(member_ids=[CAROL])]
    for now in (BEFORE, AFTER):
        msg, ids = ping(info, country, tiers, now)
        assert ids == [BOB]
        assert msg == "<@2> : <@99> has joined from Israel!"
        assert ping(info, country, [None, role(member_ids=[CAROL])], now)[1] == [CAROL]
        assert "no Chapter Greeter or National Leader" in ping(info, role(), [None, None], now)[0]


def test_no_ping_country_posts_nothing():
    info = {"name": "X", "onboarders": [ALICE], "noPing": True}
    country = role(member_ids=[BOB, NEWBIE])
    tiers = [role(member_ids=[BOB]), role(member_ids=[BOB])]
    for now in (BEFORE, AFTER):
        assert ping(info, country, tiers, now) == (None, [])
        assert ping({**info, "onboarders": []}, role(), [None, None], now) == (None, [])


def test_france_before_switch_pings_old_onboarder_with_general_dm():
    france = main.COUNTRY_DATA[FRANCE_ID]
    country = role(member_ids=[BOB, NEWBIE])
    msg, ids = ping(france, country, [role(member_ids=[BOB]), None], BEFORE)
    assert ids == [FRANCE_OLD_ONBOARDER]
    assert msg == f"<@{FRANCE_OLD_ONBOARDER}> : <@99> has joined from France!"
    settings = onboarding.country_settings(france, BEFORE)
    assert not settings.get("noPing")
    assert settings["msg"] == ""  # so the general welcome DM is sent


def test_france_after_switch_posts_nothing_and_sends_bilingual_dm():
    france = main.COUNTRY_DATA[FRANCE_ID]
    country = role(member_ids=[BOB, NEWBIE])
    assert ping(france, country, [role(member_ids=[BOB]), None], AFTER) == (None, [])
    dm = onboarding.country_settings(france, AFTER)["msg"].format(user="pat")
    assert dm.index("Bienvenue pat") < dm.index("Welcome pat")
    assert dm.count("https://discord.gg/vyXGd7AeGc") == 2


def test_country_settings_without_overrides_is_unchanged():
    info = {"name": "UK", "onboarders": [ALICE], "noDM": True}
    assert onboarding.country_settings(info, BEFORE) == info
    assert onboarding.country_settings(info, AFTER) == info


UK = main.COUNTRY_DATA[1188719374941560925]
US = main.COUNTRY_DATA[1188717849980702781]
GERMANY = main.COUNTRY_DATA[1188719399117541396]


def dms(*infos, now=AFTER):
    return onboarding.welcome_dms(infos, main.GENERAL_DM, "pat", now)


def test_no_country_gets_general_welcome():
    assert dms() == [main.GENERAL_DM.format(user="pat")]


def test_countries_without_own_msg_get_one_general_welcome():
    assert dms(US, GERMANY) == [main.GENERAL_DM.format(user="pat")]


def test_no_dm_country_suppresses_general_welcome():
    assert dms(UK) == []
    assert dms(GERMANY, UK) == []


def test_france_dm_is_headed_with_country_and_flag():
    [dm] = dms(main.COUNTRY_DATA[FRANCE_ID])
    assert dm.startswith("Welcome to PauseAI in France 🇫🇷:\n\nBienvenue pat")


def test_france_dm_sent_whatever_order_alongside_no_dm_country():
    france = main.COUNTRY_DATA[FRANCE_ID]
    assert dms(UK, france) == dms(france, UK) == dms(france)


def test_one_dm_per_country_with_own_msg():
    a = {"name": "A", "flag": "🇦🇦", "msg": "hi {user} from A"}
    b = {"name": "B", "msg": "hi {user} from B"}
    assert dms(a, US, b) == ["Welcome to PauseAI in A 🇦🇦:\n\nhi pat from A",
                             "Welcome to PauseAI in B:\n\nhi pat from B"]


def test_france_before_switch_gets_general_welcome():
    assert dms(main.COUNTRY_DATA[FRANCE_ID], now=BEFORE) == [main.GENERAL_DM.format(user="pat")]


def test_every_country_has_a_flag():
    assert all(info.get("flag") for info in main.COUNTRY_DATA.values())


def test_every_dm_fits_in_one_discord_message():
    longest_name = "x" * 32  # Discord usernames are at most 32 characters
    for info in [None, *main.COUNTRY_DATA.values()]:
        for now in (BEFORE, AFTER):
            for dm in onboarding.welcome_dms([info] if info else [], main.GENERAL_DM, longest_name, now):
                assert len(dm) <= onboarding.DISCORD_MESSAGE_LIMIT
