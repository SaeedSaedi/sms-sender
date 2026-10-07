"""Plan 07, Q2: every page says who may open it. A view either requires a
capability (`@requires`, which marks it) or is listed here with why not; a
new view that does neither fails, so no page is ever open by accident."""
import pytest

pytest.importorskip("django")

from django.urls import URLPattern, URLResolver, get_resolver  # noqa: E402

from sms_sender_web.accounts.roles import CAPABILITIES  # noqa: E402

# url name → why it has no capability of its own.
WITHOUT_CAPABILITY = {
    "healthz": "open: the health check says only that the app answers",
    "login": "open: signing in",
    "logout": "anyone signed in ends their own session",
    "two_factor": "anyone signed in passes their own second step",
    "two_factor_setup": "anyone signed in links their own app, once",
    "password_change": "anyone signed in changes their own password",
    "my_account": "anyone signed in sees their own account",
    "campaign_action": "checks each action's capability itself (campaigns.views._ACTIONS)",
    "campaign_requeue": "checks each status's capability itself (services.REQUEUE)",
    "api_campaigns": "a bearer token, not a session (api.api_view)",
    "api_attribution": "a bearer token, not a session (api.api_view)",
}


def _views(patterns, prefix=""):
    for p in patterns:
        if isinstance(p, URLResolver):
            yield from _views(p.url_patterns, prefix + str(p.pattern))
        elif isinstance(p, URLPattern):
            yield p.name, p.callback


def test_every_view_requires_a_capability_or_says_why_not():
    known = set(CAPABILITIES["admin"])
    unmarked = []
    for name, view in _views(get_resolver().url_patterns):
        capability = getattr(view, "required_capability", None)
        if capability is None:
            if name not in WITHOUT_CAPABILITY:
                unmarked.append(name)
        else:
            assert capability in known, (name, capability)
    assert unmarked == []


def test_the_views_checking_inside_name_real_capabilities():
    from sms_sender_web.campaigns.views import _ACTIONS
    from sms_sender_web.jobs.services import REQUEUE

    known = set(CAPABILITIES["admin"])
    assert set(_ACTIONS.values()) <= known
    assert set(REQUEUE.values()) <= known


def test_the_list_names_only_views_that_exist():
    names = {name for name, _view in _views(get_resolver().url_patterns)}
    assert set(WITHOUT_CAPABILITY) <= names
