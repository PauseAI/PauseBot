from types import SimpleNamespace as NS
import onboarding

ALICE, BOB, CAROL, NEWBIE = 1, 2, 3, 99


def role(name="role", member_ids=()):
    return NS(name=name, members=[NS(id=i) for i in member_ids])


def test_uses_intersection_of_country_and_national_leader():
    country = role(member_ids=[ALICE, BOB, NEWBIE])
    leaders = role(member_ids=[BOB, CAROL])

    assert onboarding.resolve_onboarders([], country, leaders, NEWBIE) == [BOB]


def test_static_onboarders_override_intersection():
    country = role(member_ids=[ALICE, BOB])
    leaders = role(member_ids=[BOB])

    assert onboarding.resolve_onboarders([CAROL, ALICE], country, leaders, NEWBIE) == [CAROL, ALICE]


def test_empty_intersection_still_posts_a_message():
    country = role(member_ids=[ALICE, NEWBIE])
    leaders = role(member_ids=[CAROL])

    ids = onboarding.resolve_onboarders(None, country, leaders, NEWBIE)
    assert ids == []
    msg = onboarding.onboarding_message(ids, "<@99>", "Israel")
    assert msg == "<@99> has joined from Israel, but there's no National Leader for Israel to ping."
    assert "<@1>" not in msg


def test_missing_national_leader_role_gives_no_onboarders():
    assert onboarding.resolve_onboarders([], role(member_ids=[ALICE]), None, NEWBIE) == []


def test_joining_member_is_never_pinged():
    # A new member who already holds National Leader shouldn't ping themselves
    country = role(member_ids=[NEWBIE, BOB])
    leaders = role(member_ids=[NEWBIE, BOB])
    assert onboarding.resolve_onboarders([], country, leaders, NEWBIE) == [BOB]
    # Nor when they're on the static list
    assert onboarding.resolve_onboarders([NEWBIE, ALICE], country, leaders, NEWBIE) == [ALICE]


def test_onboarders_are_deduplicated():
    assert onboarding.resolve_onboarders([ALICE, BOB, ALICE], role(), role(), NEWBIE) == [ALICE, BOB]


def test_message_mentions_each_onboarder():
    assert onboarding.onboarding_message([ALICE, BOB], "<@99>", "UK") == "<@1> <@2> : <@99> has joined from UK!"


def test_find_national_leader_role_by_name():
    leader = role("National Leader")
    guild = NS(roles=[role("Moderator"), leader])
    assert onboarding.find_national_leader_role(guild) is leader
    assert onboarding.find_national_leader_role(NS(roles=[role("Moderator")])) is None
