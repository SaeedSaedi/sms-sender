"""Plan 05, P6: a 100,000-recipient campaign stays quick. The report, its
filters and search, the downloads, analytics (with its series and
audience) and the campaign's ready step
each answer within BUDGET seconds (they took 0.1-0.8 s on the development
machine). Opt-in: pytest -m perf."""
from __future__ import annotations

import time

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender.state import StateStore  # noqa: E402
from sms_sender_web.campaigns.models import MessageTemplate, Preset  # noqa: E402
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
    preset = Preset.objects.create(slug="bigp", name="big", settings=settings_)
    Campaign.objects.create(slug="fresh", name="fresh", settings=settings_, preset=preset)
    return phones


@pytest.fixture
def admin_client(make_user, verified):
    client = Client()
    verified(client, make_user("admin1", "admin"))
    return client


def _in_series(client, path):
    """The big campaign as an alert of its preset (plan 06, L5), so the
    series pages and the report's comparison read 100,000 recipients."""
    Campaign.objects.filter(slug="big").update(preset=Preset.objects.get(slug="bigp"))
    return client.get(path)


def _audience(client, phones):
    """Two big segments to overlap, read cold every time (no cache)."""
    from sms_sender_web.reports import insights

    if not Segment.objects.filter(slug="half").exists():
        half = Segment.objects.create(slug="half", name="half", status=Segment.Status.READY, summary={"valid": N // 2})
        half.path.write_text("phone\n" + "".join(f"{p}\n" for p in phones[N // 4: N * 3 // 4]), encoding="utf-8")
    insights._CACHE.clear()
    return client.get("/analytics/audience/", {"s": ["big", "half"]})


@pytest.mark.parametrize("label, call", [
    ("control room", lambda c, p: c.get("/")),
    ("campaign list", lambda c, p: c.get("/campaigns/")),
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
    # The composer (plan 06, L3): the page, what typing and the segments ask for, and an alert's page.
    ("composer", lambda c, p: c.get("/compose/bigp/")),
    ("composer: the message", lambda c, p: c.post("/compose/bigp/preview/?part=message",
                                                  {"segments": ["big"], "value_token": "y"})),
    ("composer: who gets it", lambda c, p: c.post("/compose/bigp/preview/?part=counts",
                                                  {"segments": ["big"], "value_token": "y"})),
    ("an alert's page", lambda c, p: c.get("/compose/c/fresh/")),
    # Insights (plan 06, L5).
    ("series", lambda c, p: _in_series(c, "/analytics/series/")),
    ("a series", lambda c, p: _in_series(c, "/analytics/series/bigp/")),
    ("report of an alert", lambda c, p: _in_series(c, "/reports/big/")),
    ("audience", _audience),
])
def test_a_hundred_thousand_recipients_stay_quick(big, admin_client, label, call):
    """The best of three: a slow CI machine only ever adds time, and the
    first call also pays for one-off work (compiling templates)."""
    times = []
    for _ in range(3):
        started = time.perf_counter()
        response = call(admin_client, big)
        times.append(time.perf_counter() - started)
        assert response.status_code == 200, label
    assert min(times) < BUDGET, f"{label}: {', '.join(f'{t:.2f}s' for t in times)}"
