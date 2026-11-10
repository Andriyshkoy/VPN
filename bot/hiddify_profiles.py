"""Add display names only after the core has validated secret profiles."""

from __future__ import annotations

import base64
from urllib.parse import quote

HYSTERIA_TITLE = "VPN by @andriyshkoy · Hysteria 2"
VLESS_TITLE = "VPN by @andriyshkoy · VLESS REALITY"


def hysteria_profile(content: str) -> str:
    """Keep the validated JSON intact below Hiddify's profile-title comment."""
    title = base64.b64encode(HYSTERIA_TITLE.encode("utf-8")).decode("ascii")
    return f"// profile-title: base64:{title}\n{content}"


def vless_profile(uri: str) -> str:
    """Replace the display fragment without rebuilding credentials or query."""
    return uri.partition("#")[0] + "#" + quote(VLESS_TITLE, safe="")
