"""Clicks: counts from Shlink (bots excluded) stored per link, reports per
segment with "missing user ID" kept apart, and the two exports — the
backend's without phone numbers."""
from __future__ import annotations

import csv
from pathlib import Path

from click.testing import CliRunner

from sms_sender.cli import cli
from sms_sender.clicks import (
    ATTRIBUTION_HEADER,
    attribution_rows,
    click_report,
    clicker_rows,
    describe,
    sync_clicks,
)
from sms_sender.shortlink import LinkVisits, ShlinkClient, ShlinkHaltError
from sms_sender.state import LinkRow, StateStore

BASE = "https://kifpool.me/u"
A, B, C, D = "09120000001", "09120000002", "09120000003", "09120000004"


def link(key: str, ref: str | None) -> LinkRow:
    return LinkRow(key=key, ref=ref, long_url=f"https://kifpool.me/o?r={ref}", title="t",
                   tags=("campaign-coin-7",), valid_until="2026-10-11T08:00:00+00:00")


def campaign_db(path: Path) -> StateStore:
    """A, B (vip-2) and C (new-users) sent with links of their own; B has no
    user ID; D was never sent."""
    state = StateStore(path)
    state.bind_campaign("coin-7", None)
    state.upsert_pending([(A, A), (B, B)], segment="vip-2")
    state.upsert_pending([(C, C), (D, D)], segment="new-users")
    state.assign_user_ids({A: "u-1", C: "u-3", D: "u-4"})
    for i, phone in enumerate([A, B, C, D], start=1):
        state.add_links([link(phone, f"ref{i:07d}")])
        state.mark_link_ready(phone, f"c{i}", f"{BASE}/c{i}")
    for i, phone in enumerate([A, B, C], start=1):
        state.claim(phone, link_key=phone)  # each was sent their own link
        state.mark_sent(phone, message_id=i, status_code=200)
    state.record_delivery({A: 10}, checked_at=0)
    return state


class Visits:
    def __init__(self, counts: dict[str, int], times=()):
        self.counts = counts
        self.times = list(times)  # when each visit happened, for clicks over time
        self.tags: list[str] = []
        self.since: list = []

    def visits_by_tag(self, tag):
        self.tags.append(tag)
        return [LinkVisits(code, total=n + 5, non_bots=n) for code, n in self.counts.items()]

    def visit_times(self, tag, *, since=None):
        self.since.append(since)
        return [t for t in self.times if since is None or t >= since]


def test_sync_stores_non_bot_clicks_per_link(tmp_path):
    state = campaign_db(tmp_path / "s.db")
    source = Visits({"c1": 2, "c2": 1, "c3": 0, "not-ours": 9})
    result = sync_clicks(state, source, "coin-7", now=lambda: 1000.0)
    assert source.tags == ["campaign-coin-7"]
    assert (result.links, result.updated, result.clicks) == (4, 3, 12)
    assert state.last_click_sync() == 1000.0


def test_report_per_segment_keeps_missing_user_ids_apart(tmp_path):
    state = campaign_db(tmp_path / "s.db")
    sync_clicks(state, Visits({"c1": 2, "c2": 1, "c3": 0}), "coin-7")
    segments, campaign_clicks = click_report(state)
    by_name = {s.segment: s for s in segments}
    vip = by_name["vip-2"]
    assert (vip.sent, vip.clicks, vip.clicked) == (2, 3, 2)
    assert (vip.missing_user_id, vip.clicked_missing_user_id) == (1, 1)
    new = by_name["new-users"]
    assert (new.sent, new.clicks, new.clicked, new.missing_user_id) == (1, 0, 0, 0)
    assert campaign_clicks == 0
    assert describe(segments, campaign_clicks) == [
        "  new-users: sent 1, clicks 0, clicked 0 (0.0%)",
        "  vip-2: sent 2, clicks 3, clicked 2 (100.0%); missing user ID: 1 (1 clicked)",
    ]


def test_shared_links_count_for_the_segment_not_for_people(tmp_path):
    state = StateStore(tmp_path / "s.db")
    state.upsert_pending([(A, A), (B, B)], segment="vip-2")
    state.upsert_pending([(C, C)], segment="vip-2")
    state.add_links([link("segment:vip-2:p1", None), link("campaign:p1", None)])
    state.mark_link_ready("segment:vip-2:p1", "s1", f"{BASE}/s1")
    state.mark_link_ready("campaign:p1", "k1", f"{BASE}/k1")
    for i, (phone, key) in enumerate(
        [(A, "segment:vip-2:p1"), (B, "segment:vip-2:p1"), (C, "campaign:p1")], start=1,
    ):
        state.claim(phone, link_key=key)
        state.mark_sent(phone, message_id=i, status_code=200)
    sync_clicks(state, Visits({"s1": 7, "k1": 4}), "coin-7")
    (vip,), campaign_clicks = click_report(state)
    # The segment link counts once for the segment, however many got it.
    assert (vip.sent, vip.clicks, vip.clicked, vip.clicked_missing_user_id) == (3, 7, None, None)
    assert campaign_clicks == 4
    assert list(clicker_rows(state)) == []  # nobody can be named from a shared link
    assert list(attribution_rows(state)) == []


