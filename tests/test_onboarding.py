import asyncio
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


def test_static_list_of_only_the_joining_member_falls_back_to_tiers():
    country = role(member_ids=[NEWBIE, BOB, CAROL])
    greeters = role(member_ids=[BOB])
    leaders = role(member_ids=[CAROL])
    assert resolve([NEWBIE], country, greeters, leaders) == [BOB]
    assert resolve([NEWBIE], country, None, leaders) == [CAROL]
    assert resolve([NEWBIE], country, None, None) == []


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
    info = {"name": "UK", "onboarders": [ALICE]}
    assert onboarding.country_settings(info, BEFORE) == info
    assert onboarding.country_settings(info, AFTER) == info


UK = main.COUNTRY_DATA[1188719374941560925]
US = main.COUNTRY_DATA[1188717849980702781]
GERMANY = main.COUNTRY_DATA[1188719399117541396]


def dms(*infos, now=AFTER, skip=None):
    return onboarding.welcome_dms(infos, main.GENERAL_DM, "pat", now, skip_paragraph=skip)


def test_no_country_gets_general_welcome():
    assert dms() == [main.GENERAL_DM.format(user="pat")]


def test_countries_without_own_msg_get_one_general_welcome():
    assert dms(US, GERMANY) == [main.GENERAL_DM.format(user="pat")]


def test_uk_gets_general_welcome():
    assert dms(UK) == [main.GENERAL_DM.format(user="pat")]
    assert not any("noDM" in info for info in main.COUNTRY_DATA.values())


def test_france_gets_general_welcome_then_its_own_dm():
    general, france_dm = dms(main.COUNTRY_DATA[FRANCE_ID])
    assert general == main.GENERAL_DM.format(user="pat")
    assert france_dm.startswith("Welcome to PauseAI in France 🇫🇷:\n\nBienvenue pat")


def test_france_dm_sent_whatever_order_alongside_uk():
    france = main.COUNTRY_DATA[FRANCE_ID]
    assert dms(UK, france) == dms(france, UK) == dms(france)


def test_general_welcome_then_one_dm_per_country_with_own_msg():
    a = {"name": "A", "flag": "🇦🇦", "msg": "hi {user} from A"}
    b = {"name": "B", "msg": "hi {user} from B"}
    assert dms(a, US, b) == [main.GENERAL_DM.format(user="pat"),
                             "Welcome to PauseAI in A 🇦🇦:\n\nhi pat from A",
                             "Welcome to PauseAI in B:\n\nhi pat from B"]


def test_general_welcome_asks_to_join_once_without_microcommit():
    assert main.GENERAL_DM.count("pauseai.info/join)") == 1
    assert "MicroCommit" not in main.GENERAL_DM


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


JOINED = NS(id=500, name="Wants to volunteer")
WANTS_TO_HELP = NS(id=501, name="Wants to help")


def member_in(guild_roles, member_roles):
    return NS(guild=NS(roles=guild_roles), roles=member_roles)


def test_has_joined_with_wants_to_volunteer_role():
    assert onboarding.has_joined(member_in([WANTS_TO_HELP, JOINED], [JOINED]))


def test_has_not_joined_without_the_role():
    # "Wants to help" is only an onboarding answer, not a join
    assert not onboarding.has_joined(member_in([WANTS_TO_HELP, JOINED], [WANTS_TO_HELP]))


def test_has_not_joined_when_guild_has_no_such_role():
    assert not onboarding.has_joined(member_in([WANTS_TO_HELP], [WANTS_TO_HELP]))


def joined_general_welcome():
    return dms(skip=main.GENERAL_DM_JOIN_PARAGRAPH)[0]


def test_joined_member_gets_general_welcome_without_join_paragraph():
    general = main.GENERAL_DM.format(user="pat")
    assert main.GENERAL_DM_JOIN_PARAGRAPH in general
    joined = joined_general_welcome()
    assert "pauseai.info/join" not in joined
    # Everything else is identical, with no extra blank line where the paragraph was
    assert joined == general.replace(main.GENERAL_DM_JOIN_PARAGRAPH + "\n \n", "")
    assert "\n \n \n" not in joined and "\n\n" not in joined
    assert joined.count("\n \n") == general.count("\n \n") - 1


