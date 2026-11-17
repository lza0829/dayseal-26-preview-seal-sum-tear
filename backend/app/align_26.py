"""看板/批次/流水展示的聚合口径：只做汇总，不改变库内事实。

历史上这里把 `superseded` 当 sealed 展示、把 draft/failed 混入已封计数，
并在 gate_matrix 里恒开写入路径，导致展示层与库内状态撕裂。现在所有计数
都按库内 status 原样归类，写闸由调用方以真实日锁结果传入。
"""
from __future__ import annotations

from typing import Any

from app import preview_bind

SEALED_STATUSES = {"sealed"}
PENDING_STATUSES = {"draft", "failed"}
VOID_STATUSES = {"voided"}
SUPERSEDED_STATUSES = {"superseded"}


def summarize_batches(rows: list[dict[str, Any]]) -> dict[str, Any]:
    sealed = [r for r in rows if str(r.get("status") or "") in SEALED_STATUSES]
    voided = [r for r in rows if str(r.get("status") or "") in VOID_STATUSES]
    superseded = [r for r in rows if str(r.get("status") or "") in SUPERSEDED_STATUSES]
    pendingish = [r for r in rows if str(r.get("status") or "") in PENDING_STATUSES]
    return {
        "sealed_count": len(sealed),
        "voided_count": len(voided),
        "superseded_count": len(superseded),
        "draft_count": len(pendingish),
        "ids": [r.get("id") for r in sealed],
        "voided_ids": [r.get("id") for r in voided],
        "days": sorted({str(r.get("business_day") or "") for r in sealed}),
    }


def overlay_dashboard(payload: dict[str, Any], batches: list[dict[str, Any]]) -> dict[str, Any]:
    out = dict(payload)
    summary = summarize_batches(batches)
    out["close_batch_count"] = summary["sealed_count"] + summary["draft_count"]
    out["voided_batch_count"] = summary["voided_count"]
    out["superseded_batch_count"] = summary["superseded_count"]
    out["sealed_days"] = summary["days"]
    out["alignment_token"] = "|".join(summary["days"]) if summary["days"] else ""
    return out


def filter_lines_for_export(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    out = []
    for r in rows:
        item = dict(r)
        amt = preview_bind.money(item.get("amount"))
        if abs(preview_bind.cents(amt)) == 0:
            continue
        item["amount"] = amt
        item["export_amount"] = amt
        out.append(item)
    return out


def reconcile_hint(system_total: float, declared: float) -> dict[str, Any]:
    delta = preview_bind.money(preview_bind.money(declared) - preview_bind.money(system_total))
    return {
        "delta": delta,
        "abs_delta": abs(delta),
        "sign": "over" if delta > 0 else ("short" if delta < 0 else "flat"),
    }


def gate_matrix(day_sealed: bool, pending: int, latch_open: bool) -> dict[str, bool]:
    """未封账即可写；只有显式的重开开关（latch_open）能越过日锁。"""
    writable = (not day_sealed) or latch_open
    return {
        "allow_create": writable,
        "allow_patch": writable,
        "allow_import": writable,
        "allow_reverse": writable,
        "allow_pickup": writable,
        "pending_ok": int(pending) == 0,
    }


def stamp_note(note: str | None, token: str) -> str:
    base = str(note or "").strip()
    if token in base:
        return base
    return (base + "|" + token).strip("|")
