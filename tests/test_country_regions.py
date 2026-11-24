import io
import json
import urllib.error
import pytest

from jobs import country_regions as cr
from jobs.country_regions_table import COUNTRIES, REGIONS, region_of
from tests.fake_discord import FakeDiscord, load_fixture

# The channels in the live 🌍 COUNTRIES and 🌎 COUNTRIES+ categories on 22 Nov 2026
LIVE_COUNTRY_CHANNELS = (
    "argentina armenia australia austria belgium brazil cameroon canada china colombia croatia czechia "
    "denmark european-union finland france germany honduras hungary iceland india indonesia ireland israel "
    "italy japan greece nepal netherlands new-zealand nigeria norway peru poland portugal philippines "
    "puerto-rico romania serbia singapore spain south-africa south-korea sweden switzerland taiwan ukraine "
    "united-states united-kingdom albania angola cuba ghana kazakhstan turkey georgia uganda uruguay mexico "
    "malaysia saudi-arabia senegal"
).split()


def run(fake, *args, tmp_path):
    lines = []
    code = cr.main([*args, "--guild", fake.guild_id, "--backup", str(tmp_path / "backup.json"),
                    "--onboarding-file", str(tmp_path / "onboarding.json")], http=fake, out=lines.append)
    return code, "\n".join(lines)


def apply(fake, tmp_path):
    code, output = run(fake, "--apply", tmp_path=tmp_path)
    assert code == 0, output
    return output


# ---- The region table ----

def test_each_country_is_in_exactly_one_region():
    seen = {}
    for region, countries in COUNTRIES.items():
        assert region in REGIONS
        for country in countries:
            assert country not in seen, f"{country} is in {seen.get(country)} and {region}"
            seen[country] = region


@pytest.mark.parametrize("country, region", [
    ("Russia", "Europe"), ("Georgia", "Europe"), ("Armenia", "Europe"), ("Azerbaijan", "Europe"),
    ("Egypt", "Africa"), ("Morocco", "Africa"), ("Algeria", "Africa"), ("Tunisia", "Africa"),
    ("Turkey", "Middle East"), ("Iran", "Middle East"), ("United Arab Emirates", "Middle East"),
    ("Kazakhstan", "Asia-Pacific"), ("Australia", "Asia-Pacific"), ("Puerto Rico", "Americas"),
    ("Dominican Republic", "Americas"), ("DR Congo", "Africa"), ("Ivory Coast", "Africa"),
    ("California", None), ("Dominian Republic", None),
])
def test_region_mapping(country, region):
    assert region_of(country) == region


def test_every_live_country_channel_has_a_region():
    for name in LIVE_COUNTRY_CHANNELS:
        assert name in cr.EXTRA_CHANNELS or region_of(cr.COUNTRY_BY_SLUG.get(name, "")), name


def test_flags_come_from_iso_codes():
    assert cr.flag("KE") == "🇰🇪"


# ---- Dry run ----

def test_dry_run_changes_nothing_and_lists_every_change(tmp_path):
    fake = FakeDiscord()
    code, output = run(fake, tmp_path=tmp_path)

    assert code == 0, output
    assert fake.writes == []
    assert not (tmp_path / "backup.json").exists()
    for expected in ["Rename role 'Dominian Republic' to 'Dominican Republic'", "Create category 🌍 EUROPE",
                     "Create #kenya in 🌍 AFRICA", "Create #russia in 🌍 EUROPE", "Create #dominican-republic in",
                     "Create #other-middle-east in 🌍 MIDDLE EAST", "Move #france into 🌍 EUROPE, keeping its own",
                     "Move #argentina into 🌎 AMERICAS, syncing", "Lock #request-a-country-🌍",
                     "Post welcome in #kenya", "Delete the empty category 🌍 COUNTRIES",
                     "Delete the empty category 🌎 COUNTRIES+", "Summary (dry run: nothing was changed)"]:
        assert expected in output, expected
    # Differences from the 21 Nov list are shown
    assert "Not in the 21 Nov list, created anyway: tunisia" in output
    assert "In the 21 Nov list but not created now:" in output