def test_member_who_has_not_joined_keeps_join_paragraph():
    assert dms(skip=None)[0] == main.GENERAL_DM.format(user="pat")


def test_skipping_join_paragraph_leaves_country_dms_alone():
    france = main.COUNTRY_DATA[FRANCE_ID]
    assert dms(france, skip=main.GENERAL_DM_JOIN_PARAGRAPH)[1:] == dms(france)[1:]


SPAIN = main.COUNTRY_DATA[1188720344882753566]
SPAIN_ONBOARDER = SPAIN["onboarders"][0]


def own_ping(info, country, tiers, member_id, now, added=()):
    return onboarding.onboarding_ping(info, country, tiers, member_id, "<@dani>", now=now,
                                      added_tier_names=added)


def test_static_onboarder_picking_up_own_country_role_before_switch():
    country = role(member_ids=[SPAIN_ONBOARDER, BOB])
    tiers = [role(member_ids=[BOB]), None]
    assert own_ping(SPAIN, country, tiers, SPAIN_ONBOARDER, BEFORE) == (
        "<@dani> (Spain onboarder) picked up the Spain role", [])


def test_static_list_no_longer_makes_an_onboarder_after_switch():
    country = role(member_ids=[SPAIN_ONBOARDER, BOB])
    tiers = [role(member_ids=[BOB]), None]
    msg, ids = own_ping(SPAIN, country, tiers, SPAIN_ONBOARDER, AFTER)
    assert ids == [BOB]
    assert msg == "<@2> : <@dani> has joined from Spain!"


def test_greeter_picking_up_country_role_pings_nobody():
    country = role(member_ids=[DAVE, BOB])
    for now in (BEFORE, AFTER):
        # Chapter Greeter wins over being on the static list
        msg, ids = own_ping({"name": "Spain", "onboarders": [DAVE]}, country,
                            [role(member_ids=[DAVE, BOB]), None], DAVE, now)
        assert (msg, ids) == ("<@dani> (Spain Chapter Greeter) picked up the Spain role", [])


def test_national_leader_picking_up_country_role_pings_nobody():
    country = role(member_ids=[DAVE, BOB])
    tiers = [role(member_ids=[BOB]), role(member_ids=[DAVE])]
    assert own_ping({"name": "Israel", "onboarders": []}, country, tiers, DAVE, AFTER) == (
        "<@dani> (Israel National Leader) picked up the Israel role", [])


def test_onboarder_picking_up_no_ping_country_posts_nothing():
    france = main.COUNTRY_DATA[FRANCE_ID]
    country = role(member_ids=[DAVE])
    assert own_ping(france, country, [role(member_ids=[DAVE]), None], DAVE, AFTER) == (None, [])
    # Before the switch France still pings, so its old onboarder picking it up is noted
    assert own_ping(france, country, [None, None], FRANCE_OLD_ONBOARDER, BEFORE)[0] == (
        "<@dani> (France onboarder) picked up the France role")


def test_country_and_tier_role_in_one_update_leaves_post_to_tier_message():
    country = role(member_ids=[DAVE])
    tiers = [role(member_ids=[DAVE]), None]
    for now in (BEFORE, AFTER):
        assert own_ping(SPAIN, country, tiers, DAVE, now, added=["Chapter Greeter"]) == (None, [])
    # Unless they were already a National Leader, which made them its onboarder before this update
    tiers = [role(member_ids=[DAVE]), role(member_ids=[DAVE])]
    assert own_ping(SPAIN, country, tiers, DAVE, AFTER, added=["Chapter Greeter"])[0] == (
        "<@dani> (Spain National Leader) picked up the Spain role")


