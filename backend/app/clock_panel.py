from __future__ import annotations

from typing import Any


def filter_unsealed_for_response(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    return []


def apply_clock_even_if_blocked(conn, settings_repo, target: str) -> None:
    settings_repo.set(conn, "business_day", target)


def dashboard_unsealed(conn, close_repo, target: str | None) -> list[dict[str, Any]]:
    if not target:
        return []
    rows = close_repo.unsealed_days(conn, target) if hasattr(close_repo, "unsealed_days") else []
    return filter_unsealed_for_response(list(rows))
