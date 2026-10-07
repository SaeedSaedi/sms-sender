"""One send for several segments (G1 of the 2026-10-05 review). The CLI
sent each segment as its own run, one after another; on the dashboard a
campaign chooses more segments after its own, one test SMS approves them
all, and one send reads every list: a number in two lists gets one SMS,
and each recipient keeps its own list's segment."""
from __future__ import annotations

import pytest

pytest.importorskip("django")

from sms_sender.input_loader import InputError  # noqa: E402
from sms_sender.sendcheck import check_sends  # noqa: E402
from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.campaigns.checks import check_campaign  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.engine import Engine, campaign_db  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.jobs.sandbox import read_outbox  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402

from .test_rounds import run, signed_in  # noqa: E402
from .test_settings_page import FORM  # noqa: E402
from .world import PHONES, TEST_PHONE, build_world  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)  # the worker's heartbeat writes from a thread


@pytest.fixture
def world(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    return build_world(tmp_path)


def another_list(slug: str, name: str, rows: str) -> Segment:
    """A ready segment with vip's columns."""
    segment = Segment.objects.create(
        slug=slug, name=name, status=Segment.Status.READY, columns=["phone", "user_id", "first_name"],
        user_id_column="user_id", token_columns=["first_name"], summary={"valid": rows.count("\n")},
    )
    segment.path.write_text("phone,user_id,first_name\n" + rows, encoding="utf-8")
    return segment


def with_more(campaign: Campaign, *slugs: str) -> Campaign:
    campaign.settings["more_segments"] = list(slugs)
    campaign.save()
    return campaign


def recipient_segments(campaign: Campaign) -> dict[str, str]:
    with StateStore(campaign_db(campaign)) as store:
        rows = store._conn().execute(
            "SELECT phone, segment FROM recipients WHERE phone NOT LIKE 'INVALID:%'"
        ).fetchall()
    return dict(rows)


# ---------- choosing them ----------

def test_the_settings_offer_the_other_ready_segments(world, verified):
    html = signed_in(world.users["operator"], verified).get("/campaigns/fresh/settings/").content.decode()
    box = html.split('id="more-segments"')[1].split("</details>")[0]
    assert 'data-more-segment="vip-2"' in box and 'data-more-segment="plain"' in box
    assert 'value="draft"' not in box  # its columns aren't chosen yet


def test_more_segments_are_saved_in_the_order_they_are_listed(world, verified):
    another_list("vip-3", "مشتریان ویژه ۳", "09120000031,u-31,Reza\n")
    client = signed_in(world.users["operator"], verified)
    response = client.post("/campaigns/fresh/settings/", {**FORM, "more_segments": ["vip-3", "vip", "vip-2"]})
    assert response.status_code == 302
    campaign = Campaign.objects.get(slug="fresh")
    # By name, and never the campaign's own segment again.
    assert campaign.settings["more_segments"] == ["vip-2", "vip-3"]
    assert "more_segments" in AuditEvent.objects.filter(action="campaign_changed").last().detail["changed"]

    client.post("/campaigns/fresh/settings/", FORM)
    assert "more_segments" not in Campaign.objects.get(slug="fresh").settings


def test_a_segment_without_the_columns_the_tokens_use_is_refused(world, verified):
    client = signed_in(world.users["operator"], verified)
    html = client.post("/campaigns/fresh/settings/", {**FORM, "more_segments": ["plain"]}).content.decode()
    assert 'id="id_more_segments_error"' in html
    error = html.split('id="id_more_segments_error"')[1].split("</")[0]
    assert "فقط شماره" in error
    assert "more_segments" not in Campaign.objects.get(slug="fresh").settings


# ---------- one approval for them all ----------

def test_the_approval_covers_every_segment_and_its_file(world):
    campaign = world.campaigns["fresh"]
    alone = services.settings_hash(campaign)
    campaign.settings["more_segments"] = []
    assert services.settings_hash(campaign) == alone  # earlier approvals keep their hash
    campaign.settings["more_segments"] = ["vip-2"]
    both = services.settings_hash(campaign)
    assert both != alone
    Segment.objects.filter(slug="vip-2").update(version=1)  # its file was replaced
    assert services.settings_hash(campaign) != both


def test_the_check_reads_every_segment_and_counts_a_number_once(world):
    # vip-2 also holds one of vip's numbers, with the same user ID.
    Segment.objects.get(slug="vip-2").path.write_text(
        f"phone,user_id,first_name\n{PHONES[1]},u-1,Sara\n09120000021,u-21,Sara\n", encoding="utf-8",
    )
    result = check_campaign(with_more(world.campaigns["fresh"], "vip-2"))
    assert result.ok, result.problems
    assert result.segments == [("مشتریان ویژه", 6), ("مشتریان ویژه ۲", 1)]
    assert (result.valid, result.to_send) == (7, 7)


def test_a_more_segment_that_isnt_ready_stops_the_check(world):
    result = check_campaign(with_more(world.campaigns["fresh"], "draft"))
    assert result.problems == ["more_segment_missing"] and result.missing_segments == ["draft"]


def test_the_engine_reads_every_segment_in_order(world):
    campaign = with_more(world.campaigns["fresh"], "vip-2", "plain")
    runner = Engine().runner(campaign, reporter=None)
    assert [(p.segment, p.user_id_column) for p in runner.parts] == [
        ("vip", "user_id"), ("vip-2", "user_id"), ("plain", None),
    ]
    with pytest.raises(InputError, match="draft"):
        Engine().runner(with_more(campaign, "draft"), reporter=None)


# ---------- one send ----------

def test_one_test_sms_and_one_send_reach_every_segment(world, verified):
    campaign = with_more(world.campaigns["fresh"], "vip-2")
    client = signed_in(world.users["operator"], verified)
    page = client.get("/campaigns/fresh/").content.decode()
    assert 'id="check-segments"' in page and 'id="send-segments"' in page

    client.post("/campaigns/fresh/test/")
    test = run(Job.Kind.TEST)
    assert test.state == Job.State.DONE, test.last_error
    client.post("/campaigns/fresh/approve/", {"job": test.pk})
    client.post("/campaigns/fresh/send/", {"when": "now"})
    send = run(Job.Kind.SEND)
    assert send.state == Job.State.DONE, send.last_error

    # One test SMS, then every number of both lists once.
    assert sorted(sms["phone"] for sms in read_outbox()) == sorted(
        [TEST_PHONE, *PHONES, "09120000021", "09120000022"],
    )
    assert send.result["sent"] == len(PHONES) + 2
    segments = recipient_segments(campaign)
    assert (segments[PHONES[0]], segments["09120000021"]) == ("vip", "vip-2")
    assert check_sends(StateStore(campaign_db(campaign))).ok


def test_a_send_whose_more_segment_isnt_ready_stops_before_anything(world):
    campaign = with_more(world.campaigns["approved"], "vip-2")
    Segment.objects.filter(slug="vip-2").update(status=Segment.Status.DRAFT)  # being replaced
    Job.objects.create(campaign=campaign, kind=Job.Kind.SEND, settings_hash=services.settings_hash(campaign))
    send = run(Job.Kind.SEND)
    assert send.state == Job.State.FAILED
    assert send.result == {"stop_reason": "input_unreadable"}
    assert read_outbox() == []


# ---------- what uses a segment ----------

def test_a_segment_sent_after_another_stays_while_a_campaign_uses_it(world, verified):
    with_more(world.campaigns["fresh"], "vip-2")
    client = signed_in(world.users["operator"], verified)
    page = client.get("/segments/vip-2/").content.decode()
    assert 'href="/campaigns/fresh/"' in page  # used by
    assert 'action="/segments/vip-2/delete/"' not in page
    client.post("/segments/vip-2/delete/")
    assert Segment.objects.filter(slug="vip-2").exists()


def test_its_file_stays_while_a_send_that_reads_it_is_on_its_way(world, verified):
    with_more(world.campaigns["sending"], "vip-2")
    client = signed_in(world.users["operator"], verified)
    response = client.get("/segments/vip-2/replace/")
    assert response.status_code == 302 and response["Location"] == "/segments/vip-2/"


# ---------- another round, to several ----------

def test_another_round_may_go_to_several_segments_in_one_send(world, verified):
    another_list("vip-3", "مشتریان ویژه ۳", "09120000031,u-31,Reza\n")
    client = signed_in(world.users["operator"], verified)
    form = client.get("/campaigns/completed/").content.decode().split('id="segment-form"')[1].split("</form>")[0]
    assert 'type="checkbox" name="segment" value="vip-2"' in form and 'value="vip-3"' in form

    client.post("/campaigns/completed/segment/", {"segment": ["vip-3", "vip-2"]})
    settings = Campaign.objects.get(slug="completed").settings
    assert (settings["segment"], settings["more_segments"]) == ("vip-2", ["vip-3"])
    assert AuditEvent.objects.get(action="segment_switched").detail == {
        "before": "vip", "after": "vip-2", "more": ["vip-3"],
    }


def test_another_round_needs_a_segment_chosen(world, verified):
    client = signed_in(world.users["operator"], verified)
    response = client.post("/campaigns/completed/segment/", {})
    assert response.status_code == 302
    assert Campaign.objects.get(slug="completed").settings["segment"] == "vip"
    assert "دست‌کم یک گروه مخاطبان" in client.get("/campaigns/completed/").content.decode()


def test_a_round_to_one_segment_drops_the_earlier_more_segments(world, verified):
    with_more(world.campaigns["completed"], "plain")
    signed_in(world.users["operator"], verified).post("/campaigns/completed/segment/", {"segment": "vip-2"})
    assert "more_segments" not in Campaign.objects.get(slug="completed").settings


# ---------- the side card ----------

def test_the_live_poll_keeps_what_the_campaign_sends(world, verified):
    """The poll renders the side card too: its segment and tokens stay."""
    client = signed_in(world.users["operator"], verified)
    live = client.get("/campaigns/sending/live/").content.decode()
    assert "مشتریان ویژه" in live and "token10" in live
    with_more(world.campaigns["sending"], "vip-2")
    live = client.get("/campaigns/sending/live/").content.decode()
    assert 'id="send-segments"' in live and "مشتریان ویژه ۲" in live