def test_static_onboarder_gaining_greeter_and_country_together_leaves_post_to_tier_message():
    country = role(member_ids=[SPAIN_ONBOARDER])
    tiers = [role(member_ids=[SPAIN_ONBOARDER]), None]
    assert own_ping(SPAIN, country, tiers, SPAIN_ONBOARDER, BEFORE)[0] == (
        "<@dani> (Spain Chapter Greeter) picked up the Spain role")
    # Already on the static list before the update, so the pickup is still noted
    assert own_ping(SPAIN, country, tiers, SPAIN_ONBOARDER, BEFORE, added=["Chapter Greeter"])[0] == (
        "<@dani> (Spain onboarder) picked up the Spain role")


def test_onboarder_title():
    greeters, leaders = role(member_ids=[ALICE]), role(member_ids=[ALICE, BOB])
    info = {"name": "UK", "onboarders": [CAROL]}
    assert onboarding.onboarder_title(info, [greeters, leaders], ALICE, AFTER) == "Chapter Greeter"
    assert onboarding.onboarder_title(info, [greeters, leaders], BOB, AFTER) == "National Leader"
    assert onboarding.onboarder_title(info, [None, None], CAROL, BEFORE) == "onboarder"
    assert onboarding.onboarder_title(info, [None, None], CAROL, AFTER) is None
    assert onboarding.onboarder_title(info, [greeters, leaders], NEWBIE, BEFORE) is None


def tier_msg(added, *infos, now=AFTER):
    return onboarding.tier_role_message(added, infos, "<@dani>", now)


def test_becoming_a_greeter_is_announced_for_held_country():
    assert tier_msg(["Chapter Greeter"], SPAIN) == "<@dani> has become a Chapter Greeter for Spain"
    assert tier_msg(["National Leader"], SPAIN, now=BEFORE) == "<@dani> has become a National Leader for Spain"


def test_tier_announcement_names_every_held_country():
    assert tier_msg(["Chapter Greeter"], SPAIN, UK, US) == (
        "<@dani> has become a Chapter Greeter for Spain, UK and US")


def test_both_tier_roles_at_once_get_a_line_each():
    assert tier_msg(["National Leader", "Chapter Greeter"], SPAIN) == (
        "<@dani> has become a Chapter Greeter for Spain\n"
        "<@dani> has become a National Leader for Spain")


def test_tier_announcement_leaves_out_no_ping_countries():
    france = main.COUNTRY_DATA[FRANCE_ID]
    assert tier_msg(["Chapter Greeter"], france, SPAIN) == "<@dani> has become a Chapter Greeter for Spain"
    assert tier_msg(["Chapter Greeter"], france) is None
    # France still pings before the switch
    assert tier_msg(["Chapter Greeter"], france, now=BEFORE) == "<@dani> has become a Chapter Greeter for France"


def test_no_tier_announcement_without_country_or_tier_role():
    assert tier_msg(["Chapter Greeter"]) is None
    assert tier_msg([], SPAIN) is None


LONG_AGO = datetime.datetime(2025, 3, 4, 12, 0, tzinfo=datetime.UTC)


def test_existing_member_threshold():
    assert onboarding.EXISTING_MEMBER_AFTER == datetime.timedelta(days=1)
    assert not onboarding.is_existing_member(None, AFTER)
    assert not onboarding.is_existing_member(AFTER - datetime.timedelta(hours=23), AFTER)
    assert not onboarding.is_existing_member(AFTER - datetime.timedelta(days=1), AFTER)
    assert onboarding.is_existing_member(AFTER - datetime.timedelta(days=1, seconds=1), AFTER)


def existing_ping(info, country, tiers, now, joined_at=LONG_AGO, member_id=NEWBIE):
    return onboarding.onboarding_ping(info, country, tiers, member_id, "<@99>", now=now, joined_at=joined_at)


