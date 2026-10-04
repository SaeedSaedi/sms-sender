"""Links in a real run: every link exists before the first SMS, no SMS
goes out unless every link is ready, and a resumed run reuses the links
it already made."""
from __future__ import annotations

import threading
from pathlib import Path

import pytest
from click.testing import CliRunner

from sms_sender import cli as cli_module
from sms_sender.cli import cli
from sms_sender.input_loader import TokenColumns
from sms_sender.links import LinkSettings, LinkStage, LinkStageResult
from sms_sender.runner import Runner, format_report
from sms_sender.shortlink import ShlinkClient, ShlinkError, ShlinkHaltError
from sms_sender.state import PENDING, SENT, CampaignMismatchError, StateStore

from .test_links import BASE, FakeShlink
from .test_runner import FakeSender, RecordingReporter

A, B, C = "09120000001", "09120000002", "09120000003"


def links(**kw) -> LinkSettings:
    kw.setdefault("destination", "https://kifpool.me/offer")
    kw.setdefault("token", "token3")
    return LinkSettings(**kw)


def write(tmp_path: Path, text: str, name: str = "vip-2.txt") -> Path:
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return p


def runner(tmp_path, inp, *, state=None, sender=None, shlink=None, **kw) -> Runner:
    return Runner(
        input_path=inp, state=state or StateStore(tmp_path / "s.db"),
        sender=sender or FakeSender(), workers=1, reporter=RecordingReporter(),
        campaign="coin-7", links=kw.pop("links", links()), link_client=shlink or FakeShlink(),
        link_rate_per_sec=0, **kw,
    )


def test_every_sms_carries_its_own_link_and_links_come_first(tmp_path):
    events: list[str] = []

    class Shlink(FakeShlink):
        def create(self, **kw):
            events.append("link")
            return super().create(**kw)

    class Sender(FakeSender):
        def send(self, phone, tokens=None):
            events.append("sms")
            return super().send(phone, tokens)

    sender = Sender()
    summary = runner(tmp_path, write(tmp_path, f"{A}\n{B}\n"), sender=sender, shlink=Shlink()).run()
    assert events == ["link", "link", "sms", "sms"]
    sent = {phone: tokens["token3"] for phone, tokens in sender.tokens.items()}
    assert set(sent) == {A, B} and sent[A] != sent[B]
    assert all(link.startswith(f"{BASE}/c") for link in sent.values())
    assert (summary.links_ready, summary.links_created) == (2, 2)
    assert "links             2  (2 created this run)" in format_report(summary)


def test_no_sms_goes_out_unless_every_link_is_ready(tmp_path):
    shlink = FakeShlink()
    shlink.fail.append(("utm", ShlinkError(None, None, "retries exhausted")))
    sender = FakeSender()
    state = StateStore(tmp_path / "s.db")
    summary = runner(tmp_path, write(tmp_path, f"{A}\n{B}\n"), state=state, sender=sender,
                     shlink=shlink).run()
    assert sender.calls == []
    assert summary.halted and not summary.stopped
    assert state.counts() == {PENDING: 2}


def test_a_bad_shlink_key_halts_before_any_sms(tmp_path):
    shlink = FakeShlink()
    shlink.fail.append(("utm", ShlinkHaltError(401, "invalid-api-key", "no such key")))
    sender = FakeSender()
    summary = runner(tmp_path, write(tmp_path, f"{A}\n"), sender=sender, shlink=shlink).run()
    assert summary.halted and sender.calls == []


def test_the_approval_test_sms_carries_its_own_link(tmp_path):
    sent: list[str] = []

    class Recording(FakeSender):
        def send(self, phone, tokens=None):
            sent.append(tokens["token3"])
            return super().send(phone, tokens)

    state = StateStore(tmp_path / "s.db")
    runner(
        tmp_path, write(tmp_path, f"{A}\n"), state=state, sender=Recording(),
        approval_test_number=A, approval_prompt=lambda *_: True,
    ).run()
    # The test SMS and the real one both went to A, each with its own link.
    links_by_key = {k: v.short_url for k, v in state.get_links([A, f"test:{A}"]).items()}
    assert sent == [links_by_key[f"test:{A}"], links_by_key[A]]
    assert sent[0] != sent[1]


