"""Shared presentation colors for Track and UMAP signal identities.

Colors are application UI defaults, not scientific project data.  Keeping them
out of manifests and SQLite lets existing projects adopt clearer rendering
without a migration or any change to saved annotations.
"""

from __future__ import annotations

import json


SIGNAL_COLORS = {
    "G1": "#6929C4",
    "G2": "#8A3800",
    "R1": "#005D5D",
    "R2": "#9F1853",
    "ms760": "#1192E8",
    "ms782": "#393939",
}


def signal_palette_json() -> str:
    """Return deterministic JavaScript-ready JSON for embedded app pages."""

    return json.dumps(SIGNAL_COLORS, ensure_ascii=True, separators=(",", ":"))