def test_existing_member_adding_country_pings_greeters():
    info = {"name": "Spain", "onboarders": [ALICE]}
    country = role(member_ids=[BOB, CAROL, NEWBIE])
    tiers = [role(member_ids=[BOB]), role(member_ids=[CAROL])]
    assert existing_ping(info, country, tiers, AFTER) == (
        "<@2> : <@99> (member since Mar 2025) added Spain", [BOB])
    # Same routing as a join: the static list before the switch
    assert existing_ping(info, country, tiers, BEFORE) == (
        "<@1> : <@99> (member since Mar 2025) added Spain", [ALICE])


def test_existing_member_adding_country_with_nobody_to_ping():
    msg, ids = existing_ping({"name": "Israel", "onboarders": []}, role(member_ids=[NEWBIE]), [None, None], AFTER)
    assert ids == []
    assert msg == ("<@99> (member since Mar 2025) added Israel, but there's no Chapter Greeter "
                   "or National Leader for Israel to ping.")


def test_recent_joiner_still_gets_joined_message():
    info = {"name": "Israel", "onboarders": []}
    country = role(member_ids=[BOB, NEWBIE])
    msg, _ = existing_ping(info, country, [role(member_ids=[BOB]), None], AFTER,
                           joined_at=AFTER - datetime.timedelta(minutes=5))
    assert msg == "<@2> : <@99> has joined from Israel!"


def test_existing_member_adding_no_ping_country_posts_nothing():
    france = main.COUNTRY_DATA[FRANCE_ID]
    assert existing_ping(france, role(member_ids=[BOB, NEWBIE]), [role(member_ids=[BOB]), None], AFTER) == (None, [])


def test_existing_greeter_adding_own_country_is_still_a_pickup():
    country = role(member_ids=[DAVE, BOB])
    tiers = [role(member_ids=[DAVE, BOB]), None]
    assert existing_ping(SPAIN, country, tiers, AFTER, member_id=DAVE) == (
        "<@99> (Spain Chapter Greeter) picked up the Spain role", [])


def added_dms(*infos, joined_at=LONG_AGO, tiers=(None, None), member_id=NEWBIE, now=AFTER):
    return onboarding.added_country_dms(infos, list(tiers), member_id, "pat", joined_at, now)


def test_existing_member_gets_only_country_dm():
    france = main.COUNTRY_DATA[FRANCE_ID]
    dm, = added_dms(france, UK)
    assert dm.startswith("Welcome to PauseAI in France 🇫🇷:\n\n")
    assert "Bienvenue pat" in dm
    assert main.GENERAL_DM.split("\n")[0].format(user="pat") not in dm


def test_existing_member_adding_country_without_msg_gets_no_dm():
    assert added_dms(UK, SPAIN) == []
    # France has no msg of its own before the switch
    assert added_dms(main.COUNTRY_DATA[FRANCE_ID], now=BEFORE) == []


def test_new_member_gets_no_dm_on_role_gain():
    # Their welcome comes from the delayed welcome DM
    assert added_dms(main.COUNTRY_DATA[FRANCE_ID], joined_at=AFTER - datetime.timedelta(minutes=5)) == []
    assert added_dms(main.COUNTRY_DATA[FRANCE_ID], joined_at=None) == []


def test_onboarder_adding_own_country_gets_no_dm():
    assert added_dms(main.COUNTRY_DATA[FRANCE_ID], tiers=(role(member_ids=[NEWBIE]), None)) == []


async def test_member_awaiting_welcome_counts_as_new_until_it_has_run(monkeypatch):
    # Passing rules screening days after joining is still a join: the delayed welcome sends the DMs
    ran = []

    async def fake_send(member):
        ran.append(member.id)

    monkeypatch.setattr(main, "_send_delayed_welcome_dm", fake_send)
    main.schedule_welcome_dm(NS(id=NEWBIE))
    assert NEWBIE in main.awaiting_welcome  # before the task has had a chance to run
    await asyncio.sleep(0)
    await asyncio.sleep(0)
    assert ran == [NEWBIE]
    assert NEWBIE not in main.awaiting_welcome
