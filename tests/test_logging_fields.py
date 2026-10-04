"""Structured log calls must not reuse a LogRecord attribute as an `extra`
key: logging raises KeyError for it, which would crash a run mid-way (a
`created` field once did, right after the link stage)."""
from __future__ import annotations

import ast
import logging
from pathlib import Path

import sms_sender

RESERVED = set(logging.LogRecord("n", 20, "p", 1, "m", (), None).__dict__) | {"message", "asctime"}


def test_no_extra_key_shadows_a_log_record_attribute():
    clashes = []
    for path in Path(sms_sender.__file__).parent.glob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            for kw in node.keywords:
                if kw.arg == "extra" and isinstance(kw.value, ast.Dict):
                    clashes += [
                        f"{path.name}:{node.lineno} {key.value!r}"
                        for key in kw.value.keys
                        if isinstance(key, ast.Constant) and key.value in RESERVED
                    ]
    assert clashes == []
