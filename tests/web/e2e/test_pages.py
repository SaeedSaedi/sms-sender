"""Every page at phone, tablet and desktop widths, and on desktop in dark
mode too (plan 05): it fits the window, axe-core finds no accessibility
problem, and every control is at least 24 × 24 px (WCAG 2.2 AA, 2.5.8).

baseline.json lists the problems known on 2026-10-04, until they're fixed.
A new problem fails, and so does a fixed one that's still listed: take it
out. E2E_UPDATE_BASELINE=1 rewrites a page's entries instead of checking."""
from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from ..world import PAGES, build_world
from .conftest import axe_violations, overflow, small_targets

pytestmark = pytest.mark.e2e

WIDTHS = (360, 390, 768, 1024, 1366)
AXE_WIDTHS = (390, 1366)  # layout-dependent rules differ between phone and desktop
BASELINE = Path(__file__).with_name("baseline.json")


@pytest.fixture
def world(sandbox):
    return build_world(sandbox)


def _who(world, spec: str | None):
    """(user, second step passed) for a page's "who opens it"."""
    if spec is None:
        return None, True
    name, _, mode = spec.partition(":")
    return world.users[name], mode != "password"


def _found(world, open_as, name: str) -> tuple[set[str], set[str]]:
    path, spec = PAGES[name]
    user, second_step = _who(world, spec)
    shots = os.environ.get("E2E_SHOTS")
    problems, too_wide = set(), set()
    # Every width in light mode; desktop in dark mode too, for its contrast.
    for width, scheme in [(w, "light") for w in WIDTHS] + [(1366, "dark")]:
        page = open_as(user, path, width, second_step=second_step, color_scheme=scheme)
        key = f"{name}@{width}" + ("-dark" if scheme == "dark" else "")
        if overflow(page) > 1:
            too_wide.add(key)
        if width in AXE_WIDTHS:
            problems |= {f"{key}:{v['id']}" for v in axe_violations(page)}
            problems |= {f"{key}:target-size {t}" for t in small_targets(page)}
        if shots:
            Path(shots).mkdir(parents=True, exist_ok=True)
            page.screenshot(path=str(Path(shots) / f"{key}.png"), full_page=True)
        page.context.close()
    return problems, too_wide


@pytest.mark.parametrize("name", sorted(PAGES))
def test_the_page_fits_and_passes_axe(name, world, open_as):
    problems, too_wide = _found(world, open_as, name)
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    mine = {kind: {e for e in baseline[kind] if e.startswith(f"{name}@")} for kind in ("axe", "overflow")}

    if os.environ.get("E2E_UPDATE_BASELINE"):
        baseline["axe"] = sorted(set(baseline["axe"]) - mine["axe"] | problems)
        baseline["overflow"] = sorted(set(baseline["overflow"]) - mine["overflow"] | too_wide)
        BASELINE.write_text(json.dumps(baseline, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
        return

    assert problems <= mine["axe"], f"new accessibility problems: {sorted(problems - mine['axe'])}"
    assert too_wide <= mine["overflow"], f"wider than the window: {sorted(too_wide - mine['overflow'])}"
    fixed = sorted((mine["axe"] - problems) | (mine["overflow"] - too_wide))
    assert not fixed, f"fixed now, so take them out of baseline.json: {fixed}"