def test_a_resumed_run_reuses_its_links(tmp_path):
    inp = write(tmp_path, f"{A}\n{B}\n{C}\n")
    state, shlink = StateStore(tmp_path / "s.db"), FakeShlink()

    class CancelAfterOne(FakeSender):
        def send(self, phone, tokens=None):
            result = super().send(phone, tokens)
            first.cancel()
            return result

    first = runner(tmp_path, inp, state=state, sender=CancelAfterOne(), shlink=shlink)
    assert first.run().stopped
    second_sender = FakeSender()
    summary = runner(tmp_path, inp, state=state, sender=second_sender, shlink=shlink).run()
    assert len(shlink.created) == 3  # no new links for the rest
    assert summary.links_created == 0 and summary.links_ready == 2
    assert state.counts() == {SENT: 3}
    links_by_phone = {k: v.short_url for k, v in state.get_links([A, B, C]).items()}
    for phone, tokens in second_sender.tokens.items():
        assert tokens["token3"] == links_by_phone[phone]


def test_links_and_csv_tokens_travel_together(tmp_path):
    inp = write(tmp_path, f"phone,name\n{A},Sara\n", "in.csv")
    sender = FakeSender()
    runner(tmp_path, inp, sender=sender, token_columns=TokenColumns(columns={"token10": "name"})).run()
    assert sender.tokens[A]["token10"] == "Sara"
    assert sender.tokens[A]["token3"].startswith(f"{BASE}/")


def test_segment_links_are_shared(tmp_path):
    sender = FakeSender()
    runner(tmp_path, write(tmp_path, f"{A}\n{B}\n"), sender=sender,
           links=links(strategy="segment")).run()
    assert sender.tokens[A]["token3"] == sender.tokens[B]["token3"]


def test_another_destination_after_sending_is_refused(tmp_path):
    from sms_sender.runner import campaign_settings
    from sms_sender.sender import SenderConfig

    state = StateStore(tmp_path / "s.db")
    inp = write(tmp_path, f"{A}\n")
    cfg = SenderConfig(api_key="k", template="t")
    runner(tmp_path, inp, state=state, settings=campaign_settings(cfg, None, links())).run()
    other = links(destination="https://kifpool.me/another")
    with pytest.raises(CampaignMismatchError, match="links changed"):
        runner(tmp_path, inp, state=state, links=other,
               settings=campaign_settings(cfg, None, other)).run()


def test_a_cancel_during_the_link_stage_is_a_stop_not_a_halt(tmp_path):
    holder: dict = {}

    class CancellingShlink(FakeShlink):
        def create(self, **kw):
            holder["runner"].cancel()
            raise ShlinkError(None, None, "interrupted")

    r = runner(tmp_path, write(tmp_path, f"{A}\n{B}\n"), shlink=CancellingShlink())
    holder["runner"] = r
    summary = r.run()
    assert summary.stopped and not summary.halted


def test_a_recipient_without_its_link_is_never_claimed(tmp_path, monkeypatch):
    def partial(self, recipients, test_phone=None):
        return LinkStageResult(needed=1, created=1, extended=0, tokens={A: f"{BASE}/x1"})
    monkeypatch.setattr(LinkStage, "run", partial)
    state, sender = StateStore(tmp_path / "s.db"), FakeSender()
    runner(tmp_path, write(tmp_path, f"{A}\n{B}\n"), state=state, sender=sender).run()
    assert sender.calls == [A]
    assert state.counts() == {SENT: 1, PENDING: 1}  # B untouched, not in_flight


def test_links_need_a_campaign_and_a_client(tmp_path):
    with pytest.raises(ValueError, match="link client and a campaign"):
        Runner(input_path=write(tmp_path, f"{A}\n"), state=StateStore(tmp_path / "s.db"),
               sender=FakeSender(), links=links())


# ---------- CLI ----------

def _cli(tmp_path, monkeypatch, *args: str, captured: dict | None = None):
    from .test_cli import _stub_make_runner

    monkeypatch.chdir(tmp_path)
    Path("in.txt").write_text(f"{A}\n", encoding="utf-8")
    monkeypatch.setattr(cli_module, "load_api_key", lambda: "k")
    monkeypatch.setattr(cli_module, "make_runner", _stub_make_runner(captured if captured is not None else {}))
    return CliRunner().invoke(cli, [
        "send", "--input", "in.txt", "--template", "t", "--log-file", "test.log", *args,
    ])


