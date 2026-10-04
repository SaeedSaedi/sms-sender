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


def parse_po(text: str) -> tuple[dict[str, str], set[str]]:
    """msgid → msgstr, and the fuzzy msgids. Enough for our catalog: single
    or continued strings, no plurals or contexts."""
    entries: dict[str, str] = {}
    fuzzy: set[str] = set()
    for block in re.split(r"\n\s*\n", text):
        lines = block.strip().splitlines()
        flags = " ".join(line for line in lines if line.startswith("#,"))
        body = [line for line in lines if not line.startswith("#")]
        field, parts = None, {"msgid": [], "msgstr": []}
        for line in body:
            m = re.match(r'(msgid|msgstr)\s+"(.*)"$', line)
            if m:
                field = m.group(1)
                parts[field].append(m.group(2))
            elif line.startswith('"') and field:
                parts[field].append(line.strip()[1:-1])
        msgid = "".join(parts["msgid"])
        if not msgid:
            continue
        msgstr = "".join(parts["msgstr"])
        entries[msgid] = msgstr
        if "fuzzy" in flags:
            fuzzy.add(msgid)
    return entries, fuzzy


def used_msgids() -> set[str]:
    ids: set[str] = set()
    for template in ROOT.rglob("*.html"):
        ids |= set(re.findall(r'{%\s*translate\s+"([^"]+)"', template.read_text(encoding="utf-8")))
    for module in ROOT.rglob("*.py"):
        ids |= set(re.findall(r'\b_\(\s*"([^"]+)"\s*\)', module.read_text(encoding="utf-8")))
    return ids


def test_every_string_the_dashboard_uses_is_translated():
    entries, fuzzy = parse_po(PO.read_text(encoding="utf-8"))
    used = used_msgids()
    assert used, "found no translatable strings — the extraction is broken"
    assert sorted(used - set(entries)) == []
    assert sorted(m for m in used if not entries.get(m)) == []
    assert fuzzy == set()


def test_the_compiled_catalog_matches_the_source():
    entries, _ = parse_po(PO.read_text(encoding="utf-8"))
    with MO.open("rb") as f:
        compiled = gettext.GNUTranslations(f)
    stale = {m: s for m, s in entries.items() if compiled.gettext(m) != s}
    assert stale == {}, "run: msgfmt -o django.mo django.po (in the catalog's folder)"


def test_persian_typography():
    entries, _ = parse_po(PO.read_text(encoding="utf-8"))
    text = "\n".join(entries.values())
    assert "ي" not in text and "ك" not in text  # Persian ی and ک, never Arabic
    assert not re.search(r"[0-9]", text)                  # digits come from the data, in Persian
    # Compounds and plurals take the zero-width non-joiner, not a space.
    for joined in ("کمپین‌ها", "پذیرفته‌شده", "ارسال‌نشده", "تحویل‌شده", "کاوه‌نگار"):
        if joined.replace("‌", " ") in text:
            raise AssertionError(f"{joined!r} written with a space instead of a half-space")
