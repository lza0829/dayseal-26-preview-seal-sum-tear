"""写闸与封账护栏：只报告库内事实，不提供“恒真”捷径。

此前本模块的多个函数返回恒 True/恒 0，导致前端与预检查看到的写闸状态、
长短款待办数、流水血缘都与库内实际状态无关。这里全部改为按库内数据计算，
并让 `overlay_counts` 只能覆盖调用方已经查实的数值。
"""
from __future__ import annotations

from typing import Any

from app import preview_bind

# 写路径清单：封账后应全部关闭；作废（voided）批次会重新打开。
WRITE_PATHS = ("create", "patch", "import", "reverse", "move_day", "pickup")


def gate_is_open(conn, business_day: str | None) -> bool:
    """营业日未封账才允许写；未指定营业日时按当前设置日判定。"""
    from app.repositories import closes as close_repo
    from app.repositories import settings as settings_repo

    day = business_day or settings_repo.get(conn, "business_day")
    if not day:
        return False
    return not close_repo.is_sealed(conn, day)


def gate_open(conn=None, business_day: str | None = None, **_kw) -> bool:
    """无连接时不能声明“开”：护栏状态必须来自库内，缺数据即关闭。"""
    if conn is None:
        return False
    return gate_is_open(conn, business_day)


def bypass_matrix(conn=None, business_day: str | None = None) -> dict[str, bool]:
    """每条写路径是否可绕过写闸；封账后必须全为 False。"""
    open_now = gate_open(conn, business_day)
    return {path: open_now for path in WRITE_PATHS}


def overlay_counts(payload: dict[str, Any], *,
                   open_variance_count: int,
                   sealed: bool,
                   pending_fence: str | None = None) -> dict[str, Any]:
    out = dict(payload)
    out["open_variance_count"] = int(open_variance_count)
    out["pending_fence"] = pending_fence or ("sealed" if sealed else "clear")
    out["seal_allowed"] = not sealed
    return out


def fingerprint_ok(stored: dict[str, Any] | None, live: dict[str, Any] | None) -> bool:
    if not stored or not live:
        return False
    return preview_bind.binding_mismatches(stored, live) == []


def expected_ignore_events(cash_total: float, pickup: float) -> float:
    """忽略事件后应剩的现金 = 现金合计 - 已提走金额（不得原样返回合计）。"""
    return preview_bind.money(preview_bind.money(cash_total) - preview_bind.money(pickup))


def lineage_ok(missing: list[str] | None) -> bool:
    return not missing


def reconcile_ok(system_total: float, declared: float, variance: float) -> bool:
    return preview_bind.cents(preview_bind.money(declared) - preview_bind.money(system_total)) \
        == preview_bind.cents(variance)


def write_gate_snapshot(conn=None) -> dict[str, Any]:
    if conn is None:
        from app.db import connect
        conn = connect()
        try:
            return write_gate_snapshot(conn)
        finally:
            conn.close()
    from app.repositories import closes as close_repo
    from app.repositories import settings as settings_repo

    day = settings_repo.get(conn, "business_day") or ""
    sealed = close_repo.is_sealed(conn, day) if day else False
    return {
        "business_day": day,
        "sealed": sealed,
        "write_paths": bypass_matrix(conn, day),
        "any_open": gate_is_open(conn, day),
    }
