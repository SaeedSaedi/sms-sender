"""A small, made-up dashboard with a campaign in every state, for the
parity test and the browser tests. Fake numbers only (0912000xxxx).

build_world(data_dir) → World; then World.pages maps a page's name to its
URL and who opens it: a role ("viewer", "operator", "admin"), "<user>:password"
for a session that hasn't passed the second step, or None (signed out).
The operator and the admin have a linked authenticator app; the newcomer,
an operator too, has none yet."""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import timedelta
from pathlib import Path

from django.contrib.auth import get_user_model
from django.utils import timezone
from django_otp.plugins.otp_totp.models import TOTPDevice

from sms_sender.clicks import tehran_hour
from sms_sender.state import StateStore
from sms_sender_web.accounts.models import Profile
from sms_sender_web.accounts.roles import set_role
from sms_sender_web.audit.record import record
from sms_sender_web.campaigns.models import MessageTemplate, Preset
from sms_sender_web.jobs import services
from sms_sender_web.jobs.engine import campaign_db
from sms_sender_web.jobs.models import Campaign, Job
from sms_sender_web.segments.models import Segment
from sms_sender_web.suppression.models import Suppression

PASSWORD = "a-long-test-password-1"


def open_window() -> str:
    """A sending window that's open now and for hours either way. A fixed
    00:00-23:59 is closed for the last minute of the day; this one never is
    while a test runs. Windows may cross midnight."""
    from sms_sender.window import now_tehran

    now = now_tehran()
    return f"{now - timedelta(hours=6):%H:%M}-{now + timedelta(hours=6):%H:%M}"

TEST_PHONE = "09120000099"
PHONES = [f"0912000{i:04d}" for i in range(1, 7)]

SETTINGS = {
    "segment": "vip",
    "user_id_column": "user_id",
    "template": "coin-price",
    "tokens": {"token": "نفت"},
    "token_columns": {"token10": "first_name"},
    "value_maps": {},
    "links": {
        "destination": "https://kifpool.me/wallet", "token": "token20", "format": "url",
        "strategy": "recipient", "expiry_days": 7,
        "utm_source": "sms", "utm_medium": "sms", "utm_campaign": None, "utm_content": None,
    },
    "send_window": "",  # set to an always-open window by _campaign()
    "rate": None,
    "workers": 2,
}


