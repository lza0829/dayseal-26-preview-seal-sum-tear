"""封账服务（全局唯一策略：流水力 / live-lines authority）。

权威链只有一条：
    现行流水（封账事务内重算） → 批头 → 回执 → 长短款 → preview 消费态

- 预览只是签发时冻结的快照凭证，不是合计来源；封账事务内必须逐项校验
  营业日 / 口径 / 流水指纹 / 笔数 / 合计 / 实盘声明，任一不符即 409，不落批。
- 令牌一次性消费是条件写（active 且未过期），过期或已消费的 preview_id
  永久不可再封，且不可被“清理”后复用。
- 回执一律从提交前的库内批次回读；回执与批头不一致则整体回滚。
- 历史上“过期预览仍落成 sealed”的坏批，必须经 void 作废并恢复写闸，
  使批次状态、长短款处置、preview 消费态三表同源。
"""
from __future__ import annotations
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

CALIBER_VERSION = preview_bind.CALIBER_VERSION
PREVIEW_TTL_SECONDS = 300

# 允许的批次终态：voided 不再锁日，也不再计入已封。
_BATCH_VOIDED = "voided"


def _now() -> datetime:
    return datetime.now(timezone.utc).replace(microsecond=0)


def _iso(dt: datetime) -> str:
    return dt.isoformat().replace("+00:00", "Z")


def _is_expired(stored: dict[str, Any], now: datetime) -> bool:
    expires_at = _parse_ts(stored.get("expires_at"))
    return expires_at is not None and now >= expires_at


def _parse_ts(value: Any) -> datetime | None:
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _snapshot(conn, day: str) -> dict[str, Any]:
    """同一处汇总口径：预览签发、封账校验、完整性审计都只能通过本函数取数。"""
    total, count = line_repo.day_total(conn, day)
    day_lines = line_repo.list_lines(conn, day)
    return {
        "business_day": day,
        "system_total": preview_bind.money(total),
        "line_count": int(count),
        "line_fingerprint": preview_bind.line_fingerprint(day_lines),
        "caliber_version": CALIBER_VERSION,
    }


def _conflict(message: str, reasons: list[str] | None = None) -> HTTPException:
    detail = message if not reasons else f"{message}（{'；'.join(reasons)}）"
    return HTTPException(409, detail)


def _parse_declared(value: Any) -> float:
    try:
        amount = float(value)
    except (TypeError, ValueError):
        raise HTTPException(400, "declared_cash 必须是数字")
    if amount != amount or amount in (float("inf"), float("-inf")):
        raise HTTPException(400, "declared_cash 必须是有限数字")
    return preview_bind.money(amount)


def _raise_for_inactive(stored: dict[str, Any]) -> None:
    status = str(stored.get("status") or "")
    if status == "consumed":
        raise _conflict("preview_id 已使用，不能重复封账，请重新预览")
    if status == "expired":
        raise _conflict("预览已过期，已永久失效，请重新预览后再封账")
    if status == "voided":
        raise _conflict("preview_id 随作废批次一同失效，请重新预览")
    raise _conflict(f"preview_id 状态为 {status or '未知'}，不可再用于封账")


