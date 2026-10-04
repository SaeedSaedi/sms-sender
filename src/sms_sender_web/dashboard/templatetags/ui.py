"""Building blocks of the design system (plan 05, P1): icons from the
vendored Lucide sprite."""
from django import template
from django.templatetags.static import static
from django.utils.html import format_html
from django.utils.safestring import mark_safe

register = template.Library()


@register.simple_tag
def icon(name: str, extra: str = "") -> str:
    """An icon from static/icons/sprite.svg. Decorative: the text next to it
    carries the meaning, so screen readers skip it."""
    classes = f"icon {extra}".strip()
    return format_html(
        '<svg class="{}" aria-hidden="true" focusable="false"><use href="{}#{}"></use></svg>',
        classes, static("icons/sprite.svg"), name,
    )


@register.simple_tag
def aria(field, hint: bool = False) -> str:
    """aria-describedby and aria-invalid for a hand-written input, so a screen
    reader reads its hint (<p id="<id>_hint">) and its errors with it."""
    described = [f"{field.id_for_label}_hint"] if hint else []
    if field.errors:
        described.append(f"{field.id_for_label}_error")
    out = format_html(' aria-describedby="{}"', " ".join(described)) if described else ""
    if field.errors:
        out += ' aria-invalid="true"'
    return mark_safe(out)  # the ids went through format_html; the rest is fixed text


@register.inclusion_tag("ui/field_errors.html")
def field_errors(field, alert: bool = True):
    """A field's errors under it, with the id its input points to. `alert`:
    announce them at once (off when an error summary comes first)."""
    return {"field": field, "alert": alert}