def test_attribution_has_no_phone_numbers(tmp_path):
    state = campaign_db(tmp_path / "s.db")
    sync_clicks(state, Visits({"c1": 2}), "coin-7")
    rows = list(attribution_rows(state))
    assert len(rows) == 3  # D wasn't sent
    by_ref = {r[0]: dict(zip(ATTRIBUTION_HEADER, r)) for r in rows}
    a = by_ref["ref0000001"]
    assert (a["user_id"], a["user_id_status"], a["segment"]) == ("u-1", "ok", "vip-2")
    assert (a["short_url"], a["delivery"], a["clicks"]) == (f"{BASE}/c1", "delivered", 2)
    assert a["accepted_at"].endswith("+00:00")
    b = by_ref["ref0000002"]
    assert (b["user_id"], b["user_id_status"], b["delivery"]) == ("", "missing user ID", "not checked")
    assert "0912" not in repr(rows)


def test_clickers_are_named_most_clicks_first(tmp_path):
    state = campaign_db(tmp_path / "s.db")
    sync_clicks(state, Visits({"c1": 2, "c2": 5}), "coin-7")
    assert list(clicker_rows(state)) == [
        [B, "", "missing user ID", "vip-2", "ref0000002", 5],
        [A, "u-1", "ok", "vip-2", "ref0000001", 2],
    ]


# ---------- CLI ----------

def test_clicks_command_syncs_and_reports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("data/db").mkdir(parents=True)
    campaign_db(Path("data/db/coin-7.db"))
    seen = Visits({"c1": 2, "c2": 1})
    monkeypatch.setattr(ShlinkClient, "visits_by_tag", lambda self, tag: seen.visits_by_tag(tag))
    result = CliRunner().invoke(cli, ["clicks", "--campaign", "coin-7", "--log-file", "t.log"])
    assert result.exit_code == 0, result.output
    assert "2 link(s) at Shlink, 3 click(s) (bots excluded)" in result.output
    assert "vip-2: sent 2, clicks 3, clicked 2 (100.0%); missing user ID: 1 (1 clicked)" in result.output

    status = CliRunner().invoke(cli, ["status", "--campaign", "coin-7"])
    assert "clicks     3 (bots excluded, synced" in status.output
    assert "clicked 2 of 3 recipients" in status.output


def test_clicks_command_stops_on_a_bad_key(tmp_path, monkeypatch):
    db = tmp_path / "s.db"
    campaign_db(db)

    def refuse(self, tag):
        raise ShlinkHaltError(401, "invalid-api-key", "no such key")
    monkeypatch.setattr(ShlinkClient, "visits_by_tag", refuse)
    result = CliRunner().invoke(cli, ["clicks", "--state", str(db), "--log-file", str(tmp_path / "t.log")])
    assert result.exit_code == 2 and "Shlink didn't answer" in result.output


def test_clicks_command_needs_a_db(tmp_path):
    result = CliRunner().invoke(cli, ["clicks", "--state", str(tmp_path / "nope.db"),
                                      "--log-file", str(tmp_path / "t.log")])
    assert result.exit_code == 2 and "No state DB" in result.output
    assert not (tmp_path / "nope.db").exists()


def test_exports_land_in_data_exports(tmp_path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    Path("data/db").mkdir(parents=True)
    state = campaign_db(Path("data/db/coin-7.db"))
    sync_clicks(state, Visits({"c1": 2}), "coin-7")

    result = CliRunner().invoke(cli, ["export-attribution", "--campaign", "coin-7"])
    assert result.exit_code == 0, result.output
    path = Path("data/exports/coin-7-attribution.csv")
    assert f"Wrote 3 rows to {path}" in result.output
    text = path.read_text(encoding="utf-8")
    assert text.splitlines()[0] == ",".join(ATTRIBUTION_HEADER)
    assert "0912" not in text

    result = CliRunner().invoke(cli, ["export-clickers", "--campaign", "coin-7"])
    assert result.exit_code == 0, result.output
    rows = list(csv.reader(Path("data/exports/coin-7-clickers.csv").open(encoding="utf-8")))
    assert rows[1][:3] == [A, "u-1", "ok"] and len(rows) == 2


# ---------- clicks over time (schema v7) ----------

def test_clicks_are_kept_per_hour_and_never_counted_twice(tmp_path):
    from datetime import datetime, timedelta, timezone

    state = campaign_db(tmp_path / "s.db")
    t0 = datetime(2026, 10, 5, 9, 40, tzinfo=timezone.utc)
    source = Visits({"c1": 3}, times=[t0, t0 + timedelta(minutes=10), t0 + timedelta(minutes=30)])
    assert sync_clicks(state, source, "coin-7").hours == 3
    nine, ten = int(t0.replace(minute=0).timestamp()), int(t0.replace(minute=0).timestamp()) + 3600
    assert state.click_hours() == [(nine, 2), (ten, 1)]
    # The next sync reads from the start of the latest hour again, and counts
    # that hour afresh: the 10:10 visit isn't counted twice.
    source.times.append(t0 + timedelta(hours=1, minutes=5))
    sync_clicks(state, source, "coin-7")
    assert source.since[-1] == datetime.fromtimestamp(ten, tz=timezone.utc)
    assert state.click_hours() == [(nine, 2), (ten, 2)]


def test_counts_still_stand_when_shlink_cant_say_when(tmp_path):
    from sms_sender.shortlink import ShlinkError

    class NoTimes(Visits):
        def visit_times(self, tag, *, since=None):
            raise ShlinkError(404, "not-found", "no such endpoint")

    state = campaign_db(tmp_path / "s.db")
    result = sync_clicks(state, NoTimes({"c1": 2}), "coin-7")
    assert (result.clicks, result.hours) == (2, 0) and state.click_hours() == []
    segments, _ = click_report(state)
    assert {seg.segment: seg.clicks for seg in segments}["vip-2"] == 2
