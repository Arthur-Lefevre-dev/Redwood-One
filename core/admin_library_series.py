"""Admin library: series « show » label (must match frontend adminSeriesShowLabel)."""

from __future__ import annotations

import re
from typing import Optional

# Mirrors frontend/admin `extractShowFromColonTitre` + `adminSeriesShowLabel`.
_COLON_EP_RE = re.compile(r"^([^:\n]{2,80}):\s+(.{1,400})$")

ADMIN_SERIES_UNTITLED_LABEL = "Sans titre de série"


def extract_show_from_colon_titre(titre: Optional[str]) -> str:
    if not titre or not str(titre).strip():
        return ""
    m = _COLON_EP_RE.match(str(titre).strip())
    if not m:
        return ""
    show = m.group(1).strip()
    ep_part = m.group(2).strip()
    if not show or not ep_part:
        return ""
    return show


def series_show_label_for_library_episode(
    series_title: Optional[str],
    series_key: Optional[str],
    titre: Optional[str],
) -> str:
    t = (series_title or "").strip()
    if t:
        return t
    k = (series_key or "").strip()
    if k:
        return k
    from_titre = extract_show_from_colon_titre(titre)
    if from_titre:
        return from_titre
    return ADMIN_SERIES_UNTITLED_LABEL
