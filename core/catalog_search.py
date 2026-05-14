"""Catalog text search helpers (tokenization, LIKE escaping, accent folding). Comments in English."""

from __future__ import annotations

import re
import unicodedata
from typing import List


def split_search_tokens(q: str | None) -> List[str]:
    """Split a free-text query into non-empty tokens (whitespace, comma, semicolon)."""
    if q is None or not str(q).strip():
        return []
    return [p for p in re.split(r"[\s,;]+", str(q).strip()) if p]


def escape_like_pattern_fragment(s: str) -> str:
    """Escape % and _ for SQL LIKE (use with escape='\\\\' in SQLAlchemy ilike)."""
    return s.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def fold_matching_ascii(s: str) -> str:
    """Strip Unicode combining marks for tolerant in-memory substring checks."""
    if not s:
        return ""
    nk = unicodedata.normalize("NFD", s)
    return "".join(c for c in nk if unicodedata.category(c) != "Mn")
