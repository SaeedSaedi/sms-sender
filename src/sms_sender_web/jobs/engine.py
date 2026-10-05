"""The CLI's engine, built for a campaign: the dashboard runs exactly what
`sms-sender send` runs. Tests swap this class for one with fakes."""
from __future__ import annotations

from pathlib import Path

from django.conf import settings as django_settings

from sms_sender import input_loader
from sms_sender.config import load_api_key
from sms_sender.input_loader import TokenColumns
from sms_sender.links import DEFAULT_RATE as DEFAULT_LINK_RATE
from sms_sender.links import LinkSettings
from sms_sender.rate import parse_rate
from sms_sender.runner import Reporter, Runner, make_runner
from sms_sender.sender import TOKEN_MAX_SPACES, Sender, SenderConfig
from sms_sender.shortlink import ShlinkClient, load_shlink_config
from sms_sender.state import StateStore
from sms_sender.window import DEFAULT_WINDOW, parse_window

from ..suppression.service import phones_for
from .models import Campaign
from .sandbox import SandboxKavenegar, SandboxShlink


def campaign_db(campaign: Campaign) -> Path:
    return Path(django_settings.SMS_SENDER_DB_DIR) / f"{campaign.slug}.db"


def _timeout(campaign: Campaign | None) -> float:
    """The campaign's timeout for calls to Kavenegar and Shlink (the CLI's
    --timeout), else the default."""
    return float(((campaign.settings or {}).get("timeout") if campaign else None) or 15.0)


class Engine:
    def state(self, campaign: Campaign) -> StateStore:
        return StateStore(campaign_db(campaign))

    def sender(self, campaign: Campaign | None = None) -> Sender:
        """For lookups only (delivery, reconciliation): no template needed.
        A campaign's own timeout (advanced settings) applies to its lookups."""
        if django_settings.SANDBOX:
            return SandboxKavenegar()
        return Sender(SenderConfig(api_key=load_api_key(), template="", timeout=_timeout(campaign)))

    def link_client(self, campaign: Campaign | None = None) -> ShlinkClient:
        if django_settings.SANDBOX:
            return SandboxShlink()
        return ShlinkClient(load_shlink_config(timeout=_timeout(campaign)))

    def runner(
        self, campaign: Campaign, reporter: Reporter, *,
        test_number: str | None = None, cost_per_sms: int | None = None, smoke_test: bool = False,
        allow_settings_change: bool = False,
    ) -> Runner:
        """A run for the campaign's settings (the CLI's flags). With
        `test_number`, a test run: everything up to one SMS to that number,
        then stop. Otherwise a send, given the approved test's cost per SMS."""
        s = campaign.settings
        tokens = {name: s.get("tokens", {}).get(name) for name in TOKEN_MAX_SPACES}
        sender_cfg = SenderConfig(
            api_key="sandbox" if django_settings.SANDBOX else load_api_key(),
            template=s["template"], **tokens,
            timeout=float(s.get("timeout", 15.0)),
            max_attempts=int(s.get("max_attempts", 5)),
            backoff_max=float(s.get("backoff_max", 30.0)),
        )
        token_columns = (
            TokenColumns(columns=dict(s["token_columns"]), value_maps=dict(s.get("value_maps", {})))
            if s.get("token_columns") else None
        )
        links = LinkSettings(**s["links"]) if s.get("links") else None
        # The dashboard's suppression list (global, and this campaign's own),
        # plus any opt-out files in the settings.
        opt_out: set[str] = set(phones_for(campaign))
        for path in s.get("opt_out", []):
            opt_out.update(r.phone for r in input_loader.load(path).valid)
        return make_runner(
            input_path=s["input"],
            db_path=campaign_db(campaign),
            sender_cfg=sender_cfg,
            workers=int(s.get("workers", 5)),
            rate_per_sec=parse_rate(s.get("rate")),
            token_columns=token_columns,
            campaign=campaign.slug,
            opt_out=frozenset(opt_out) or None,
            send_window=parse_window(s.get("send_window", DEFAULT_WINDOW)),
            user_id_column=s.get("user_id_column"),
            segment=s.get("segment"),
            links=links,
            link_client=self.link_client(campaign) if links else None,
            link_rate_per_sec=parse_rate(s.get("link_rate", DEFAULT_LINK_RATE)),
            reporter=reporter,
            install_signal_handlers=False,  # the worker handles signals
            approval_test_number=test_number,
            test_only=test_number is not None,
            smoke_test=smoke_test,
            allow_settings_change=allow_settings_change,
            cost_per_sms=cost_per_sms,
            make_sender=SandboxKavenegar if django_settings.SANDBOX else Sender,
        )
