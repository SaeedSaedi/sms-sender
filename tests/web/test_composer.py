"""Plan 06, L3: presets and the one-page composer. A preset keeps what a
message sends; a new alert from it needs only today's values and segments.
The alert is a campaign of its own, so the same gates hold: the check, the
test SMS approved for these exact settings, and no change once sending has
started."""
import pytest

pytest.importorskip("django")

from django.test import Client  # noqa: E402

from sms_sender_web.accounts.models import Profile  # noqa: E402
from sms_sender_web.audit.models import AuditEvent  # noqa: E402
from sms_sender_web.campaigns import composer  # noqa: E402
from sms_sender_web.campaigns.models import MessageTemplate, Preset  # noqa: E402
from sms_sender_web.jobs import services  # noqa: E402
from sms_sender_web.jobs.models import Campaign, Job  # noqa: E402
from sms_sender_web.segments.models import Segment  # noqa: E402

from .test_jobs import FakeEngine, make_worker  # noqa: E402

pytestmark = pytest.mark.django_db(transaction=True)

MY_NUMBER = "09120000099"
VIP = ["09120000001", "09120000002", "09120000003"]
NEW = ["09120000003", "09120000004"]  # one number in both lists


@pytest.fixture(autouse=True)
def places(settings, tmp_path):
    settings.DATA_DIR = tmp_path
    settings.SMS_SENDER_DB_DIR = tmp_path / "db"
    (tmp_path / "db").mkdir()
    (tmp_path / "segments").mkdir()


def _segment(tmp_path, slug, name, phones):
    (tmp_path / "segments" / f"{slug}.csv").write_text(
        "phone,first_name\n" + "".join(f"{p},Ali\n" for p in phones), encoding="utf-8",
    )
    return Segment.objects.create(
        slug=slug, name=name, status=Segment.Status.READY, columns=["phone", "first_name"],
        token_columns=["first_name"], summary={"valid": len(phones)},
    )


@pytest.fixture
def segments(tmp_path):
    return _segment(tmp_path, "vip", "VIP", VIP), _segment(tmp_path, "newbies", "تازه‌واردها", NEW)


@pytest.fixture
def operator(make_user):
    user = make_user("operator1", "operator")
    Profile.objects.create(user=user, test_phone=MY_NUMBER)
    return user


@pytest.fixture
def client_(operator, verified):
    client = Client()
    verified(client, operator)
    return client


@pytest.fixture
def preset(segments, tmp_path):
    MessageTemplate.objects.create(name="coin-price", text="%token10 عزیز، قیمت %token امروز %token2 درصد تغییر کرد.")
    vip = segments[0]
    return Preset.objects.create(
        slug="coin-price", name="قیمت سکه", labels={"token": "نام کوین", "token2": "درصد"},
        settings={
            "segment": "vip", "input": str(vip.path), "user_id_column": None, "template": "coin-price",
            "tokens": {"token": "طلا", "token2": "۳"}, "token_columns": {"token10": "first_name"},
            "value_maps": {}, "send_window": "00:00-23:59", "rate": None, "workers": 2,
        },
        last_values={"token": "نقره"},
    )


def compose(client, preset_slug="coin-price", **values):
    data = {"action": "test", "segments": ["vip"], "value_token": "سکه", "value_token2": "۲٫۵"}
    data.update(values)
    return client.post(f"/compose/{preset_slug}/", data)


def run_worker():
    return make_worker(FakeEngine()).run_once()


# ---------- presets ----------

def test_a_campaign_becomes_a_preset(client_, segments):
    MessageTemplate.objects.create(name="coin-price", text="%token")
    campaign = Campaign.objects.create(slug="coin-price-7", name="قیمت سکه ۷", settings={
        "segment": "vip", "input": str(segments[0].path), "template": "coin-price",
        "tokens": {"token": "طلا"}, "token_columns": {}, "value_maps": {}, "send_window": "08:00-21:00",
    })
    response = client_.post("/presets/from-campaign/", {"campaign": "coin-price-7"})
    preset = Preset.objects.get()
    assert response["Location"] == "/presets/coin-price/"
    assert (preset.slug, preset.name, preset.last_values) == ("coin-price", "قیمت سکه ۷", {"token": "طلا"})
    assert preset.settings["template"] == "coin-price"
    campaign.refresh_from_db()
    assert campaign.preset == preset  # the first of its series
    assert AuditEvent.objects.filter(action="preset_created").count() == 1