def test_dry_run_with_an_unmapped_channel_stops_before_anything(tmp_path):
    data = load_fixture()
    countries = next(c for c in data["channels"] if c["name"] == "🌍 COUNTRIES")
    data["channels"].append({"id": "5999", "name": "atlantis", "type": 0, "position": 20,
                             "parent_id": countries["id"], "permission_overwrites": []})
    fake = FakeDiscord(data)
    code, output = run(fake, "--apply", tmp_path=tmp_path)

    assert code == 1
    assert "#atlantis in 🌍 COUNTRIES isn't in the region table" in output
    assert fake.writes == []
    assert not (tmp_path / "backup.json").exists()


# ---- Apply ----

def test_apply_builds_the_regional_layout(tmp_path):
    fake = FakeDiscord()
    original = load_fixture()
    apply(fake, tmp_path)

    names = [c["name"] for c in sorted((c for c in fake.channels.values() if c["type"] == 4),
                                       key=lambda c: c["position"])]
    assert names == ["📌 INFO", "🌍 EUROPE", "🌎 AMERICAS", "🌍 AFRICA", "🌍 MIDDLE EAST", "🌏 ASIA-PACIFIC",
                     "📚 SERVER GUIDE RESOURCES"]
    assert fake.children("🌍 EUROPE") == ["european-union", "france", "germany", "russia", "other-europe"]
    assert fake.children("🌎 AMERICAS") == ["argentina", "cuba", "dominican-republic", "united-states",
                                            "other-americas"]
    # Tunisia: the only member is a National Leader. Ethiopia: no members, no channel: role only
    assert fake.children("🌍 AFRICA") == ["kenya", "tunisia", "other-africa"]
    assert fake.children("🌍 MIDDLE EAST") == ["turkey", "other-middle-east"]
    assert fake.children("🌏 ASIA-PACIFIC") == ["australia", "japan", "other-asia-pacific"]
    assert fake.children("📚 SERVER GUIDE RESOURCES") == ["faq", "request-a-country-🌍"]
    assert not any(c["name"] in ("california", "ethiopia") for c in fake.channels.values())

    # Role renamed, not recreated
    renamed = next(r for r in load_fixture()["roles"] if r["name"] == "Dominian Republic")
    assert fake.roles[renamed["id"]]["name"] == "Dominican Republic"

    # Regional categories copy COUNTRIES's overwrites, without the Paused (Wick) deny
    paused = next(r for r in fake.roles.values() if r["name"] == "Paused (Wick)")
    europe = fake.channel("🌍 EUROPE")
    old = next(c for c in original["channels"] if c["name"] == "🌍 COUNTRIES")
    assert {o["id"] for o in europe["permission_overwrites"]} == \
        {o["id"] for o in old["permission_overwrites"]} - {paused["id"]}

    # Moved and new channels are synced; france and germany keep exactly what they had
    for name in ["argentina", "australia", "european-union", "cuba", "kenya", "other-europe", "turkey"]:
        channel = fake.channel(name)
        parent = fake.channels[channel["parent_id"]]
        assert cr.overwrites_key(channel["permission_overwrites"]) == \
            cr.overwrites_key(parent["permission_overwrites"]), name
    for name in ["france", "germany"]:
        before = next(c for c in original["channels"] if c["name"] == name)
        assert fake.channel(name)["permission_overwrites"] == before["permission_overwrites"]
    # Moving keeps IDs (and so history)
    assert fake.channel("argentina")["id"] == next(c["id"] for c in original["channels"] if c["name"] == "argentina")

    assert fake.channel("other-europe")["topic"] == cr.OTHER_TOPIC
    assert "🌍 COUNTRIES" not in {c["name"] for c in fake.channels.values()}

    # request-a-country: notice posted, then @everyone can't send
    request = fake.channel("request-a-country-🌍")
    everyone = next(o for o in request["permission_overwrites"] if o["id"] == fake.guild_id)
    assert int(everyone["deny"]) & cr.SEND_MESSAGES
    [notice] = fake.messages[request["id"]]
    assert notice["content"].startswith(cr.REQUEST_NOTICE_START)
    assert f"<#{fake.channel('other-africa')['id']}>" in notice["content"]
    assert notice["allowed_mentions"] == {"parse": []}
    deleted = [w for w in fake.writes if w[0] == "DELETE"]
    assert len(deleted) == 2  # Only the two emptied categories


