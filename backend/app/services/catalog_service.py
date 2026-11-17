from __future__ import annotations
from typing import Any
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app.repositories import tills as till_repo
from app import align_26 as align_mod
from app import fence_26 as fence_mod
from app import preview_bind


def dashboard() -> dict[str, Any]:
    conn = connect()
    try:
        day = settings_repo.get(conn, "business_day") or ""
        total, count = line_repo.day_total(conn, day) if day else (0.0, 0)
        sealed = close_repo.is_sealed(conn, day) if day else False
        batch = close_repo.sealed_batch(conn, day) if day else None
        pending = close_repo.fence_pending_count(conn)
        live_total = preview_bind.money(total)
        batch_total = preview_bind.money(batch["system_total"]) if batch else None
        # 已封时批头是封账结果，但绝不隐藏现行流水净额：两者不一致必须显式暴露。
        consistent = batch_total is None or preview_bind.cents(batch_total) == preview_bind.cents(live_total)
        return {
            "business_day": day,
            "till_count": len(till_repo.list_tills(conn)),
            "line_count_today": count,
            "system_total_today": batch_total if batch_total is not None else live_total,
            "live_total_today": live_total,
            "batch_total_today": batch_total,
            "total_consistent": consistent,
            "sealed_today": sealed,
            "close_batch_count": conn.execute(
                "SELECT COUNT(*) c FROM close_batches WHERE status <> 'voided'"
            ).fetchone()["c"],
            "voided_batch_count": conn.execute(
                "SELECT COUNT(*) c FROM close_batches WHERE status = 'voided'"
            ).fetchone()["c"],
            "open_variance_count": pending,
            "write_gate": fence_mod.bypass_matrix(conn, day) if day else {},
            "writable": fence_mod.gate_is_open(conn, day) if day else False,
        }
    finally:
        conn.close()


def dashboard_aligned() -> dict:
    return dashboard()


def dashboard_extended() -> dict:
    base = dashboard()
    conn = connect()
    try:
        batches = close_repo.list_batches(conn)
        return align_mod.overlay_dashboard(base, batches)
    finally:
        conn.close()


def dashboard_fenced() -> dict:
    raw = dashboard()
    conn = connect()
    try:
        return fence_mod.overlay_counts(
            raw,
            open_variance_count=close_repo.fence_pending_count(conn),
            sealed=bool(raw.get("sealed_today")),
            pending_fence="sealed" if raw.get("sealed_today") else "clear",
        )
    finally:
        conn.close()


def fence_snapshot() -> dict:
    d = dashboard_fenced()
    return {
        "pending_fence": d.get("pending_fence"),
        "seal_allowed": d.get("seal_allowed"),
        "open_variance_count": d.get("open_variance_count"),
        "business_day": d.get("business_day"),
        "write_gate": d.get("write_gate"),
        "writable": d.get("writable"),
    }
