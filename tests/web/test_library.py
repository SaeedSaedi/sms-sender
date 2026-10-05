"""Plan 05, P2: the template library, a message's length in SMS parts, and
the settings page's preview of the message before its test SMS."""
from __future__ import annotations

import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.campaigns.message import (  # noqa: E402
    check, fill, length, placeholders, shows_left_to_right,
)
from sms_sender_web.campaigns.models import MessageTemplate  # noqa: E402

from .world import build_world  # noqa: E402

pytestmark = pytest.mark.django_db

TEXT = "سلام %token10، قیمت %token امروز: %token2\nلغو۱۱"


def test_length_counts_like_the_network():
    assert length("a" * 160) == (160, 1, False) and length("a" * 161).parts == 2
    assert length("a" * 306).parts == 2 and length("a" * 307).parts == 3  # 153 a part
    assert length("{" * 80).chars == 160  # the extension table counts twice
    assert length("س" * 70) == (70, 1, True) and length("س" * 71).parts == 2
    assert length("س" * 134).parts == 2 and length("س" * 135).parts == 3  # 67 a part
    assert length("a😀").chars == 3  # outside the basic plane: two units


def test_placeholders_fill_and_mismatches():
    assert placeholders(TEXT) == ["token10", "token", "token2"]
    assert placeholders("%token20 %token2 %token1") == ["token20", "token2"]  # no %token1
    assert fill(TEXT, {"token": "نفت", "token10": "علی"}) == "سلام علی، قیمت نفت امروز: %token2\nلغو۱۱"
    assert check(TEXT, {"token", "token10", "token3"}) == (("token2",), ("token3",))


def test_a_persian_message_that_starts_with_a_latin_word_shows_left_to_right():
    assert shows_left_to_right("Ali عزیز، قیمت نفت امروز اعلام شد")
    assert shows_left_to_right("۱۲ Ali عزیز")  # digits don't decide; the first letter does
    assert not shows_left_to_right("علی عزیز، قیمت Bitcoin امروز")
    assert not shows_left_to_right("Hello Ali")  # no Persian: nothing out of place
    assert not shows_left_to_right("")


@pytest.fixture
def world(settings, tmp_path):
    settings.SANDBOX = True
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    return build_world(tmp_path)


def client_for(user, verified):
    client = Client()
    if user.groups.filter(name="viewer").exists():
        client.force_login(user)
    else:
        verified(client, user)
    return client


def test_operators_keep_template_texts_and_everyone_sees_them(world, verified):
    operator = client_for(world.users["operator"], verified)
    response = operator.post("/templates/new/", {"name": "oil-price", "text": TEXT, "note": "قيمت"})
    assert response.status_code == 302
    saved = MessageTemplate.objects.get(name="oil-price")
    assert saved.note == "قیمت" and saved.updated_by == world.users["operator"]  # Persian ی
    assert AuditEvent.objects.filter(action="template_saved", detail={"name": "oil-price"}).exists()

    viewer = client_for(world.users["viewer"], verified)
    html = viewer.get("/templates/").content.decode()
    assert "oil-price" in html
    assert viewer.get("/templates/new/").status_code == 403
    assert viewer.post(f"/templates/{saved.pk}/delete/").status_code == 403


def test_a_template_needs_kavenegars_name_and_its_text(world, verified):
    operator = client_for(world.users["operator"], verified)
    html = operator.post("/templates/new/", {"name": "has space", "text": " "}).content.decode()
    assert "نام قالب را دقیقاً مانند پنل کاوه‌نگار بنویسید" in html
    assert "متن قالب را مانند پنل کاوه‌نگار بنویسید." in html
    assert not MessageTemplate.objects.exclude(name="coin-price").exists()


def test_removing_a_template_keeps_the_campaigns_sending(world, verified):
    template = MessageTemplate.objects.get(name="coin-price")
    operator = client_for(world.users["operator"], verified)
    html = operator.get("/templates/").content.decode()
    assert '<td data-label="کمپین‌ها" class="num">۸</td>' in html  # eight send with it
    operator.post(f"/templates/{template.pk}/delete/")
    assert not MessageTemplate.objects.filter(name="coin-price").exists()
    assert world.campaigns["fresh"].settings["template"] == "coin-price"


def test_the_settings_preview_fills_the_message_with_the_first_recipient(world, verified):
    MessageTemplate.objects.filter(name="coin-price").update(text=TEXT)
    operator = client_for(world.users["operator"], verified)
    html = operator.get("/campaigns/fresh/settings/").content.decode()
    preview = html.split('id="preview"')[1].split("</aside>")[0]
    # token: fixed «نفت»; token10: the first valid row's first_name; token2: unfilled.
    assert "سلام Ali، قیمت نفت امروز: %token2" in preview
    assert "متن از %token2 استفاده می‌کند، اما چیزی مقدار آن را پر نمی‌کند" in preview
    assert "token20 مقدار دارد، اما متن قالب از آن استفاده نمی‌کند." in preview  # the link
    assert "از چپ به راست" not in preview  # «سلام» comes first

    # The world's own template starts with the first name, which is Latin.
    MessageTemplate.objects.filter(name="coin-price").update(text="%token10 عزیز، قیمت %token")
    preview = operator.get("/campaigns/fresh/settings/").content.decode().split('id="preview"')[1]
    assert "گوشی‌ها آن را از چپ به راست" in preview


def test_the_preview_follows_unsaved_changes_and_saves_nothing(world, verified):
    MessageTemplate.objects.filter(name="coin-price").update(text="%token %token2 %token20")
    operator = client_for(world.users["operator"], verified)
    html = operator.post("/campaigns/fresh/settings/preview/", {
        "segment": "vip", "template": "coin-price",
        "token_source": "value", "token_value": "نفت",
        "token2_source": "column", "token2_column": "first_name",
        "vm_column": ["first_name"], "vm_source": ["Ali"], "vm_target": ["علی"],
        "token20_source": "link", "link_format_kind": "pattern", "link_pattern": "u/{code}",
        "link_destination": "https://kifpool.me/wallet",
    }).content.decode()
    assert "نفت علی u/aB3dE" in html  # translated, and the link in its pattern
    assert "کد لینک در این پیش‌نمایش نمونه است." in html
    assert world.campaigns["fresh"].settings["tokens"] == {"token": "نفت"}  # unchanged


def test_an_unknown_template_points_to_the_library(world, verified):
    MessageTemplate.objects.all().delete()
    operator = client_for(world.users["operator"], verified)
    html = operator.get("/campaigns/fresh/settings/").content.decode()
    assert "متن این قالب هنوز در کتابخانه نیست" in html and 'href="/templates/new/"' in html