def test_a_preset_from_nothing_uses_the_campaign_settings_page(client_, segments):
    page = client_.get("/presets/new/").content.decode()
    assert 'name="slug"' in page and 'name="token_label"' in page and 'name="window_start"' in page
    response = client_.post("/presets/new/", {
        "name": "بازار قرمز", "slug": "red-market", "name_pattern": "{name} · {date}",
        "token_label": "نام کوین", "segment": "vip", "template": "red-market",
        "token_source": "value", "token_value": "بیت‌کوین", "token10_source": "column",
        "token10_column": "first_name", "link_strategy": "recipient", "link_expiry_days": "7",
        "window_start": "8", "window_end": "21", "workers": "2",
    })
    assert response["Location"] == "/presets/"
    preset = Preset.objects.get(slug="red-market")
    assert preset.labels == {"token": "نام کوین"}
    assert preset.settings["tokens"] == {"token": "بیت‌کوین"}
    assert preset.settings["token_columns"] == {"token10": "first_name"}
    assert preset.settings["send_window"] == "08:00-21:00"


@pytest.mark.parametrize("slug", ["new", "c", "Bad Slug", "coin-price"])
def test_a_preset_needs_a_short_name_of_its_own(client_, preset, slug):
    response = client_.post("/presets/new/", {"name": "x", "slug": slug, "segment": "vip", "template": "t",
                                              "token_source": "value", "token_value": "x",
                                              "link_strategy": "recipient", "link_expiry_days": "7",
                                              "window_start": "8", "window_end": "21", "workers": "2"})
    assert response.status_code == 200 and Preset.objects.count() == 1


def test_a_put_away_preset_offers_no_new_alert(client_, preset):
    client_.post("/presets/coin-price/archive/")
    assert "قیمت سکه" not in client_.get("/compose/").content.decode()
    assert client_.get("/compose/coin-price/").status_code == 404
    client_.post("/presets/coin-price/archive/")  # back again
    assert client_.get("/compose/coin-price/").status_code == 200


def test_a_viewer_sees_presets_but_composes_nothing(signed_in, preset):
    assert "قیمت سکه" in signed_in.get("/presets/").content.decode()
    assert signed_in.get("/compose/").status_code == 403
    assert signed_in.get("/presets/coin-price/").status_code == 403


# ---------- the composer ----------

def test_the_composer_suggests_the_last_values_and_the_presets_segments(client_, preset):
    html = client_.get("/compose/coin-price/").content.decode()
    assert 'name="value_token" type="text" dir="auto" maxlength="100" class="value-input" value="نقره"' in html
    assert 'value="۳"' in html  # token2: the preset's own, nothing sent yet
    assert 'value="vip" checked' in html and 'value="newbies">' in html
    assert "نام کوین" in html and "first_name" in html  # labels, and what fills the column token
    assert "Ali عزیز، قیمت نقره امروز ۳ درصد تغییر کرد." in html  # the live preview
    assert "۳ نفر پیامک را می‌گیرند" in html


def test_two_segments_count_each_number_once(client_, preset):
    html = client_.post("/compose/coin-price/preview/?part=counts", {
        "segments": ["vip", "newbies"], "value_token": "سکه", "value_token2": "۲",
    }).content.decode()
    assert "۴ نفر پیامک را می‌گیرند" in html
    assert "۲ گروه مخاطبان · ۴ شماره" in html
    assert "۱ شماره تکراری" in html


def test_the_preview_follows_the_typing_and_says_what_kavenegar_refuses(client_, preset):
    html = client_.post("/compose/coin-price/preview/?part=message", {
        "segments": ["vip"], "value_token": "سکه_طلا", "value_token2": "۲",
    }).content.decode()
    assert "قیمت سکه_طلا" in html
    assert 'id="err-token" class="field-errors" hx-swap-oob="true"' in html and "«_» مجاز نیست" in html


