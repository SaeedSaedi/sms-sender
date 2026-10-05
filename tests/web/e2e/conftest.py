"""Browser tests (plan 05): the real pages in Google Chrome, served by the
live server, in sandbox mode (nothing is sent). Opt-in, because they need
the `e2e` extra and Chrome:

    pip install -e ".[dev,web,e2e]"
    pytest -m e2e

Set E2E_SHOTS=<folder> to also save a screenshot of every page at every width."""
from __future__ import annotations

import os
import threading
from pathlib import Path

import pytest

pytest.importorskip("django")
sync_api = pytest.importorskip("playwright.sync_api")

from django.conf import settings as django_settings  # noqa: E402
from django.db import connection  # noqa: E402
from django.test import Client  # noqa: E402
from django.utils import translation  # noqa: E402
from django.utils.translation import gettext  # noqa: E402
from django_otp import DEVICE_ID_SESSION_KEY  # noqa: E402
from django_otp.plugins.otp_totp.models import TOTPDevice  # noqa: E402

# Playwright's sync API keeps an event loop in this thread, and Django's ORM
# refuses to run next to one unless told it's safe. Tests only.
os.environ.setdefault("DJANGO_ALLOW_ASYNC_UNSAFE", "true")

AXE = Path(__file__).parent / "vendor" / "axe.min.js"
expect = sync_api.expect  # Playwright's retrying assertions, for the test modules


def fa(msgid: str) -> str:
    """The Persian text a page shows for this message id."""
    with translation.override("fa"):
        return gettext(msgid)


@pytest.fixture(scope="session")
def browser():
    with sync_api.sync_playwright() as p:
        try:
            browser = p.chromium.launch(channel="chrome")
        except Exception as e:  # noqa: BLE001 — no Google Chrome on this machine
            pytest.skip(f"Google Chrome isn't available: {e}")
        yield browser
        browser.close()


@pytest.fixture
def sandbox(settings, tmp_path, transactional_db):
    """Sandbox mode with its own data folder; the live server sees it too."""
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()
    return tmp_path


def session_cookie(user, *, second_step: bool) -> str:
    """A signed-in session for this user, as the browser's cookie value.
    `second_step`: through two-step verification too, for the roles that
    have it (viewers have none)."""
    from sms_sender_web.accounts.roles import needs_two_factor

    client = Client()
    client.force_login(user)
    if second_step and needs_two_factor(user):
        device = TOTPDevice.objects.get(user=user, confirmed=True)
        session = client.session
        session[DEVICE_ID_SESSION_KEY] = device.persistent_id
        session.save()
    return client.cookies[django_settings.SESSION_COOKIE_NAME].value


@pytest.fixture
def open_as(browser, live_server):
    """open_as(user_or_None, path, width, second_step=True, color_scheme="light")
    → a Playwright page at that viewport width, signed in as the user."""
    contexts = []

    def open_(user, path: str, width: int = 1366, *, second_step: bool = True, height: int = 900,
              color_scheme: str = "light"):
        context = browser.new_context(viewport={"width": width, "height": height}, locale="fa-IR",
                                      color_scheme=color_scheme)
        contexts.append(context)
        if user is not None:
            context.add_cookies([{
                "name": django_settings.SESSION_COOKIE_NAME,
                "value": session_cookie(user, second_step=second_step),
                "url": live_server.url,
            }])
        page = context.new_page()
        page.goto(live_server.url + path)
        return page

    yield open_
    for context in contexts:
        try:
            context.close()
        except Exception:  # noqa: BLE001 — a test closed it already
            pass


@pytest.fixture
def sandbox_worker(sandbox):
    """The real worker, in a thread, running jobs as they're queued."""
    from sms_sender_web.jobs.worker import Worker

    stop = threading.Event()
    worker = Worker(worker_id="e2e:1", heartbeat_sec=0.2, stop=stop)

    def loop():
        try:
            worker.run_forever(poll_sec=0.2)
        finally:
            connection.close()

    thread = threading.Thread(target=loop, daemon=True)
    thread.start()
    yield worker
    stop.set()
    thread.join(timeout=30)


def axe_violations(page) -> list[dict]:
    """axe-core's findings on the page as it is now (tests only; never shipped)."""
    page.add_script_tag(path=str(AXE))
    return page.evaluate(
        """async () => (await axe.run(document, {resultTypes: ["violations"]})).violations
            .map(v => ({id: v.id, impact: v.impact, targets: v.nodes.map(n => n.target.join(" "))}))"""
    )


def overflow(page) -> int:
    """How many pixels the page is wider than the window (0: fits)."""
    return page.evaluate("document.documentElement.scrollWidth - document.documentElement.clientWidth")


def small_targets(page) -> list[str]:
    """Controls smaller than 24 × 24 CSS px (WCAG 2.2 AA, 2.5.8): buttons,
    fields, selects, summaries and button-like links. Inline links in a
    sentence are exempt, and so is a checkbox inside its label (the label is
    the target)."""
    return page.evaluate(
        """() => {
          const out = [];
          const picked = document.querySelectorAll(
            'button, select, textarea, summary, a.button, ' +
            'input:not([type=hidden]):not([type=checkbox]):not([type=radio]), ' +
            'input[type=checkbox]:not(label input), input[type=radio]:not(label input)');
          for (const el of picked) {
            const box = el.getBoundingClientRect();
            const style = getComputedStyle(el);
            if (!box.width || !box.height || style.visibility === "hidden") continue;
            if (el.closest("[hidden], dialog:not([open])")) continue;
            if (el.classList.contains("visually-hidden") || el.classList.contains("file-input")) continue;
            if (box.width < 24 || box.height < 24) {
              const text = (el.innerText || el.value || el.getAttribute("aria-label") || el.name || "").trim().slice(0, 30);
              out.push(`${el.tagName.toLowerCase()}${el.className ? "." + [...el.classList].join(".") : ""} "${text}" ${Math.round(box.width)}x${Math.round(box.height)}`);
            }
          }
          return out;
        }"""
    )
