"""Plan 05, P2: where a campaign stands and what comes next — the stage on
the campaign list, the overview and the campaign's page; the steps; each
job's result and notes in Persian; window-paused sends going on by
themselves."""
from __future__ import annotations

from datetime import timedelta

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402
from django.utils import timezone  # noqa: E402

from sms_sender_web.campaigns import lifecycle as lc  # noqa: E402
from sms_sender_web.campaigns.present import notes, result_line, top_errors  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.jobs.worker import Worker  # noqa: E402

from .world import build_world  # noqa: E402

pytestmark = pytest.mark.django_db


@pytest.fixture
def world(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    return build_world(tmp_path)


@pytest.fixture
def operator_client(world, verified):
    client = Client()
    verified(client, world.users["operator"])
    return client


def stage(world, slug: str) -> str:
    return lc.lifecycle(world.campaigns[slug]).stage


def test_each_campaign_has_one_stage(world):
    assert stage(world, "draft") == lc.DRAFT
    assert stage(world, "fresh") == lc.READY
    assert stage(world, "awaiting") == lc.AWAITING
    assert stage(world, "approved") == lc.APPROVED
    assert stage(world, "sending") == lc.SENDING
    assert stage(world, "paused") == lc.PAUSED
    assert stage(world, "halted") == lc.STOPPED
    assert stage(world, "completed") == lc.COMPLETED


def test_a_newer_test_after_a_send_decides_the_stage(world):
    """After a send, a new test SMS (another segment, a changed message)
    takes over: the campaign is being tested again."""
    campaign = world.campaigns["completed"]
    Job.objects.create(campaign=campaign, kind=Job.Kind.TEST, state=Job.State.RUNNING,
                       params={"test_number": "09120000099"})
    assert lc.lifecycle(campaign).stage == lc.TESTING


def test_changed_settings_take_an_approved_campaign_back_to_ready(world):
    campaign = world.campaigns["approved"]
    campaign.settings["template"] = "another-template"
    campaign.save()
    life = lc.lifecycle(campaign)
    assert life.stage == lc.READY and life.step == 2


def test_a_send_for_other_settings_belongs_to_an_earlier_round(world):
    """The list changed since the send (another segment), or the message
    did: the campaign starts again from the check and the test SMS."""
    campaign = world.campaigns["completed"]
    assert stage(world, "completed") == lc.COMPLETED
    campaign.settings["segment"] = "another"
    campaign.save()
    assert stage(world, "completed") == lc.READY
    # A send still on its way is never an earlier round.
    sending = world.campaigns["sending"]
    sending.settings["segment"] = "another"
    sending.save()
    assert stage(world, "sending") == lc.SENDING


def test_a_failed_test_is_ready_again_and_says_so(world):
    campaign = world.campaigns["fresh"]
    Job.objects.create(campaign=campaign, kind=Job.Kind.TEST, state=Job.State.FAILED,
                       result={"stop_reason": "test_failed", "stop_fields": {"code": 424}})
    life = lc.lifecycle(campaign)
    assert life.stage == lc.READY and life.test_failed


def test_the_window_paused_a_send_or_an_operator_did(world):
    send = Job.objects.get(campaign=world.campaigns["paused"], kind=Job.Kind.SEND)
    assert not lc.lifecycle(world.campaigns["paused"]).paused_by_window
    send.result = {"stop_reason": "window_closed", "stop_fields": {"start": "08:00", "end": "21:00"}}
    send.save()
    assert lc.lifecycle(world.campaigns["paused"]).paused_by_window


def test_the_campaign_page_shows_the_stage_the_steps_and_the_next_step(world, operator_client):
    html = operator_client.get("/campaigns/awaiting/").content.decode()
    assert '<span class="pill pill-warning">در انتظار تأیید</span>' in html
    assert "پیامک آزمایشی را روی گوشی خود ببینید و سپس آن را تأیید یا رد کنید." in html
    # Step 3 of 5 (the test SMS) is the current one; steps 1 and 2 are done.
    assert html.count('class="is-done"') == 2 and 'class="is-current" aria-current="step"' in html
    assert "بله، تأیید می‌کنم" in html


def test_a_stopped_send_explains_itself_where_you_look(world, operator_client):
    html = operator_client.get("/campaigns/halted/").content.decode()
    current = html.split('id="current-step"')[1].split("</section>")[0]
    assert "کاوه‌نگار خطای ۴۱۸ برگرداند" in current and 'role="alert"' in current
    assert "ادامه ارسال" in current  # and the way on, in the same place


def test_a_finished_send_shows_its_results_and_whats_left(world, operator_client):
    html = operator_client.get("/campaigns/completed/").content.decode()
    current = html.split('id="current-step"')[1].split("</section>")[0]
    assert "<dt>مدت</dt><dd class=\"num\"><bdi dir=\"ltr\">۱:۳۵</bdi></dd>" in current
    assert "خطای ۴۱۱: شماره گیرنده نامعتبر است." in current
    assert "وضعیت ۱ گیرنده نامعلوم است" in current and 'id="followup-reconcile"' in current
    assert "برای ۱ گیرنده پیامکی ارسال نشد" in current and 'id="followup-send"' in current
    # The history: what each job did, and its notes in Persian only.
    assert "۱ گیرنده در فهرست عدم ارسال است و پیامکی دریافت نمی‌کند." in html
    assert "engine English" not in html


def test_notes_and_results_read_in_persian(world):
    finished = Job.objects.get(campaign=world.campaigns["completed"], kind=Job.Kind.SEND)
    assert notes(finished) == ["۱ گیرنده در فهرست عدم ارسال است و پیامکی دریافت نمی‌کند."]
    assert top_errors(finished.result) == [("خطای ۴۱۱: شماره گیرنده نامعتبر است.", 1)]
    reconcile = Job(kind=Job.Kind.RECONCILE, state=Job.State.DONE,
                    result={"sent": 3, "requeued": 1, "needs_review": 0, "deferred": 2})
    assert result_line(reconcile) == (
        "ارسال‌شده ۳ · قابل ارسال دوباره ۱ · نیازمند بررسی ۰ · هنوز بررسی‌نشده ۲"
    )
    test_note = finished.events.create(key="test_tokens_from", data={"phone": "09120001234"})
    assert test_note.pk and "۰۹۱۲*****۳۴" in notes(finished)[1]  # numbers stay masked


def test_the_overview_counts_stages_and_lists_what_needs_attention(world, operator_client):
    html = operator_client.get("/campaigns/").content.decode()
    tiles = html.split('class="figures overview"')[1].split("</ul>")[0]
    assert '<span class="figure-label">متوقف با خطا</span><span class="figure-value num">۱</span>' in tiles
    assert '<span class="figure-label">در انتظار تأیید</span><span class="figure-value num">۱</span>' in tiles
    attention = html.split('class="card step attention"')[1].split("</section>")[0]
    assert "کاوه‌نگار خطای ۴۱۸ برگرداند" in attention  # the stopped one, with its reason
    assert "وضعیت ۱ گیرنده نامعلوم است" in attention  # the finished one's leftovers
    assert '<span class="pill pill-success">آماده ارسال</span>' in html


def test_a_send_the_window_paused_goes_on_when_the_window_opens(world):
    send = Job.objects.get(campaign=world.campaigns["paused"], kind=Job.Kind.SEND)
    send.result = {"stop_reason": "window_closed", "stop_fields": {"start": "08:00", "end": "21:00"}}
    send.save()
    operator_paused = Job.objects.get(campaign=world.campaigns["sending"], kind=Job.Kind.SEND)
    operator_paused.state = Job.State.PAUSED
    operator_paused.save()

    assert Worker(worker_id="w1").resume_when_window_opens() == 1  # the world's window is open
    send.refresh_from_db()
    operator_paused.refresh_from_db()
    assert send.state == Job.State.QUEUED and send.events.filter(key="window_resumed").exists()
    assert operator_paused.state == Job.State.PAUSED  # an operator's pause waits for them


def test_a_send_waits_while_its_window_is_closed(world):
    campaign: Campaign = world.campaigns["paused"]
    now = timezone.localtime()
    closed = f"{(now + timedelta(hours=2)):%H:%M}-{(now + timedelta(hours=3)):%H:%M}"
    campaign.settings["send_window"] = closed
    campaign.save()
    send = Job.objects.get(campaign=campaign, kind=Job.Kind.SEND)
    send.result = {"stop_reason": "outside_window"}
    send.save()
    assert Worker(worker_id="w1").resume_when_window_opens() == 0
    send.refresh_from_db()
    assert send.state == Job.State.PAUSED