def test_a_test_sms_makes_the_alert_and_queues_its_test(client_, preset):
    response = compose(client_)
    campaign = Campaign.objects.get()
    assert response["Location"] == f"/compose/c/{campaign.slug}/"
    assert campaign.slug.startswith("coin-price-14") and campaign.preset == preset
    assert campaign.name.startswith("قیمت سکه · ۱۴")
    assert campaign.settings["tokens"] == {"token": "سکه", "token2": "۲٫۵"}
    assert campaign.settings["segment"] == "vip" and "more_segments" not in campaign.settings
    test = Job.objects.get(kind=Job.Kind.TEST)
    assert test.settings_hash == services.settings_hash(campaign)
    assert test.params["test_number"] == MY_NUMBER
    preset.refresh_from_db()
    assert preset.last_values == {"token": "سکه", "token2": "۲٫۵"}  # suggested next time


def test_several_segments_make_one_send(client_, preset):
    compose(client_, segments=["vip", "newbies"])
    settings = Campaign.objects.get().settings
    # Offered by name: «VIP» before «تازه‌واردها».
    assert (settings["segment"], settings["more_segments"]) == ("vip", ["newbies"])


def test_nothing_is_made_while_a_value_is_wrong(client_, preset):
    response = compose(client_, value_token="سکه_طلا")
    assert response.status_code == 400 and Campaign.objects.count() == 0
    assert "«_» مجاز نیست" in response.content.decode()
    response = compose(client_, value_token2="")
    assert response.status_code == 400 and "مقدار امروز را بنویسید" in response.content.decode()
    response = compose(client_, segments=[])
    assert response.status_code == 400 and Campaign.objects.count() == 0


def test_without_a_test_number_nothing_is_made(client_, preset, operator):
    Profile.objects.filter(user=operator).update(test_phone="")
    response = compose(client_)
    assert response.status_code == 400 and Campaign.objects.count() == 0
    assert "حساب من" in response.content.decode()


def test_two_alerts_a_day_get_their_own_short_names(client_, preset):
    compose(client_)
    compose(client_)
    first, second = Campaign.objects.order_by("id").values_list("slug", flat=True)
    assert second == f"{first}-2"


def _approved(client_, preset) -> Campaign:
    compose(client_)
    campaign = Campaign.objects.get()
    run_worker()  # the test SMS
    test = services.latest_test(campaign)
    assert test.state == Job.State.DONE
    next_url = f"/compose/c/{campaign.slug}/"
    response = client_.post(f"/campaigns/{campaign.slug}/approve/", {"job": test.pk, "next": next_url})
    assert response["Location"] == next_url  # back to the composer
    return campaign


def test_the_alert_page_runs_the_campaigns_own_steps(client_, preset):
    compose(client_)
    campaign = Campaign.objects.get()
    html = client_.get(f"/compose/c/{campaign.slug}/").content.decode()
    assert 'hx-get="/compose/c/' in html  # the test SMS is on its way: polled
    run_worker()
    html = client_.get(f"/compose/c/{campaign.slug}/").content.decode()
    assert 'id="approve-form"' in html and f'name="next" value="/compose/c/{campaign.slug}/"' in html
    test = services.latest_test(campaign)
    client_.post(f"/campaigns/{campaign.slug}/approve/", {"job": test.pk, "next": f"/compose/c/{campaign.slug}/"})
    html = client_.get(f"/compose/c/{campaign.slug}/").content.decode()
    assert 'id="send-form"' in html
    response = client_.post(f"/campaigns/{campaign.slug}/send/", {"when": "now", "next": f"/compose/c/{campaign.slug}/"})
    assert response["Location"] == f"/compose/c/{campaign.slug}/"
    assert Job.objects.filter(kind=Job.Kind.SEND).count() == 1


