"""Plan 05, P6: a 100,000-recipient campaign stays quick. The report, its
filters and search, the downloads, analytics and the campaign's ready step
each answer within BUDGET seconds (they took 0.1-0.8 s on the development
machine). Opt-in: pytest -m perf."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.campaigns.models import MessageTemplate  # noqa: E402
from sms_sender_web.jobs.models import Campaign  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402

pytestmark = [pytest.mark.perf, pytest.mark.django_db]

N = 100_000
BUDGET = 2.0  # seconds a page may take


@pytest.fixture
def big(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()
    phones = [f"0912{i:07d}" for i in range(N)]
    segment = Segment.objects.create(slug="big", name="big", status=Segment.Status.READY,
                                     columns=["phone", "user_id", "first_name"], user_id_column="user_id",
                                     token_columns=["first_name"], summary={"valid": N})
    segment.path.parent.mkdir(parents=True, exist_ok=True)
    segment.path.write_text("phone,user_id,first_name\n" + "".join(f"{p},u-{i},Ali\n" for i, p in enumerate(phones)),
                            encoding="utf-8")
    store = StateStore(tmp_path / "db" / "big.db")
    store.bind_campaign("big", {"template": "t", "tokens": {"token": "x"}, "token_columns": {"token10": "first_name"},
                                "value_maps": {}})
    store.upsert_pending([(p, p) for p in phones], segment="big")
    conn, now, sent = store._conn(), time.time(), phones[: N * 8 // 10]
    conn.execute("BEGIN")
    conn.executemany("UPDATE recipients SET status='sent', sent_at=?, cost=3020, delivery_status=? WHERE phone=?",
                     [(now, 10 if i % 3 else 11, p) for i, p in enumerate(sent)])
    conn.executemany("INSERT INTO attempts (phone, kind, outcome, started_at, message_id) "
                     "VALUES (?, 'send', 'accepted', ?, ?)", [(p, now, 10_000 + i) for i, p in enumerate(sent)])
    conn.execute("COMMIT")
    MessageTemplate.objects.create(name="t", text="%token10 عزیز %token")
    settings_ = {"segment": "big", "input": str(segment.path), "user_id_column": "user_id", "template": "t",
                 "tokens": {"token": "x"}, "token_columns": {"token10": "first_name"}, "value_maps": {},
                 "send_window": "", "workers": 2}
    Campaign.objects.create(slug="big", name="big", settings=settings_)
    Campaign.objects.create(slug="fresh", name="fresh", settings=settings_)
    return phones


@pytest.fixture
def admin_client(make_user, verified):
    client = Client()
    verified(client, make_user("admin1", "admin"))
    return client


@pytest.mark.parametrize("label, call", [
    ("home", lambda c, p: c.get("/")),
    ("report", lambda c, p: c.get("/reports/big/")),
    ("report, filtered", lambda c, p: c.get("/reports/big/", {"status": "sent", "delivery": "not_delivered"})),
    ("report, a far page", lambda c, p: c.get("/reports/big/", {"page": "500"})),
    ("report, one number", lambda c, p: c.post("/reports/big/", {"q": p[54321]})),
    ("recipients download", lambda c, p: c.get("/reports/big/recipients.csv")),
    ("analytics", lambda c, p: c.get("/analytics/")),
    ("campaign page", lambda c, p: c.get("/campaigns/big/")),
    ("ready step: check and preview", lambda c, p: c.get("/campaigns/fresh/")),
    ("ready step: one number", lambda c, p: c.post("/campaigns/fresh/preview/", {"preview": p[777]},
                                                    HTTP_HX_REQUEST="true")),
])
def test_a_hundred_thousand_recipients_stay_quick(big, admin_client, label, call):
    started = time.perf_counter()
    response = call(admin_client, big)
    took = time.perf_counter() - started
    assert response.status_code == 200, label
    assert took < BUDGET, f"{label}: {took:.2f}s"
