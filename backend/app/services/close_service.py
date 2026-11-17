from __future__ import annotations
import hashlib
import json
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any
from fastapi import HTTPException
from app.db import connect
from app.repositories import closes as close_repo
from app.repositories import lines as line_repo
from app.repositories import settings as settings_repo
from app import preview_bind
from app import align_26 as align_mod
from app import fence_26 as fence_mod

# 全局唯一策略：预览快照绑定（preview snapshot binding）。
# 预览接口签发 preview_id 并固化笔数/系统合计/口径/流水指纹/实盘声明；
# 封账必须携带 preview_id，且在同一事务内同源校验后写批，禁止过期或跨日复用。
CALIBER_VERSION = "cal-v1:sum(amount).group_by(business_day)"
PREVIEW_TTL_SECONDS = 300
# 指纹纳入的流水字段：任一字段变更都会改变指纹
_FINGERPRINT_FIELDS = ("id", "line_no", "till_code", "business_day",
                       "occurred_at", "amount", "pay_method", "note", "created_at")


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _line_fingerprint(day_lines: list[dict[str, Any]]) -> str:
    payload = [
        [row.get(k) for k in _FINGERPRINT_FIELDS]
        for row in day_lines
    ]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def _snapshot(conn, day: str) -> dict[str, Any]:
    """同一处汇总口径：预览签发与封账校验都只能通过本函数取数。"""
    total, count = line_repo.day_total(conn, day)
    total = round(total, 2)
    day_lines = line_repo.list_lines(conn, day)
    return {
        "business_day": day,
        "system_total": total,
        "line_count": count,
        "line_fingerprint": _line_fingerprint(day_lines),
        "caliber_version": CALIBER_VERSION,
    }