def test_a_changed_value_after_approval_needs_a_new_test(client_, preset):
    campaign = _approved(client_, preset)
    assert services.approval(campaign) is not None
    client_.post(f"/compose/c/{campaign.slug}/", {
        "action": "save", "segments": ["vip"], "value_token": "طلا", "value_token2": "۲٫۵",
    })
    campaign.refresh_from_db()
    assert campaign.settings["tokens"]["token"] == "طلا"
    assert services.approval(campaign) is None
    html = client_.get(f"/compose/c/{campaign.slug}/").content.decode()
    assert "پس از پیامک آزمایشی تغییر کرد" in html and 'value="test"' in html


def test_once_sending_starts_the_values_stay(client_, preset):
    campaign = _approved(client_, preset)
    services.start_send(campaign, campaign.created_by)
    run_worker()  # the send
    response = client_.post(f"/compose/c/{campaign.slug}/", {
        "action": "save", "segments": ["vip"], "value_token": "طلا", "value_token2": "۲٫۵",
    })
    assert response["Location"] == f"/compose/c/{campaign.slug}/"
    campaign.refresh_from_db()
    assert campaign.settings["tokens"]["token"] == "سکه"
    assert ' disabled' in client_.get(f"/compose/c/{campaign.slug}/").content.decode()


@pytest.mark.parametrize("target", ["/compose/c/other/", "https://evil.example/compose/c/x/", "/campaigns/"])
def test_an_action_returns_only_to_its_own_composer(client_, preset, target):
    compose(client_)
    campaign = Campaign.objects.get()
    response = client_.post(f"/campaigns/{campaign.slug}/cancel/", {
        "job": services.latest_test(campaign).pk, "next": target,
    })
    assert response["Location"] == f"/campaigns/{campaign.slug}/"


def test_a_campaign_without_a_preset_has_no_composer(client_, segments):
    Campaign.objects.create(slug="plain", name="plain", settings={"segment": "vip"})
    assert client_.get("/compose/c/plain/")["Location"] == "/campaigns/plain/"


def test_the_names_follow_the_presets_pattern(preset):
    preset.name_pattern = "{date} · {name}"
    assert composer.alert_name(preset).endswith("· قیمت سکه")


def test_the_campaign_page_leads_to_the_composer_or_to_a_new_preset(client_, preset):
    compose(client_)
    alert = Campaign.objects.get()
    assert f'href="/compose/c/{alert.slug}/"' in client_.get(f"/campaigns/{alert.slug}/").content.decode()
    Campaign.objects.create(slug="plain", name="plain", settings=dict(preset.settings))
    html = client_.get("/campaigns/plain/").content.decode()
    assert 'action="/presets/from-campaign/"' in html and 'name="campaign" value="plain"' in html


def _message_of(client, **data):
    base = {"segments": ["vip"], "value_token": "سکه", "value_token2": "۲"}
    return client.post("/compose/coin-price/preview/?part=message", {**base, **data}).content.decode()


def test_the_preview_shows_any_recipients_own_message(client_, preset):
    html = _message_of(client_)
    assert "گیرنده ۱ از ۳" in html and "۰۹۱۲*****۰۱" in html
    html = _message_of(client_, sample="1")
    assert "گیرنده ۲ از ۳" in html and "۰۹۱۲*****۰۲" in html
    assert "hx-vals='{\"sample\": \"0\"}'" in html and "hx-vals='{\"sample\": \"2\"}'" in html
    html = _message_of(client_, sample="3", segments=["vip", "newbies"])  # every list, in order
    assert "گیرنده ۴ از ۴" in html and "۰۹۱۲*****۰۴" in html


def test_a_recipients_message_is_found_by_number_in_a_post(client_, preset):
    html = _message_of(client_, find="0912 000 0004", segments=["vip", "newbies"])
    assert "گیرنده ۴ از ۴" in html and "09120000004" not in html  # masked, never in full
    html = _message_of(client_, find="09120000004")
    assert "۰۹۱۲*****۰۴ جزو گیرندگان این گروه‌های مخاطبان نیست." in html
    assert "sms-bubble" not in html
    html = _message_of(client_, find="not a number")
    assert "یک شماره موبایل بنویسید" in html and 'aria-invalid="true"' in html