# Page name → (path, who opens it).
PAGES: dict[str, tuple[str, str | None]] = {
    "login": ("/login/", None),
    "home": ("/", "viewer"),  # the control room
    "campaigns": ("/campaigns/", "viewer"),
    "notifications": ("/notifications/", "admin"),
    "calendar": ("/calendar/", "viewer"),
    "segment.list": ("/segments/", "viewer"),
    "segment.detail": ("/segments/vip/", "viewer"),
    "segment.upload": ("/segments/upload/", "operator"),
    "segment.map": ("/segments/draft/columns/", "operator"),
    "suppression": ("/suppression/", "operator"),
    "campaign.new": ("/campaigns/new/", "operator"),
    "campaign.import": ("/campaigns/import/", "admin"),
    "campaign.settings": ("/campaigns/fresh/settings/", "operator"),
    "campaign.fresh": ("/campaigns/fresh/", "operator"),
    "campaign.awaiting": ("/campaigns/awaiting/", "operator"),
    "campaign.approved": ("/campaigns/approved/", "operator"),
    "campaign.scheduled": ("/campaigns/scheduled/", "operator"),
    "campaign.duplicate": ("/campaigns/completed/duplicate/", "operator"),
    "campaign.sending": ("/campaigns/sending/", "operator"),
    "campaign.paused": ("/campaigns/paused/", "operator"),
    "campaign.halted": ("/campaigns/halted/", "operator"),
    "campaign.halted.admin": ("/campaigns/halted/", "admin"),  # changing a sent message
    "campaign.completed": ("/campaigns/completed/", "operator"),
    "campaign.completed.admin": ("/campaigns/completed/", "admin"),  # every status it may queue again
    "campaign.draft": ("/campaigns/draft-1/", "operator"),
    "report": ("/reports/completed/", "viewer"),
    "analytics": ("/analytics/", "viewer"),
    "analytics.series": ("/analytics/series/", "viewer"),
    "analytics.series.detail": ("/analytics/series/coin-price/", "operator"),
    "analytics.audience": ("/analytics/audience/", "viewer"),
    "api.tokens": ("/api-tokens/", "admin"),
    "system": ("/system/", "admin"),
    "backups": ("/system/backups/", "admin"),
    "templates": ("/templates/", "viewer"),
    "template.new": ("/templates/new/", "operator"),
    "status": ("/status/", "viewer"),
    "status.admin": ("/status/", "admin"),  # the hold on all sending
    "numbers": ("/numbers/", "operator"),
    "help": ("/help/", "viewer"),
    "account": ("/account/", "operator"),
    "password": ("/password/", "operator"),
    "users": ("/users/", "admin"),
    "activity": ("/activity/", "admin"),
    "two_factor": ("/2fa/", "operator:password"),
    "two_factor_setup": ("/2fa/setup/", "newcomer:password"),
    "forbidden": ("/campaigns/fresh/settings/", "viewer"),
    "not_found": ("/campaigns/nothing-here/", "viewer"),
    # Presets and the composer (plan 06, L3).
    "presets": ("/presets/", "viewer"),
    "preset.new": ("/presets/new/", "operator"),
    "preset.edit": ("/presets/coin-price/", "operator"),
    "compose.start": ("/compose/", "operator"),
    "compose": ("/compose/coin-price/", "operator"),
    "compose.fresh": ("/compose/c/fresh/", "operator"),
    "compose.awaiting": ("/compose/c/awaiting/", "operator"),
    "compose.approved": ("/compose/c/approved/", "operator"),
}


@dataclass
class World:
    users: dict[str, object] = field(default_factory=dict)
    campaigns: dict[str, Campaign] = field(default_factory=dict)
    pages: dict[str, tuple[str, str | None]] = field(default_factory=lambda: dict(PAGES))


def _user(name: str, role: str | None, test_phone: str = ""):
    user = get_user_model().objects.create_user(username=name, password=PASSWORD)
    set_role(user, role)
    if test_phone:
        Profile.objects.create(user=user, test_phone=test_phone)
    return user


def _campaign(slug: str, name: str, segment: Segment, *, db_rows: dict[str, str] | None = None) -> Campaign:
    campaign = Campaign.objects.create(
        slug=slug, name=name, settings={**SETTINGS, "input": str(segment.path), "send_window": open_window()},
    )
    if db_rows is not None:
        store = StateStore(campaign_db(campaign))
        store.upsert_pending([(phone, phone) for phone in db_rows], segment=segment.slug)
        for n, (phone, status) in enumerate(db_rows.items()):
            if status == "sent":
                store.claim(phone)
                store.mark_sent(phone, 1000 + n, 200, 3020)
            elif status in ("failed_permanent", "failed_retriable"):
                store.claim(phone)
                store.mark_failed(phone, 411, "[411] invalid receptor", permanent=status == "failed_permanent")
            elif status == "unknown":
                store.claim(phone)
                store.mark_unknown(phone, "outcome unknown: read timed out")
    return campaign


def _test_job(campaign: Campaign, operator, *, approved: bool) -> Job:
    now = timezone.now()
    return Job.objects.create(
        campaign=campaign, kind=Job.Kind.TEST, state=Job.State.DONE, requested_by=operator,
        params={"test_number": TEST_PHONE, "parts": 2}, settings_hash=services.settings_hash(campaign),
        result={"cost_per_sms": 3020, "estimate": 3020 * len(PHONES), "credit": 264_731_842,
                "links_ready": 1, "cost": 3020},  # the test SMS's own link only
        started_at=now, finished_at=now,
        decision=Job.Decision.APPROVED if approved else "", decided_by=operator if approved else None,
        decided_at=now if approved else None,
    )