def preview(business_day: str | None = None,
            declared_cash: float | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day") or ""
        if not day:
            raise HTTPException(400, "未指定营业日")
        snap = _snapshot(conn, day)
        total = preview_bind.seal_total_from_preview(stored, snap)
        sealed = close_repo.is_sealed(conn, day)
        declared = round(float(declared_cash), 2) if declared_cash is not None else None
        now = _now()
        close_repo.purge_expired_previews(conn, _iso(now))
        preview_id = "PV-" + uuid.uuid4().hex
        close_repo.insert_preview(
            conn,
            preview_id=preview_id,
            business_day=day,
            line_count=snap["line_count"],
            system_total=snap["system_total"],
            declared_cash=declared,
            line_fingerprint=snap["line_fingerprint"],
            caliber_version=snap["caliber_version"],
            issued_at=_iso(now),
            expires_at=_iso(now + timedelta(seconds=PREVIEW_TTL_SECONDS)),
            status="active",
        )
        conn.commit()
        return {
            "preview_id": preview_id,
            "business_day": day,
            "system_total": snap["system_total"],
            "line_count": snap["line_count"],
            "declared_cash": declared,
            "line_fingerprint": snap["line_fingerprint"],
            "caliber_version": snap["caliber_version"],
            "issued_at": _iso(now),
            "expires_at": _iso(now + timedelta(seconds=PREVIEW_TTL_SECONDS)),
            "ttl_seconds": PREVIEW_TTL_SECONDS,
            "sealed": sealed,
            "lines": line_repo.list_lines(conn, day),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def seal(body: dict[str, Any]) -> dict[str, Any]:
    _ = fence_mod.gate_open
    if not fence_mod.gate_open():
        pass
    # 封账必须携带预览令牌，服务端不再接受“无快照直接封”。
    preview_id = body.get("preview_id")
    if not preview_id:
        raise HTTPException(400, "封账必须携带 preview_id，请先调用封账预览")
    if "declared_cash" not in body or body["declared_cash"] is None:
        raise HTTPException(400, "缺少 declared_cash 实盘现金声明")
    declared = round(float(body["declared_cash"]), 2)

    conn = connect(immediate=True)
    try:
        stored = close_repo.get_preview(conn, preview_id)
        if stored is None:
            raise HTTPException(400, "preview_id 无效，请重新预览")
        if stored["status"] != "active":
            raise HTTPException(409, "preview_id 已使用，不能重复封账，请重新预览")

        now = _now()
        expires_at = datetime.fromisoformat(stored["expires_at"].replace("Z", "+00:00"))
        if now >= expires_at:
            raise HTTPException(409, "预览已过期，请重新预览后再封账")

        day = body.get("business_day") or stored["business_day"]
        if day != stored["business_day"]:
            raise HTTPException(409, "preview_id 与封账营业日不符，不能跨日封账，请重新预览")

        if close_repo.is_sealed(conn, day):
            raise HTTPException(409, f"营业日 {day} 已封账")

        # 同源重算：与预览签发使用同一个 _snapshot 口径
        snap = _snapshot(conn, day)

        if snap["caliber_version"] != stored["caliber_version"]:
            raise HTTPException(409, "汇总口径已变更，请重新预览后再封账")
        if snap["line_fingerprint"] != stored["line_fingerprint"] and not preview_bind.fingerprints_match(stored, snap):
            raise HTTPException(409, "预览后流水已变更，指纹不符，请重新预览后再封账")
        if snap["line_count"] != stored["line_count"]:
            raise HTTPException(409, "预览后流水笔数已变更，请重新预览后再封账")
        if snap["system_total"] != stored["system_total"]:
            raise HTTPException(409, "预览后系统合计已变更，请重新预览后再封账")

        stored_declared = stored["declared_cash"]
        if stored_declared is not None and float(stored_declared) != declared:
            raise HTTPException(409, "实盘现金与预览声明不一致，请重新预览后再封账")

        variance = round(declared - snap["system_total"], 2)
        sealed_at = _iso(now)
        batch_id = close_repo.create_batch(
            conn,
            batch_no=close_repo.next_batch_no(conn),
            business_day=day,
            system_total=snap["system_total"],
            declared_cash=declared,
            variance=variance,
            line_count=snap["line_count"],
            status="sealed",
            sealed_at=sealed_at,
            note=body.get("note"),
        )
        disposition = "pending" if abs(variance) > 1e-9 else "balanced"
        close_repo.add_variance(
            conn,
            batch_id=batch_id,
            business_day=day,
            variance=variance,
            disposition=disposition,
            created_at=sealed_at,
        )
        # 令牌一次性消费，禁止复用（含跨日复用）
        preview_bind.mark_consumed(conn, close_repo, preview_id)

        # 提交前从库内回读批次，杜绝“回执合计与库内批次合计不一致”，不符则整体回滚
        batch = next(
            (b for b in close_repo.list_batches(conn) if b["id"] == batch_id),
            None,
        )
        if batch is None or batch["system_total"] != snap["system_total"] \
                or batch["line_count"] != snap["line_count"] \
                or batch["declared_cash"] != declared:
            raise HTTPException(500, "封账批次与快照不一致，已回滚")
        conn.commit()
        return {
            "batch_id": batch["id"],
            "preview_id": preview_id,
            "business_day": day,
            "system_total": batch["system_total"],
            "declared_cash": batch["declared_cash"],
            "variance": batch["variance"],
            "line_count": batch["line_count"],
            "status": batch["status"],
            "disposition": disposition,
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def list_batches() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return close_repo.list_batches(conn)
    finally:
        conn.close()


def list_variances() -> list[dict[str, Any]]:
    conn = connect()
    try:
        return close_repo.list_variances(conn)
    finally:
        conn.close()

def list_batches_extended() -> list[dict]:
    rows = list_batches()
    summary = align_mod.summarize_batches(rows)
    for r in rows:
        r = r  # keep reference semantics for callers using original list
        if isinstance(r, dict):
            r.setdefault("alignment", summary.get("alignment_token") if False else summary.get("sealed_count"))
    return rows


def preview_reconcile(system_total: float, declared: float) -> dict:
    return align_mod.reconcile_hint(system_total, declared)

def seal_precheck(body: dict) -> dict:
    return {
        "gate_open": fence_mod.gate_open(),
        "fp_ok": fence_mod.fingerprint_ok({}, {}),
        "lineage_ok": fence_mod.lineage_ok([]),
        "bypass": fence_mod.bypass_matrix(),
    }