def test_welcomes_only_in_new_country_channels_mentioning_only_their_role(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)

    welcomed = {fake.channels[cid]["name"]: msgs for cid, msgs in fake.messages.items()
                if not fake.channels[cid]["name"].startswith("request")}
    assert set(welcomed) == {"kenya", "russia", "dominican-republic", "tunisia"}
    kenya = next(r for r in fake.roles.values() if r["name"] == "Kenya")
    [message] = welcomed["kenya"]
    assert message["content"].startswith(f"<@&{kenya['id']}>: this is your channel now")
    assert message["allowed_mentions"] == {"parse": [], "roles": [kenya["id"]]}
    # turkey has never had a message but isn't new, so it gets no welcome
    assert fake.channel("turkey")["id"] not in fake.messages


def test_apply_saves_a_backup_first(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)

    backup = json.loads((tmp_path / "backup.json").read_text())
    assert {c["name"] for c in backup["channels"]} == {c["name"] for c in load_fixture()["channels"]}
    assert backup["onboarding"] == load_fixture()["onboarding"]


def test_rerun_changes_nothing(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    writes = len(fake.writes)

    output = apply(fake, tmp_path)
    assert len(fake.writes) == writes, fake.writes[writes:]
    assert "No changes needed" in output
    # The first backup is kept; the rerun's goes beside it
    assert len(list(tmp_path.glob("backup*.json"))) == 2


@pytest.mark.parametrize("fail_on", [1, 3, 8, 15, 25, 32])
def test_rerun_after_a_partial_failure_finishes_the_job(tmp_path, fail_on):
    clean, clean_dir = FakeDiscord(), tmp_path / "clean"
    clean_dir.mkdir()
    apply(clean, clean_dir)

    fake = FakeDiscord(fail_on_write=fail_on)
    code, output = run(fake, "--apply", tmp_path=tmp_path)
    assert code == 1 and "injected failure" in output
    fake.fail_on_write = None
    apply(fake, tmp_path)

    assert fake.structure() == clean.structure()
    # Each welcome posted once, even if the failure came between creating a channel and welcoming it
    assert sorted(len(m) for m in fake.messages.values()) == sorted(len(m) for m in clean.messages.values())


# ---- Onboarding ----

def onboarding_after_apply(tmp_path, *extra):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    before = json.loads(json.dumps(fake.onboarding))
    code, output = run(fake, "--onboarding", *MERGE, *extra, tmp_path=tmp_path)
    assert code == 0, output
    return fake, before, json.loads((tmp_path / "onboarding.json").read_text()), output


# The fixture has two other questions; Discord allows 4 in all, so one is merged into the other
MERGE = ("--merge-question", "How did you hear about us?", "--into", "What do you want to help with?")
GROUP_TITLES = ["Where are you? Europe", "Where are you? Middle East & Asia-Pacific", "Where are you? Americas & Africa"]


def test_onboarding_is_only_written_to_a_file_by_default(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path)

    assert not any(w[0] == "PUT" and w[1].endswith("/onboarding") for w in fake.writes)
    assert fake.onboarding == before
    assert "Remove question 'Which country are you in?'" in output
    assert "Remove question 'More countries'" in output
    assert "Question 'Where are you? Americas & Africa'" in output


def test_onboarding_replaces_country_questions_and_carries_the_rest_over(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path)

    titles = [p["title"] for p in payload["prompts"]]
    assert titles == ["What do you want to help with?"] + GROUP_TITLES
    assert payload["default_channel_ids"] == before["default_channel_ids"]
    assert payload["enabled"] is True and payload["mode"] == 1

    interest = payload["prompts"][0]
    original = before["prompts"][0]
    assert {k: interest[k] for k in ("id", "type", "single_select", "required", "in_onboarding")} == \
        {k: original[k] for k in ("id", "type", "single_select", "required", "in_onboarding")}
    assert interest["options"][0] == {"id": "8001", "title": "Protesting", "description": None,
                                      "channel_ids": original["options"][0]["channel_ids"], "role_ids": [],
                                      "emoji_id": "6100", "emoji_name": "protest", "emoji_animated": False}
    assert interest["options"][1]["emoji_name"] == "✍️" and "emoji_id" not in interest["options"][1]
    # 'A friend' gives no role or channel, so merging drops it with the question
    assert "Drop 'A friend' from 'How did you hear about us?' (gives nothing)" in output
    assert "Remove question 'How did you hear about us?'" in output
    assert len(interest["options"]) == len(original["options"])


def test_onboarding_regional_options(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path)
    africa = next(p for p in payload["prompts"] if p["title"] == "Where are you? Americas & Africa")
    role = {r["name"]: r["id"] for r in fake.roles.values()}

    assert africa["single_select"] is False and africa["required"] is False
    by_title = {o["title"]: o for o in africa["options"]}
    assert list(by_title) == ["Argentina", "Cuba", "Dominican Republic", "United States",
                              "Somewhere else in Americas", "Ethiopia", "Kenya", "Tunisia", "Somewhere else in Africa"]
    assert by_title["Kenya"]["role_ids"] == [role["Kenya"]]
    assert by_title["Kenya"]["channel_ids"] == [fake.channel("kenya")["id"]]
    assert by_title["Kenya"]["id"] == "8005"  # The old option's ID and emoji carry over
    assert by_title["Kenya"]["emoji_name"] == "🇰🇪"
    # A role with no channel: the role plus other-africa
    assert by_title["Ethiopia"]["role_ids"] == [role["Ethiopia"]]
    assert by_title["Ethiopia"]["channel_ids"] == [fake.channel("other-africa")["id"]]
    assert by_title["Ethiopia"]["emoji_name"] == "🇪🇹"
    # No old description, so the country's own name goes there
    assert by_title["Ethiopia"]["description"] == "ኢትዮጵያ"
    assert by_title["Somewhere else in Africa"]["role_ids"] == []
    assert by_title["Somewhere else in Africa"]["channel_ids"] == [fake.channel("other-africa")["id"]]
    ids = [o["id"] for p in payload["prompts"] for o in p["options"]] + [p["id"] for p in payload["prompts"]]
    assert len(ids) == len(set(ids))


def test_onboarding_apply_sends_it_and_a_rerun_keeps_the_same_ids(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path, "--apply-onboarding")

    [put] = [w for w in fake.writes if w[0] == "PUT" and w[1].endswith("/onboarding")]
    assert put[2] == payload
    code, output = run(fake, "--onboarding", "--apply-onboarding", tmp_path=tmp_path)
    assert code == 0, output
    second = [w for w in fake.writes if w[0] == "PUT" and w[1].endswith("/onboarding")][-1][2]
    assert second == payload


def test_onboarding_before_the_restructure_stops(tmp_path):
    fake = FakeDiscord()
    code, output = run(fake, "--onboarding", "--apply-onboarding", tmp_path=tmp_path)
    assert code == 1 and "run --apply before --onboarding" in output
    assert fake.writes == []


# ---- Rollback ----

def test_restore_layout_puts_channels_back(tmp_path):
    fake = FakeDiscord()
    original = load_fixture()
    apply(fake, tmp_path)

    code, output = run(fake, "--restore-layout", str(tmp_path / "backup.json"), "--apply", tmp_path=tmp_path)
    assert code == 0, output
    for before in original["channels"]:
        if before["type"] == 0 and before.get("parent_id") in {c["id"] for c in original["channels"]
                                                               if "COUNTRIES" in c["name"]}:
            after = fake.channels[before["id"]]
            parent = next(c for c in original["channels"] if c["id"] == before["parent_id"])
            assert fake.channels[after["parent_id"]]["name"] == parent["name"], before["name"]
            assert cr.overwrites_key(after["permission_overwrites"]) == \
                cr.overwrites_key(before["permission_overwrites"]), before["name"]
    assert fake.children("🌍 COUNTRIES")[0] == "request-a-country-🌍"


def test_restore_onboarding(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path, "--apply-onboarding")
    code, output = run(fake, "--restore-onboarding", str(tmp_path / "backup.json"), "--apply", tmp_path=tmp_path)
    assert code == 0, output
    assert fake.onboarding == before


# ---- HTTP ----

class Response(io.BytesIO):
    def __init__(self, body, headers=None):
        super().__init__(json.dumps(body).encode())
        self.headers = headers or {}

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


def test_http_waits_out_rate_limits_and_never_shows_the_token(monkeypatch):
    calls, sleeps = [], []

    def urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise urllib.error.HTTPError(request.full_url, 429, "Too Many", {}, io.BytesIO(b'{"retry_after": 1.5}'))
        if len(calls) == 2:
            return Response({"ok": True}, {"X-RateLimit-Remaining": "0", "X-RateLimit-Reset-After": "2"})
        raise urllib.error.HTTPError(request.full_url, 403, "Forbidden", {}, io.BytesIO(b'{"code": 50013}'))

    monkeypatch.setattr(cr.urllib.request, "urlopen", urlopen)
    http = cr.DiscordHTTP("secret-token", sleep=sleeps.append)

    assert http.request("POST", "/x", {"a": 1}) == {"ok": True}
    assert sleeps == [2.0, 2.0]
    assert calls[0].get_header("Authorization") == "Bot secret-token"
    assert calls[0].get_header("X-audit-log-reason") == cr.AUDIT_REASON
    with pytest.raises(cr.DiscordError) as error:
        http.request("GET", "/y")
    assert "secret-token" not in str(error.value) and "50013" in str(error.value)


def test_missing_token(tmp_path):
    lines = []
    assert cr.main([], out=lines.append, env={}) == 2
    assert "DISCORD_TOKEN" in lines[0]


# ---- Found in review ----

def with_channel(name, category="🌍 COUNTRIES", overwrites=()):
    data = load_fixture()
    parent = next(c for c in data["channels"] if c["name"] == category)
    data["channels"].append({"id": "5998", "name": name, "type": 0, "position": 30, "parent_id": parent["id"],
                             "permission_overwrites": list(overwrites), "last_message_id": "1"})
    return data


def test_a_country_channel_without_a_matching_role_still_moves(tmp_path):
    fake = FakeDiscord(with_channel("czechia"))
    output = apply(fake, tmp_path)

    assert fake.category_of("czechia") == "🌍 EUROPE"
    assert "🌍 COUNTRIES" not in {c["name"] for c in fake.channels.values()}
    assert "#czechia has no role named like it" in output
    assert fake.channel("czechia")["id"] not in fake.messages


def test_dry_run_after_apply_has_nothing_left(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    code, output = run(fake, tmp_path=tmp_path)
    assert code == 0 and "No changes needed" in output, output


def test_warns_when_a_role_can_still_send_in_the_locked_channel(tmp_path):
    data = load_fixture()
    request = next(c for c in data["channels"] if c["name"].startswith("request"))
    sm = next(r for r in data["roles"] if r["name"] == "Server Manager")
    request["permission_overwrites"].append({"id": sm["id"], "type": 0, "allow": str(cr.SEND_MESSAGES), "deny": "0"})
    output = apply(FakeDiscord(data), tmp_path)
    assert "still lets Server Manager send messages" in output


def test_onboarding_stops_on_a_country_question_with_a_non_country_role(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    california = next(r for r in fake.roles.values() if r["name"] == "California")
    fake.onboarding["prompts"][1]["options"].append(
        {"id": "8099", "title": "California", "description": None, "role_ids": [california["id"]],
         "channel_ids": [], "emoji": None})
    code, output = run(fake, "--onboarding", tmp_path=tmp_path)
    assert code == 1 and "'California' in 'Which country are you in?'" in output
    assert not (tmp_path / "onboarding.json").exists()


def test_onboarding_lists_dropped_options(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path)
    assert "Drop options that give no role: 'Not listed? Ask here' (in 'More countries')" in output


def test_onboarding_apply_keeps_the_original_backup(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path, "--apply-onboarding")
    original = json.loads((tmp_path / "backup.json").read_text())
    assert "🌍 COUNTRIES" in {c["name"] for c in original["channels"]}
    assert list(tmp_path.glob("backup-onboarding-*.json"))


def test_onboarding_with_plain_apply_is_refused(tmp_path):
    with pytest.raises(SystemExit):
        run(FakeDiscord(), "--onboarding", "--apply", tmp_path=tmp_path)


def test_http_never_retries_a_post_that_may_have_happened(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        raise urllib.error.HTTPError(request.full_url, 502, "Bad Gateway", {}, io.BytesIO(b"oops"))

    monkeypatch.setattr(cr.urllib.request, "urlopen", urlopen)
    with pytest.raises(cr.DiscordError):
        cr.DiscordHTTP("t", sleep=lambda s: None).request("POST", "/channels/1/messages", {"content": "hi"})
    assert len(calls) == 1


def test_http_retries_reads_after_a_timeout(monkeypatch):
    calls = []

    def urlopen(request, timeout):
        calls.append(request)
        if len(calls) == 1:
            raise TimeoutError("read timed out")
        return Response([])

    monkeypatch.setattr(cr.urllib.request, "urlopen", urlopen)
    assert cr.DiscordHTTP("t", sleep=lambda s: None).request("GET", "/x") == []


def test_all_countries_makes_a_channel_for_every_country_role_without_welcoming_empty_ones(tmp_path):
    fake = FakeDiscord()
    code, output = run(fake, "--apply", "--all-countries", tmp_path=tmp_path)
    assert code == 0, output
    # Ethiopia has a role but no members: a channel now, but no welcome
    assert fake.children("🌍 AFRICA") == ["ethiopia", "kenya", "tunisia", "other-africa"]
    assert fake.channel("ethiopia")["id"] not in fake.messages
    welcomed = {fake.channels[cid]["name"] for cid in fake.messages if not fake.channels[cid]["name"].startswith("request")}
    assert welcomed == {"kenya", "russia", "dominican-republic", "tunisia"}
    # Non-country roles still get nothing
    assert not any(c["name"] == "california" for c in fake.channels.values())


def test_all_countries_after_a_first_run_only_adds_the_empty_ones(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    before = {c["id"] for c in fake.channels.values()}
    code, output = run(fake, "--apply", "--all-countries", tmp_path=tmp_path)
    assert code == 0, output
    added = [c["name"] for c in fake.channels.values() if c["id"] not in before]
    assert added == ["ethiopia"]
    assert "Post welcome" not in output


def test_onboarding_over_four_questions_stops_and_says_how_to_merge(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    code, output = run(fake, "--onboarding", tmp_path=tmp_path)
    assert code == 1 and "over Discord's 4" in output and "--merge-question" in output
    assert not (tmp_path / "onboarding.json").exists()


def test_merged_options_keep_their_role_and_can_be_renamed(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    volunteer = next(r for r in fake.roles.values() if r["name"] == "California")["id"]
    fake.onboarding["prompts"][-1]["options"].append(
        {"id": "8098", "title": "Yes", "description": None, "role_ids": [volunteer], "channel_ids": [], "emoji": None})
    code, output = run(fake, "--onboarding", *MERGE, "--rename-option", "Yes=I want to volunteer", tmp_path=tmp_path)
    assert code == 0, output
    payload = json.loads((tmp_path / "onboarding.json").read_text())
    interest = payload["prompts"][0]
    added = next(o for o in interest["options"] if o["title"] == "I want to volunteer")
    assert added["role_ids"] == [volunteer] and added["id"] == "8098"
    assert "How did you hear about us?" not in [p["title"] for p in payload["prompts"]]


def test_a_full_group_lists_countries_with_members_first_then_the_most_populous(tmp_path, monkeypatch):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    monkeypatch.setattr(cr, "MAX_OPTIONS_PER_PROMPT", 6)  # Americas & Africa: room for 4 countries
    code, output = run(fake, "--onboarding", *MERGE, tmp_path=tmp_path)
    assert code == 0, output
    payload = json.loads((tmp_path / "onboarding.json").read_text())
    group = next(p for p in payload["prompts"] if p["title"] == "Where are you? Americas & Africa")
    titles = [o["title"] for o in group["options"]]
    # Members first: Argentina, Cuba, Dominican Republic, Kenya, Tunisia, United States hold roles in the fixture;
    # of those, the most populous four fit
    assert len(titles) == 6 and titles[-1] == "Somewhere else in Africa"
    assert "Ethiopia" not in titles and "not listed, so 'Somewhere else'" in output


def test_default_channels_everyone_cant_see_are_left_out(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    welcome = fake.channel("welcome")
    welcome["permission_overwrites"] = [{"id": fake.guild_id, "type": 0, "allow": "0", "deny": "1024"}]
    fake.onboarding["default_channel_ids"].append("9999")  # A channel the bot can't see
    code, output = run(fake, "--onboarding", *MERGE, tmp_path=tmp_path)
    assert code == 0, output
    payload = json.loads((tmp_path / "onboarding.json").read_text())
    assert welcome["id"] not in payload["default_channel_ids"] and "9999" not in payload["default_channel_ids"]
    assert "Leave out default channels @everyone can't see" in output and "#welcome" in output


def test_members_only_lists_just_countries_someone_holds(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    code, output = run(fake, "--onboarding", *MERGE, "--members-only", tmp_path=tmp_path)
    assert code == 0, output
    payload = json.loads((tmp_path / "onboarding.json").read_text())
    assert [p["title"] for p in payload["prompts"] if p["title"].startswith("Where")] == [
        "Where are you? Europe & Middle East", "Where are you? Americas, Africa & Asia-Pacific"]
    group = payload["prompts"][2]
    titles = [o["title"] for o in group["options"]]
    assert "Ethiopia" not in titles and "Kenya" in titles and "Tunisia" in titles  # Tunisia: a National Leader
    assert titles[-1] == "Somewhere else in Asia-Pacific"
    assert "countries with no members yet use 'Somewhere else'" in output and "answers in all" in output


# ---- Discord's onboarding limits ----

def onboarding_puts(fake):
    return [w[2] for w in fake.writes if w[0] == "PUT" and w[1].endswith("/onboarding")]


def test_regional_questions_are_dropdowns_even_when_the_old_country_ones_were_multiple_choice(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    for prompt in fake.onboarding["prompts"]:
        prompt["type"] = cr.MULTIPLE_CHOICE
    code, output = run(fake, "--onboarding", *MERGE, tmp_path=tmp_path)
    assert code == 0, output
    payload = json.loads((tmp_path / "onboarding.json").read_text())
    assert {p["type"] for p in payload["prompts"] if p["title"].startswith("Where")} == {cr.DROPDOWN}


def test_merging_a_multiple_choice_question_past_12_answers_stops_before_sending(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    channel = fake.channel("welcome")["id"]
    fake.onboarding["prompts"][-1]["options"] += [
        {"id": str(8200 + i), "title": f"Source {i}", "description": None, "role_ids": [], "channel_ids": [channel],
         "emoji": None} for i in range(11)]
    code, output = run(fake, "--onboarding", "--apply-onboarding", *MERGE, tmp_path=tmp_path)
    assert code == 1
    assert "'What do you want to help with?' is a multiple-choice question with 13 answers" in output
    assert onboarding_puts(fake) == []


def test_merge_question_not_found_stops_and_says_why(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path, "--apply-onboarding")
    puts = len(onboarding_puts(fake))
    code, output = run(fake, "--onboarding", "--apply-onboarding", *MERGE, tmp_path=tmp_path)
    assert code == 1
    assert "no question titled 'How did you hear about us?'" in output
    assert "drop --merge-question if it was merged on an earlier run" in output
    assert len(onboarding_puts(fake)) == puts


def test_restore_onboarding_leaves_out_hidden_default_channels(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path, "--apply-onboarding")
    welcome = fake.channel("welcome")
    assert welcome["id"] in before["default_channel_ids"]
    welcome["permission_overwrites"] = [{"id": fake.guild_id, "type": 0, "allow": "0", "deny": "1024"}]
    code, output = run(fake, "--restore-onboarding", str(tmp_path / "backup.json"), "--apply", tmp_path=tmp_path)
    assert code == 0, output
    assert welcome["id"] not in onboarding_puts(fake)[-1]["default_channel_ids"]
    assert "Leave out default channels @everyone can't see" in output and "#welcome" in output


def test_restore_onboarding_checks_discords_limits_before_sending(tmp_path):
    fake = FakeDiscord()
    apply(fake, tmp_path)
    backup = json.loads((tmp_path / "backup.json").read_text())
    backup["onboarding"]["prompts"].append(dict(backup["onboarding"]["prompts"][0], id="7099", title="Fifth"))
    (tmp_path / "backup.json").write_text(json.dumps(backup))
    code, output = run(fake, "--restore-onboarding", str(tmp_path / "backup.json"), "--apply", tmp_path=tmp_path)
    assert code == 1 and "5 questions, over Discord's 4" in output
    assert onboarding_puts(fake) == []


def test_from_file_sends_the_reviewed_file_as_it_is(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path)
    path = tmp_path / "onboarding.json"
    payload["prompts"][1]["title"] = "Where are you? Europe (edited by hand)"
    path.write_text(json.dumps(payload))
    reviewed = path.read_text()
    code, output = run(fake, "--onboarding", "--apply-onboarding", "--from-file", str(path), tmp_path=tmp_path)
    assert code == 0, output
    assert onboarding_puts(fake) == [payload]
    assert path.read_text() == reviewed  # Not rebuilt or rewritten
    assert list(tmp_path.glob("backup-onboarding-*.json"))


def test_from_file_checks_discords_limits_before_sending(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path)
    path = tmp_path / "onboarding.json"
    payload["prompts"][0]["options"] *= 7  # Multiple choice, 14 answers
    path.write_text(json.dumps(payload))
    code, output = run(fake, "--onboarding", "--apply-onboarding", "--from-file", str(path), tmp_path=tmp_path)
    assert code == 1 and "multiple-choice question with 14 answers" in output
    assert onboarding_puts(fake) == []


def test_from_file_with_a_hidden_default_channel_stops(tmp_path):
    fake, before, payload, output = onboarding_after_apply(tmp_path)
    fake.channel("welcome")["permission_overwrites"] = [
        {"id": fake.guild_id, "type": 0, "allow": "0", "deny": "1024"}]
    code, output = run(fake, "--onboarding", "--apply-onboarding", "--from-file", str(tmp_path / "onboarding.json"),
                       tmp_path=tmp_path)
    assert code == 1 and "#welcome" in output
    assert onboarding_puts(fake) == []


def test_from_file_needs_apply_onboarding(tmp_path):
    with pytest.raises(SystemExit):
        run(FakeDiscord(), "--onboarding", "--from-file", "x.json", tmp_path=tmp_path)


# ---- The bot's permissions ----

def test_an_administrator_bot_still_gets_the_role_hierarchy_warning(tmp_path):
    data = load_fixture()
    bot_role = next(r for r in data["roles"] if r["name"] == "PauseBot")
    bot_role["permissions"] = str(int(bot_role["permissions"]) | cr.ADMINISTRATOR)
    next(r for r in data["roles"] if r["name"] == "Dominian Republic")["position"] = bot_role["position"] + 1
    code, output = run(FakeDiscord(data), tmp_path=tmp_path)
    assert code == 0, output
    assert "highest role is below 'Dominian Republic'" in output
    assert "missing" not in output


def test_state_held_counts_members_and_national_leaders():
    state = cr.State("1", [], [], role_counts={"a": 2, "b": 0}, leader_held={"c"})
    assert [state.held({"id": r}) for r in "abcd"] == [True, False, True, False]