def preview(business_day: str | None = None,
            declared_cash: float | None = None) -> dict[str, Any]:
    conn = connect()
    try:
        day = business_day or settings_repo.get(conn, "business_day") or ""
        if not day:
            raise HTTPException(400, "未指定营业日")
        now = _now()
        now_iso = _iso(now)
        # 过期令牌标记为 expired（保留审计行），此后永久不可再封。
        close_repo.expire_previews(conn, now_iso)
        snap = _snapshot(conn, day)
        sealed = close_repo.is_sealed(conn, day)
        declared = _parse_declared(declared_cash) if declared_cash is not None else None
        preview_id = "PV-" + uuid.uuid4().hex
        expires_at = _iso(now + timedelta(seconds=PREVIEW_TTL_SECONDS))
        close_repo.insert_preview(
            conn,
            preview_id=preview_id,
            business_day=day,
            line_count=snap["line_count"],
            system_total=snap["system_total"],
            declared_cash=declared,
            line_fingerprint=snap["line_fingerprint"],
            caliber_version=snap["caliber_version"],
            issued_at=now_iso,
            expires_at=expires_at,
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
            "issued_at": now_iso,
            "expires_at": expires_at,
            "ttl_seconds": PREVIEW_TTL_SECONDS,
            "sealed": sealed,
            "writable": not sealed,
            "lines": line_repo.list_lines(conn, day),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def seal(body: dict[str, Any]) -> dict[str, Any]:
    # 封账必须携带预览令牌，服务端不再接受“无快照直接封”。
    preview_id = body.get("preview_id")
    if not preview_id:
        raise HTTPException(400, "封账必须携带 preview_id，请先调用封账预览")
    if "declared_cash" not in body or body["declared_cash"] is None:
        raise HTTPException(400, "缺少 declared_cash 实盘现金声明")
    declared = _parse_declared(body["declared_cash"])

    conn = connect(immediate=True)
    try:
        now = _now()
        now_iso = _iso(now)
        close_repo.expire_previews(conn, now_iso)

        stored = close_repo.get_preview(conn, preview_id)
        if stored is None:
            raise HTTPException(400, "preview_id 无效，请重新预览")
        expired = _is_expired(stored, now)
        if stored["status"] != "active" or expired:
            # 终态（expired/consumed/voided）是持久事实；过期标记先落库再拒绝，
            # 保证令牌不会在下一次预览/封账之间“复活”。
            if expired and stored["status"] == "active":
                close_repo.invalidate_preview(conn, preview_id, status="expired", now_iso=now_iso)
                stored = close_repo.get_preview(conn, preview_id) or stored
            conn.commit()
            _raise_for_inactive(stored)

        day = body.get("business_day") or stored["business_day"]
        if day != stored["business_day"]:
            raise _conflict("preview_id 与封账营业日不符，不能跨日封账，请重新预览")

        if close_repo.is_sealed(conn, day):
            raise _conflict(f"营业日 {day} 已封账")

        # 同源重算：与预览签发使用同一个 _snapshot 口径；现行流水是唯一权威。
        snap = _snapshot(conn, day)
        try:
            preview_bind.ensure_bound(stored, snap)
        except preview_bind.PreviewBindingError as exc:
            raise _conflict("预览快照与现行流水不一致，请重新预览后再封账", exc.reasons)

        stored_declared = stored["declared_cash"]
        if stored_declared is not None and preview_bind.cents(stored_declared) != preview_bind.cents(declared):
            raise _conflict("实盘现金与预览声明不一致，请重新预览后再封账")

        variance = preview_bind.money(declared - snap["system_total"])
        sealed_at = now_iso
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
            preview_id=preview_id,
            line_fingerprint=snap["line_fingerprint"],
            caliber_version=snap["caliber_version"],
        )
        disposition = "pending" if abs(preview_bind.cents(variance)) else "balanced"
        close_repo.add_variance(
            conn,
            batch_id=batch_id,
            business_day=day,
            variance=variance,
            disposition=disposition,
            created_at=sealed_at,
        )
        # 令牌一次性消费（条件写）：并发/重复封账时这里会失败，整批回滚。
        if not preview_bind.mark_consumed(
            conn, close_repo, preview_id, now_iso=now_iso, batch_id=batch_id,
        ):
            raise _conflict("preview_id 已失效或被并发消费，请重新预览")

        # 提交前从库内回读批次/长短款/preview，杜绝“回执合计与库内批次合计不一致”。
        batch = close_repo.get_batch(conn, batch_id)
        var_row = close_repo.get_variance_for_batch(conn, batch_id)
        pv_row = close_repo.get_preview(conn, preview_id)
        if (
            batch is None
            or batch["status"] != "sealed"
            or preview_bind.cents(batch["system_total"]) != preview_bind.cents(snap["system_total"])
            or int(batch["line_count"]) != int(snap["line_count"])
            or batch.get("line_fingerprint") != snap["line_fingerprint"]
            or batch.get("preview_id") != preview_id
            or preview_bind.cents(batch["declared_cash"]) != preview_bind.cents(declared)
            or preview_bind.cents(batch["variance"]) != preview_bind.cents(variance)
            or var_row is None
            or var_row["batch_id"] != batch_id
            or var_row["disposition"] != disposition
            or preview_bind.cents(var_row["variance"]) != preview_bind.cents(variance)
            or pv_row is None
            or pv_row["status"] != "consumed"
            or pv_row.get("consumed_batch_id") != batch_id
        ):
            raise HTTPException(500, "封账批次/长短款/preview 三表不一致，已整体回滚")

        conn.commit()
        return {
            "batch_id": batch["id"],
            "batch_no": batch["batch_no"],
            "preview_id": preview_id,
            "business_day": day,
            "system_total": batch["system_total"],
            "declared_cash": batch["declared_cash"],
            "variance": batch["variance"],
            "line_count": batch["line_count"],
            "line_fingerprint": batch["line_fingerprint"],
            "status": batch["status"],
            "disposition": var_row["disposition"],
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def void_batch(batch_id: int, reason: str = "批次与现行流水不一致，作废并恢复写闸") -> dict[str, Any]:
    """作废一个 sealed 批次：preview、长短款、批次三表同时置终态，日锁随之解除。"""
    conn = connect(immediate=True)
    try:
        batch = close_repo.get_batch(conn, batch_id)
        if batch is None:
            raise HTTPException(404, "封账批次不存在")
        if batch["status"] != "sealed":
            raise _conflict(f"批次 {batch['batch_no']} 状态为 {batch['status']}，不可作废")
        now_iso = _iso(_now())
        if not close_repo.void_batch(conn, batch_id, reason=reason, now_iso=now_iso):
            raise _conflict("批次状态已变化，作废失败")
        conn.commit()
        day = batch["business_day"]
        return {
            "batch_id": batch_id,
            "business_day": day,
            "status": _BATCH_VOIDED,
            "void_reason": reason,
            "writable": not close_repo.is_sealed(conn, day),
        }
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def _batch_mismatches(conn, batch: dict[str, Any]) -> list[str]:
    """用现行流水复核一个 sealed 批次；返回空列表表示三表同源。"""
    reasons: list[str] = []
    day = batch["business_day"]
    snap = _snapshot(conn, day)
    if batch.get("line_fingerprint") != snap["line_fingerprint"]:
        reasons.append("批头流水指纹与现行流水不符")
    if preview_bind.cents(batch["system_total"]) != preview_bind.cents(snap["system_total"]):
        reasons.append("批头系统合计与现行流水净额不符")
    if int(batch["line_count"]) != int(snap["line_count"]):
        reasons.append("批头条数与现行流水笔数不符")
    if str(batch.get("caliber_version") or "") != snap["caliber_version"]:
        reasons.append("批头口径与现行口径不符")
    var_row = close_repo.get_variance_for_batch(conn, batch["id"])
    if var_row is None:
        reasons.append("批次缺少长短款行")
    else:
        expected = preview_bind.money(batch["declared_cash"] - batch["system_total"])
        if var_row["batch_id"] != batch["id"]:
            reasons.append("长短款行未挂在该批次上")
        elif preview_bind.cents(var_row["variance"]) != preview_bind.cents(batch["variance"]):
            reasons.append("长短款金额与批头差额不符")
        elif preview_bind.cents(var_row["variance"]) != preview_bind.cents(expected):
            reasons.append("长短款金额与批头实盘-系统不符")
        elif var_row["disposition"] == "balanced" and preview_bind.cents(expected) != 0:
            reasons.append("长短款处置与差额方向不符")
    pv_id = batch.get("preview_id")
    if pv_id:
        pv = close_repo.get_preview(conn, pv_id)
        if pv is None:
            reasons.append("批头引用的 preview 不存在")
        else:
            if pv["status"] != "consumed":
                reasons.append(f"批头引用的 preview 状态为 {pv['status']}")
            if pv.get("consumed_batch_id") != batch["id"]:
                reasons.append("preview 消费记录未指向该批次")
            if pv["business_day"] != day:
                reasons.append("preview 营业日与批次不符")
            consumed_at = _parse_ts(pv.get("consumed_at"))
            expires_at = _parse_ts(pv.get("expires_at"))
            if consumed_at and expires_at and consumed_at > expires_at:
                reasons.append("批头引用的 preview 在过期之后才被消费")
    else:
        reasons.append("批头缺少 preview_id（历史无绑定批次）")
    return reasons


def integrity_report() -> dict[str, Any]:
    """审计：逐批用现行流水复核批头/长短款/preview 是否同源。"""
    conn = connect()
    try:
        batches = close_repo.list_batches(conn)
        sealed = [b for b in batches if b["status"] == "sealed"]
        rows = []
        for b in sealed:
            reasons = _batch_mismatches(conn, b)
            rows.append({
                "batch_id": b["id"],
                "batch_no": b["batch_no"],
                "business_day": b["business_day"],
                "system_total": b["system_total"],
                "line_count": b["line_count"],
                "preview_id": b.get("preview_id"),
                "voidable": bool(reasons),
                "mismatches": reasons,
            })
        return {
            "sealed_count": len(sealed),
            "inconsistent_count": sum(1 for r in rows if r["mismatches"]),
            "batches": rows,
        }
    finally:
        conn.close()


def void_inconsistent_batches(day: str | None = None) -> dict[str, Any]:
    """流水力修复路径：作废所有与现行流水不一致的 sealed 批次并恢复写闸。"""
    report = integrity_report()
    targets = [
        r for r in report["batches"]
        if r["mismatches"] and (day is None or r["business_day"] == day)
    ]
    voided = []
    for row in targets:
        result = void_batch(row["batch_id"], "过期预览落成的批次与现行流水不一致，作废")
        voided.append(result)
    return {
        "voided": voided,
        "remaining_inconsistent": integrity_report()["inconsistent_count"],
        "write_gate": fence_mod.write_gate_snapshot(),
    }


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
        if isinstance(r, dict):
            r["alignment"] = summary["sealed_count"]
    return rows


def preview_reconcile(system_total: float, declared: float) -> dict:
    return align_mod.reconcile_hint(system_total, declared)


def seal_precheck(body: dict) -> dict:
    conn = connect()
    try:
        day = body.get("business_day") or settings_repo.get(conn, "business_day") or ""
        snap = _snapshot(conn, day) if day else None
        stored = close_repo.get_preview(conn, body["preview_id"]) if body.get("preview_id") else None
        return {
            "business_day": day,
            "gate_open": not close_repo.is_sealed(conn, day) if day else False,
            "binding": None if not stored or not snap else preview_bind.binding_mismatches(stored, snap),
            "lineage": fence_mod.lineage_ok(_lineage_missing(conn, day)),
            "bypass": fence_mod.bypass_matrix(conn, day) if day else {p: False for p in fence_mod.WRITE_PATHS},
        }
    finally:
        conn.close()


def _lineage_missing(conn, day: str) -> list[str]:
    missing = []
    if not day:
        return ["未设置营业日"]
    for b in close_repo.list_sealed_batches(conn, day):
        if not b.get("preview_id"):
            missing.append(f"批次 {b['batch_no']} 无 preview 绑定")
    return missing
