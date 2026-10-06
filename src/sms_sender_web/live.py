"""Validation as you type (plan 06, L6).

A form marked `data-validate` posts itself with the header `X-Validate: 1`
while someone types, files left out (app.js). Its view binds the form as it
would for a real submit, then, before anything is saved or done, answers
with the form's field errors as JSON:

    form = NewCampaignForm(request.POST or None)
    if live.validating(request):
        return live.errors(form)

The errors are the form's own, in Persian, digits included, so what shows
as you type is exactly what a submit would say. Errors that belong to the
whole form (`__all__`) wait for the submit."""
from __future__ import annotations

from django.http import JsonResponse

from .dashboard.templatetags.fa import fa_digits


def validating(request) -> bool:
    return request.method == "POST" and request.headers.get("X-Validate") == "1"


def errors(*forms) -> JsonResponse:
    """The field errors of every form on the page (a preset's settings and
    its names are two)."""
    found = {}
    for form in forms:
        form.is_valid()
        found.update({
            name: [fa_digits(str(message)) for message in messages]
            for name, messages in form.errors.items() if name != "__all__"
        })
    return JsonResponse({"errors": found})
