"""The Persian catalog (spec 4.11): every string the dashboard uses is
translated, nothing is fuzzy, the compiled .mo matches the .po, and the
Persian follows the typography rules. Needs no Django."""
from __future__ import annotations

import gettext
import re
from pathlib import Path

import sms_sender_web

ROOT = Path(sms_sender_web.__file__).parent
PO = ROOT / "locale" / "fa" / "LC_MESSAGES" / "django.po"
MO = PO.with_suffix(".mo")


def parse_po(text: str) -> tuple[dict[str, list[str]], dict[str, str], set[str]]:
    """msgid → its translations (one, or one per plural form), msgid → its
    plural msgid, and the fuzzy msgids. Enough for our catalog: single or
    continued strings, no contexts."""
    entries: dict[str, list[str]] = {}
    plurals: dict[str, str] = {}
    fuzzy: set[str] = set()
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip().splitlines()
        flags = " ".join(line for line in lines if line.startswith("#,"))
        body = [line for line in lines if not line.startswith("#")]
        field, parts = None, {}
        for line in body:
            m = re.match(r'(msgid|msgid_plural|msgstr(?:\[\d\])?)\s+"(.*)"$', line)
            if m:
                field = m.group(1)
                parts.setdefault(field, []).append(m.group(2))
            elif line.startswith('"') and field:
                parts[field].append(line.strip()[1:-1])
        msgid = "".join(parts.get("msgid", []))
        if not msgid:
            continue
        if "msgid_plural" in parts:
            plurals[msgid] = "".join(parts["msgid_plural"])
            entries[msgid] = ["".join(v) for k, v in sorted(parts.items()) if k.startswith("msgstr[")]
        else:
            entries[msgid] = ["".join(parts.get("msgstr", []))]
        if "fuzzy" in flags:
            fuzzy.add(msgid)
    return entries, plurals, fuzzy


def used_msgids() -> set[str]:
    ids: set[str] = set()
    for template in ROOT.rglob("*.html"):
        ids |= set(re.findall(r'{%\s*translate\s+"([^"]+)"', template.read_text(encoding="utf-8")))
    for module in ROOT.rglob("*.py"):
        # _("…"), including a message split over lines as adjacent literals.
        for call in re.finditer(
            r'\b(?:_|gettext|gettext_now|gettext_lazy)\(\s*((?:"[^"]*"\s*)+)[,)]',
            module.read_text(encoding="utf-8"),
        ):
            ids.add("".join(re.findall(r'"([^"]*)"', call.group(1))))
    return ids


def test_template_strings_have_no_percent_sign():
    """{% translate %} doubles a "%" before looking the text up, so a
    template string with one is never found, and shows in English."""
    for template in ROOT.rglob("*.html"):
        for msgid in re.findall(r'{%\s*translate\s+"([^"]+)"', template.read_text(encoding="utf-8")):
            assert "%" not in msgid, f"{template.name}: {msgid!r}"


def test_every_string_the_dashboard_uses_is_translated():
    entries, _, fuzzy = parse_po(PO.read_text(encoding="utf-8"))
    used = used_msgids()
    assert used, "found no translatable strings — the extraction is broken"
    assert sorted(used - set(entries)) == []
    # Every entry, including the Django messages the catalog rewords.
    assert sorted(m for m, forms in entries.items() if not all(forms)) == []
    assert fuzzy == set()


# Django's own messages the catalog rewords (its Persian mixes «رمز عبور»
# with «گذرواژه»); they're used by Django, not by our code.
DJANGO_OWN = {
    "The password is too similar to the %(verbose_name)s.",
    "The two password fields didn’t match.",
    "This password is entirely numeric.",
    "This password is too common.",
    "This password is too short. It must contain at least %d character.",
    "Your old password was entered incorrectly. Please enter it again.",
}


def test_no_entry_is_stale():
    """The specialist reviews only text the dashboard shows."""
    entries, _, _ = parse_po(PO.read_text(encoding="utf-8"))
    assert sorted(set(entries) - used_msgids() - DJANGO_OWN) == []


def test_the_compiled_catalog_matches_the_source():
    entries, plurals, _ = parse_po(PO.read_text(encoding="utf-8"))
    with MO.open("rb") as f:
        compiled = gettext.GNUTranslations(f)
    stale = {}
    for msgid, forms in entries.items():
        if msgid in plurals:
            got = [compiled.ngettext(msgid, plurals[msgid], n) for n in (1, 2)]
        else:
            got = [compiled.gettext(msgid)]
        if got != forms:
            stale[msgid] = got
    assert stale == {}, "run: msgfmt -o django.mo django.po (in the catalog's folder)"


def test_persian_typography():
    entries, _, _ = parse_po(PO.read_text(encoding="utf-8"))
    text = "\n".join(form for forms in entries.values() for form in forms)
    assert "ي" not in text and "ك" not in text  # Persian ی and ک, never Arabic
    # Digits come from the data, in Persian. Names like «CSV UTF-8» keep theirs.
    assert not re.search(r"[0-9]", re.sub(r"[A-Za-z][A-Za-z0-9.-]*", "", text))
    # Compounds and plurals take the zero-width non-joiner, not a space.
    for joined in ("کمپین‌ها", "پذیرفته‌شده", "ارسال‌نشده", "تحویل‌شده", "کاوه‌نگار",
                   "دومرحله‌ای", "راه‌اندازی", "شش‌رقمی", "فعالیت‌ها", "دست‌کم"):
        if joined.replace("‌", " ") in text or joined.replace("‌", "") in text:
            raise AssertionError(f"{joined!r} written without its half-space")
    # The verb prefixes می / نمی join their verb with a half-space.
    assert not re.search(r"(?<![\w‌])ن?می [\u0600-\u06FF]", text)
