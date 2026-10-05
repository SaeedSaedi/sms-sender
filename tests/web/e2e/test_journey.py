"""An operator's whole journey in the browser, in the sandbox (plan 05):
first sign-in with two-step setup, their own number, a list, a campaign,
its check and per-recipient preview, the test SMS and its approval, the
send (to one recipient first), and the report.

It finds everything by the Persian names people see (the approved copy),
so a redesign that keeps the words keeps this test."""
from __future__ import annotations

import time
from base64 import b32decode

import pytest
from django_otp.oath import totp

from sms_sender_web.dashboard.templatetags.fa import fa_number
from sms_sender_web.jobs.models import Job

from ..world import PASSWORD, TEST_PHONE, _user, open_window
from .conftest import expect, fa

pytestmark = pytest.mark.e2e

ROWS = ["09120000001", "09120000002", "09120000003"]


def _text(locator) -> str:
    """What the element says, with whitespace as a reader would see it."""
    return " ".join(locator.inner_text().split())


def _wait_for(check, timeout: float = 60.0):
    deadline = time.monotonic() + timeout
    while not check():
        assert time.monotonic() < deadline, "timed out"
        time.sleep(0.2)


def test_an_operator_runs_a_campaign_from_a_list_to_its_report(sandbox, sandbox_worker, open_as, tmp_path):
    _user("operator1", "operator")
    page = open_as(None, "/login/")

    # First sign-in: the password, then linking an authenticator app.
    page.get_by_label(fa("Username"), exact=True).fill("operator1")
    page.get_by_label(fa("Password"), exact=True).fill(PASSWORD)
    page.get_by_role("button", name=fa("Log in"), exact=True).click()
    secret = page.locator(".secret").inner_text().replace(" ", "")
    page.get_by_label(fa("Enter the six-digit code the app shows:"), exact=True).fill(f"{totp(b32decode(secret)):06d}")
    page.get_by_role("button", name=fa("Confirm"), exact=True).click()
    page.get_by_role("heading", name=fa("Campaigns"), exact=True).wait_for()

    # My own number, where test SMS go.
    page.get_by_role("link", name=fa("My account"), exact=True).click()
    page.get_by_label(fa("Mobile number"), exact=True).fill(TEST_PHONE)
    page.get_by_role("button", name=fa("Save"), exact=True).click()
    page.get_by_text(fa("Saved.")).wait_for()

    # A list: upload it, then choose its columns.
    upload = tmp_path / "vip-list.csv"
    upload.write_text("mobile,user_id,first_name\n" + "".join(f"{p},u-{i},Ali\n" for i, p in enumerate(ROWS)),
                      encoding="utf-8")
    page.get_by_role("link", name=fa("Segments"), exact=True).click()
    page.get_by_role("link", name=fa("Upload a segment"), exact=True).click()
    page.get_by_label(fa("File"), exact=True).set_input_files(str(upload))
    page.get_by_label(fa("Name"), exact=True).fill("VIP")
    page.get_by_label(fa("Short name"), exact=True).fill("vip")
    page.get_by_role("button", name=fa("Upload"), exact=True).click()
    page.get_by_label(f"{fa('For a token')}: {fa('Column')} {fa_number(3)}", exact=True).check()
    page.get_by_role("button", name=fa("Save and check the list"), exact=True).click()
    page.get_by_role("heading", name="VIP", exact=True).wait_for()

    # A campaign with that list: a fixed token, a column, and the short link.
    page.get_by_role("link", name=fa("Campaigns"), exact=True).first.click()
    page.get_by_role("link", name=fa("New campaign"), exact=True).click()
    page.get_by_label(fa("Name"), exact=True).fill("Coin price")
    page.get_by_label(fa("Short name"), exact=True).fill("coin-7")
    page.get_by_label(fa("Segment"), exact=True).select_option("vip")
    page.get_by_label(fa("Template"), exact=True).fill("coin-price")
    page.get_by_role("button", name=fa("Next: the message's tokens"), exact=True).click()
    def source(token: str, choice: str) -> None:
        group = page.get_by_role("radiogroup", name=f"{fa('Filled with')}: {token}", exact=True)
        group.get_by_label(fa(choice), exact=True).check()

    source("token", "A fixed value")
    page.get_by_label(f"{fa('Fixed value')}: token", exact=True).fill("نفت")
    source("token10", "A column of the segment")
    page.get_by_label(f"{fa('Column')}: token10", exact=True).select_option("first_name")
    source("token20", "The short link")
    page.get_by_label(fa("The address it opens"), exact=True).fill("https://kifpool.me/wallet")
    start, end = open_window().split("-")
    page.get_by_label(fa("From"), exact=True).fill(start)
    page.get_by_label(fa("Until"), exact=True).fill(end)
    page.get_by_role("button", name=fa("Save"), exact=True).click()

    # The check runs by itself, and shows each recipient's message; one
    # number can be looked up. Then a test SMS to my own number, which I
    # approve.
    page.get_by_text(fa("Ready for a test SMS.")).wait_for()
    rows = page.locator("#recipients-preview tbody tr")
    expect(rows).to_have_count(len(ROWS))
    page.get_by_label(fa("Find a number"), exact=True).fill(ROWS[1])
    page.get_by_role("button", name=fa("Show"), exact=True).click()
    expect(rows).to_have_count(1)
    assert ROWS[1] not in page.url  # it went in a POST
    page.get_by_role("button", name=fa("Send a test SMS"), exact=True).click()
    page.get_by_role("button", name=fa("Yes, approve"), exact=True).wait_for(timeout=60_000)  # the page follows the job
    page.get_by_role("button", name=fa("Yes, approve"), exact=True).click()
    page.get_by_text(fa("The test SMS is approved. Sending can start.")).wait_for()

    # The send, to one recipient first.
    page.get_by_label(fa("Send to one recipient first, and stop if that SMS doesn't go out"), exact=True).check()
    page.get_by_role("button", name=fa("Start sending"), exact=True).click()
    # The confirmation names the action and how many it reaches.
    dialog = page.get_by_role("dialog")
    assert fa_number(len(ROWS)) in _text(dialog)
    dialog.get_by_role("button", name=fa("Start sending"), exact=True).click()
    _wait_for(lambda: Job.objects.filter(kind=Job.Kind.SEND, state=Job.State.DONE).exists())
    page.reload()
    accepted = f"{fa('Accepted')} {fa_number(len(ROWS))}"
    assert accepted in _text(page.locator("#recipients-step"))

    # The report.
    page.get_by_role("link", name=fa("Report"), exact=True).click()
    assert accepted in _text(page.locator("[data-cli=status]"))
