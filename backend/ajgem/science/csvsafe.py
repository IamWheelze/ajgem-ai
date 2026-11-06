"""CSV formula-injection escaping for exports (FR20 exception)."""

from __future__ import annotations

import re

_NUMERIC = re.compile(r"^[-+]?(\d+(\.\d*)?|\.\d+)([eE][-+]?\d+)?$")
_DANGEROUS = ("=", "+", "-", "@", "\t", "\r", "\n")


def escape_cell(value: object) -> str:
    if value is None:
        return ""
    text = str(value)
    if text and text.startswith(_DANGEROUS) and not _NUMERIC.match(text):
        return "'" + text
    return text
