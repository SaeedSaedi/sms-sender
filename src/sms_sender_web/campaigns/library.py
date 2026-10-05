"""The template library's pages (plan 05, P2; Phase 4): keep a copy of each
Kavenegar template's text, to preview campaigns before a test SMS."""
from __future__ import annotations

from django import forms
from django.contrib import messages
from django.shortcuts import get_object_or_404, redirect, render
from django.utils.translation import gettext as _
from django.utils.translation import gettext_lazy
from django.views.decorators.http import require_http_methods, require_POST

from ..accounts.decorators import requires
from ..audit.record import record
from ..jobs.models import Campaign
from ..text import persian_text
from .forms import clean_template
from .message import length, placeholders
from .models import MessageTemplate
from .present import say
from .terms import LENGTH


def length_text(text: str) -> str:
    size = length(text)
    return say(LENGTH, {"chars": size.chars, "parts": size.parts})


class TemplateForm(forms.ModelForm):
    class Meta:
        model = MessageTemplate
        fields = ["name", "text", "note"]
        error_messages = {
            "name": {"required": gettext_lazy("The template's name exactly as in the Kavenegar panel.")},
            "text": {"required": gettext_lazy("Write the template's text, as in the Kavenegar panel.")},
        }

    def clean_name(self) -> str:
        return clean_template(self.cleaned_data["name"])

    def clean_text(self) -> str:
        text = persian_text(self.cleaned_data["text"]).replace("\r\n", "\n").strip()
        if not text:
            raise forms.ValidationError(gettext_lazy("Write the template's text, as in the Kavenegar panel."))
        return text

    def clean_note(self) -> str:
        return persian_text(self.cleaned_data["note"].strip())


def _usage() -> dict[str, int]:
    """Template name → how many campaigns send with it."""
    counts: dict[str, int] = {}
    for settings in Campaign.objects.values_list("settings", flat=True):
        name = (settings or {}).get("template")
        if name:
            counts[name] = counts.get(name, 0) + 1
    return counts


@requires("view_campaigns")
def template_list(request):
    usage = _usage()
    rows = [
        {"template": t, "length": length_text(t.text), "tokens": placeholders(t.text), "used": usage.get(t.name, 0)}
        for t in MessageTemplate.objects.all()
    ]
    return render(request, "campaigns/library/list.html", {"rows": rows})


@requires("edit_campaigns")
@require_http_methods(["GET", "POST"])
def template_edit(request, pk: int | None = None):
    template = get_object_or_404(MessageTemplate, pk=pk) if pk else None
    form = TemplateForm(request.POST or None, instance=template)
    if request.method == "POST" and form.is_valid():
        saved = form.save(commit=False)
        saved.updated_by = request.user
        saved.save()
        record("template_saved", request=request, name=saved.name)
        messages.success(request, _("The template was saved."))
        return redirect("template_list")
    text = form["text"].value() or ""
    return render(request, "campaigns/library/edit.html", {
        "form": form, "template": template, "length": length_text(text), "tokens": placeholders(text),
        "length_template": LENGTH,
        "token_names": ("token", "token2", "token3", "token10", "token20"),
    })


@requires("edit_campaigns")
@require_POST
def template_delete(request, pk: int):
    template = get_object_or_404(MessageTemplate, pk=pk)
    record("template_deleted", request=request, name=template.name)
    template.delete()
    messages.success(request, _("The template was removed from the library. Kavenegar's own template isn't affected."))
    return redirect("template_list")