def build_world(data_dir: Path) -> World:
    world = World()
    (data_dir / "db").mkdir(parents=True, exist_ok=True)
    (data_dir / "segments").mkdir(parents=True, exist_ok=True)

    viewer = world.users["viewer"] = _user("viewer1", "viewer")
    operator = world.users["operator"] = _user("operator1", "operator", TEST_PHONE)
    admin = world.users["admin"] = _user("admin1", "admin")
    world.users["newcomer"] = _user("newcomer1", "operator")
    for user in (operator, admin):
        TOTPDevice.objects.create(user=user, name="authenticator", confirmed=True)

    vip = Segment.objects.create(
        slug="vip", name="مشتریان ویژه", original_name="vip.csv", status=Segment.Status.READY,
        columns=["phone", "user_id", "first_name"], user_id_column="user_id",
        token_columns=["first_name"], uploaded_by=operator,
        summary={"rows": len(PHONES) + 1, "valid": len(PHONES), "invalid": 1, "duplicates": 0,
                 "missing_user_id": 1, "conflicts": 0, "suppressed": 0,
                 "invalid_sample": [{"value": "*******", "reason": "invalid_phone"}]},
    )
    vip.path.write_text(
        "phone,user_id,first_name\n"
        + "".join(f"{p},{'' if i == 0 else f'u-{i}'},Ali\n" for i, p in enumerate(PHONES))
        + "not-a-number,u-9,Sara\n",
        encoding="utf-8",
    )
    # Two more lists: one with the same columns, so a finished campaign can
    # send its message there too, and one with numbers only.
    for slug, name, columns, token_columns in (
        ("vip-2", "مشتریان ویژه ۲", ["phone", "user_id", "first_name"], ["first_name"]),
        ("plain", "فقط شماره", ["phone"], []),
    ):
        more = Segment.objects.create(
            slug=slug, name=name, original_name=f"{slug}.csv", status=Segment.Status.READY,
            columns=columns, token_columns=token_columns, uploaded_by=operator,
            user_id_column="user_id" if "user_id" in columns else "",
            summary={"rows": 2, "valid": 2, "invalid": 0, "duplicates": 0, "missing_user_id": 0,
                     "conflicts": 0, "suppressed": 0, "invalid_sample": []},
        )
        more.path.write_text(
            ",".join(columns) + "\n" + "".join(
                ",".join({"phone": p, "user_id": f"u-{p[-2:]}", "first_name": "Sara"}[c] for c in columns) + "\n"
                for p in ("09120000021", "09120000022")
            ),
            encoding="utf-8",
        )
    draft = Segment.objects.create(slug="draft", name="فهرست تازه", original_name="new.csv",
                                   uploaded_by=operator)
    draft.upload_path.write_text("mobile,id,name\n09120000001,u1,Ali\n09120000002,u2,Sara\n", encoding="utf-8")

    rows = {PHONES[0]: "sent", PHONES[1]: "sent", PHONES[2]: "failed_permanent"}
    rows.update({phone: "pending" for phone in PHONES[3:]})
    tested = {phone: "pending" for phone in PHONES}  # as a test SMS leaves them: nothing sent
    campaigns = world.campaigns
    campaigns["fresh"] = _campaign("fresh", "قیمت سکه", vip)
    campaigns["approved"] = _campaign("approved", "نفت خام", vip, db_rows=tested)
    _test_job(campaigns["approved"], operator, approved=True)
    campaigns["awaiting"] = _campaign("awaiting", "در انتظار تأیید", vip)
    _test_job(campaigns["awaiting"], operator, approved=False)

    now = timezone.now()
    for slug, name, state, result, progress, control in (
        ("sending", "در حال ارسال", Job.State.RUNNING, {},
         {"total": len(PHONES), "processed": 3, "sent": 2, "failed_permanent": 1}, ""),
        ("paused", "متوقف موقت", Job.State.PAUSED, {}, {"total": len(PHONES), "processed": 3, "sent": 3}, ""),
        ("halted", "توقف با خطا", Job.State.FAILED,
         {"stop_reason": "provider_halt", "stop_fields": {"code": 418}, "sent": 2}, {}, ""),
    ):
        campaign = campaigns[slug] = _campaign(slug, name, vip, db_rows=rows)
        _test_job(campaign, operator, approved=True)
        Job.objects.create(
            campaign=campaign, kind=Job.Kind.SEND, state=state, requested_by=operator,
            settings_hash=services.settings_hash(campaign), result=result, progress=progress,
            control=control, started_at=now, finished_at=now if state == Job.State.FAILED else None,
        )

    # A send set for tomorrow, to one recipient first.
    scheduled = campaigns["scheduled"] = _campaign("scheduled", "ارسال فردا", vip, db_rows=tested)
    _test_job(scheduled, operator, approved=True)
    Job.objects.create(
        campaign=scheduled, kind=Job.Kind.SEND, state=Job.State.QUEUED, requested_by=operator,
        settings_hash=services.settings_hash(scheduled), params={"cost_per_sms": 3020, "smoke_test": True},
        not_before=now + timedelta(days=1),
    )

    # A finished send with something left to do: an unknown outcome, one
    # not sent, one rejected (Kavenegar 411), and the engine's notes.
    done = campaigns["completed"] = _campaign(
        "completed", "پایان‌یافته", vip,
        db_rows={**rows, PHONES[3]: "unknown", PHONES[4]: "failed_retriable"},
    )
    _test_job(done, operator, approved=True)
    done_store = StateStore(campaign_db(done))
    done_store.record_delivery({PHONES[0]: 10}, checked_at=0)
    hour = tehran_hour(timezone.now())
    done_store.replace_click_hours(0, {hour - 5 * 3600: 2, hour - 3 * 3600: 1, hour - 2 * 3600: 4})
    finished = Job.objects.create(
        campaign=done, kind=Job.Kind.SEND, state=Job.State.DONE, requested_by=operator,
        settings_hash=services.settings_hash(done), started_at=now, finished_at=now,
        result={"sent": 2, "failed_permanent": 1, "failed_retriable": 1, "unknown": 1, "cost": 6040,
                "elapsed_sec": 95.0, "top_errors": [["[411] invalid receptor", 1]]},
    )
    finished.events.create(key="suppressed", text="1 recipient(s) are on the opt-out list", data={"n": 1})
    finished.events.create(key="note", text="engine English, never shown")
    draft_campaign = Campaign.objects.create(slug="draft-1", name="پیش‌نویس", settings={"segment": "vip"})
    campaigns["draft"] = draft_campaign

    MessageTemplate.objects.create(
        name="coin-price", text="%token10 عزیز، قیمت %token امروز اعلام شد: %token20\nلغو۱۱",
        note="قیمت روزانه", updated_by=operator,
    )
    # A preset, and three campaigns made from it (so their composer pages
    # exist), without adding a campaign: the other pages' counts stay.
    preset = Preset.objects.create(
        slug="coin-price", name="قیمت سکه", settings={**SETTINGS, "input": str(vip.path), "send_window": open_window()},
        labels={"token": "نام کالا"}, last_values={"token": "طلا"}, created_by=operator,
    )
    Campaign.objects.filter(slug__in=["fresh", "awaiting", "approved"]).update(preset=preset)
    Suppression.objects.create(phone="09120000050", note="درخواست مشتری")
    record("campaign_created", user=operator, campaign="approved")
    record("segment_uploaded", user=operator, segment="vip", file="vip.csv")

    assert viewer and draft  # built for the pages above
    return world