def test_send_wires_the_link_settings(tmp_path, monkeypatch):
    captured: dict = {}
    result = _cli(
        tmp_path, monkeypatch, "--campaign", "coin-7", "--link-url", "https://kifpool.me/offer",
        "--link-token", "token3", "--link-strategy", "segment", "--link-rate", "20/s",
        captured=captured,
    )
    assert result.exit_code == 0, result.output
    assert captured["links"] == LinkSettings(
        destination="https://kifpool.me/offer", token="token3", strategy="segment",
    )
    assert isinstance(captured["link_client"], ShlinkClient)
    assert captured["link_client"].base_url == "https://shlink.invalid/u"  # conftest: never the real one
    assert captured["link_rate_per_sec"] == 20.0


@pytest.mark.parametrize("args, message", [
    (["--link-url", "https://kifpool.me/o", "--link-token", "token3"], "needs --campaign"),
    (["--campaign", "c", "--link-url", "https://kifpool.me/o"], "needs --link-token"),
    (["--campaign", "c", "--link-token", "token3"], "only applies together with --link-url"),
    (["--campaign", "c", "--link-url", "https://kifpool.me/o", "--link-token", "token3",
      "--token3", "x"], "token3 carries the link"),
    (["--campaign", "c", "--link-url", "https://evil.example/o", "--link-token", "token3"],
     "isn't an allowed destination domain"),
    (["--campaign", "c", "--link-url", "http://kifpool.me/o", "--link-token", "token3"],
     "must start with https://"),
])
def test_send_refuses_bad_link_flags(tmp_path, monkeypatch, args, message):
    result = _cli(tmp_path, monkeypatch, *args)
    assert result.exit_code == 2
    assert message in result.output


def test_send_needs_the_shlink_key(tmp_path, monkeypatch):
    monkeypatch.delenv("SHLINK_API_KEY")
    result = _cli(tmp_path, monkeypatch, "--campaign", "c", "--link-url",
                  "https://kifpool.me/o", "--link-token", "token3")
    assert result.exit_code == 2 and "SHLINK_API_KEY is not set" in result.output


def test_dry_run_shows_the_long_url_without_calling_shlink(tmp_path, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("dry-run must not call Shlink")
    monkeypatch.setattr(ShlinkClient, "create", no_network)
    inp = write(tmp_path, f"{A}\n", "vip-2.txt")
    result = CliRunner().invoke(cli, [
        "dry-run", "--input", str(inp), "--campaign", "coin-7",
        "--link-url", "https://kifpool.me/offer", "--link-token", "token3",
    ])
    assert result.exit_code == 0, result.output
    assert "links: recipient strategy, token3 carries https://shlink.invalid/u/<short-code>" in result.output
    assert "long URL   https://kifpool.me/offer?utm_source=sms&utm_medium=sms" \
           "&utm_campaign=coin-7&utm_content=vip-2&r=" in result.output
    assert "tags       campaign-coin-7" in result.output


def test_preview_shows_a_placeholder_and_never_sends_one(tmp_path):
    result = CliRunner().invoke(cli, [
        "preview", "--phone", A, "--template", "t", "--link-token", "token3",
        "--link-format", "code",
    ])
    assert result.exit_code == 0, result.output
    assert "token3=<short-code>" in result.output
    refused = CliRunner().invoke(cli, [
        "preview", "--phone", A, "--template", "t", "--link-token", "token3", "--send",
    ])
    assert refused.exit_code == 2 and "--send can't include a link" in refused.output


def test_status_counts_links(tmp_path):
    from sms_sender.state import LinkRow

    db = tmp_path / "s.db"
    state = StateStore(db)
    state.add_links([LinkRow(key=A, ref="r1", long_url="u1", title="t", tags=("x",),
                             valid_until="2026-10-11T08:00:00+00:00"),
                     LinkRow(key=B, ref="r2", long_url="u2", title="t", tags=("x",),
                             valid_until="2026-10-11T08:00:00+00:00")])
    state.mark_link_ready(A, "c1", f"{BASE}/c1")
    result = CliRunner().invoke(cli, ["status", "--state", str(db)])
    assert "links      ready 1 · pending 1" in result.output


def test_link_stage_threads_share_nothing_unsafe(tmp_path):
    """Many links over several workers: every row ends ready exactly once."""
    shlink = FakeShlink()
    phones = [f"0912{i:07d}" for i in range(60)]
    inp = write(tmp_path, "\n".join(phones) + "\n")
    state = StateStore(tmp_path / "s.db")
    r = runner(tmp_path, inp, state=state, shlink=shlink, link_workers=6)
    summary = r.run()
    assert summary.links_ready == 60 and len(shlink.created) == 60
    assert state.link_counts() == {"ready": 60}
    assert threading.active_count() < 50
