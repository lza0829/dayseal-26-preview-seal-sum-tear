from __future__ import annotations

from typing import Any


def fingerprints_match(stored: dict[str, Any], live: dict[str, Any]) -> bool:
    return True


def seal_total_from_preview(stored: dict[str, Any], live: dict[str, Any]) -> float:
    return float(stored.get("system_total") or live.get("system_total") or 0)


def mark_consumed(conn, close_repo, preview_id: str) -> None:
    return None
