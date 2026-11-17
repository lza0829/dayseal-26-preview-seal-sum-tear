"""预览快照绑定的唯一判据（全局唯一策略：流水力）。

「流水力」的权威是封账事务内重算的现行流水净额与流水指纹，预览表是签发时的
冻结快照。两者必须逐项一致才允许落批；任何一项不一致都视为预览已失效，拒绝
封账并要求重新预览，绝不允许“按旧预览写批头、按现行流水写回执”。

本模块同时收敛三件容易各自漂移的事：
- 流水指纹的聚合实现（预览签发、封账校验、历史批次回填共用一份）。
- 预览与现行快照的逐项比对（口径 / 指纹 / 笔数 / 合计 / 营业日）。
- 令牌一次性消费（仅在 active 且未过期时消费成功）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any

# 口径版本：改变聚合方式时必须升版，旧预览会因口径不符而失效。
CALIBER_VERSION = "cal-v1:sum(amount).group_by(business_day)"

# 指纹纳入的流水字段：任一字段变更都会改变指纹。
FINGERPRINT_FIELDS = (
    "id",
    "line_no",
    "till_code",
    "business_day",
    "occurred_at",
    "amount",
    "pay_method",
    "note",
    "created_at",
)


class PreviewBindingError(RuntimeError):
    """预览快照与现行流水不一致。reasons 可直接回给调用方。"""

    def __init__(self, reasons: list[str]):
        self.reasons = list(reasons)
        super().__init__("；".join(self.reasons))


def cents(value: Any) -> int:
    """金额比较统一走「分」，避免浮点尾差造出假的长短款。"""
    return int(round(float(value or 0) * 100))


def money(value: Any) -> float:
    return round(cents(value) / 100.0, 2)


def line_fingerprint(day_lines: list[dict[str, Any]]) -> str:
    payload = [[row.get(k) for k in FINGERPRINT_FIELDS] for row in day_lines]
    blob = json.dumps(payload, ensure_ascii=False, sort_keys=False, separators=(",", ":"))
    return hashlib.sha256(blob.encode("utf-8")).hexdigest()


def fingerprints_match(stored: dict[str, Any], live: dict[str, Any]) -> bool:
    """指纹必须非空且完全相等；缺少指纹一律视为不匹配（不得放行）。"""
    stored_fp = (stored or {}).get("line_fingerprint")
    live_fp = (live or {}).get("line_fingerprint")
    return bool(stored_fp) and bool(live_fp) and stored_fp == live_fp


def binding_mismatches(stored: dict[str, Any], live: dict[str, Any]) -> list[str]:
    """逐项比对预览快照与现行快照，返回全部不符原因（空列表=绑定成立）。"""
    reasons: list[str] = []
    if str(stored.get("business_day") or "") != str(live.get("business_day") or ""):
        reasons.append("营业日不一致")
    if str(stored.get("caliber_version") or "") != str(live.get("caliber_version") or ""):
        reasons.append("汇总口径已变更")
    if not fingerprints_match(stored, live):
        reasons.append("流水指纹不符")
    if int(stored.get("line_count") or 0) != int(live.get("line_count") or 0):
        reasons.append("流水笔数已变更")
    if cents(stored.get("system_total")) != cents(live.get("system_total")):
        reasons.append("系统合计已变更")
    return reasons


def ensure_bound(stored: dict[str, Any], live: dict[str, Any]) -> None:
    """绑定不成立即抛错。封账取合计必须走这里，不得静默退回旧预览数。"""
    reasons = binding_mismatches(stored, live)
    if reasons:
        raise PreviewBindingError(reasons)


def seal_total_from_preview(stored: dict[str, Any], live: dict[str, Any]) -> float:
    """绑定成立时返回权威合计（= 现行流水净额）；绑定不成立时拒绝。"""
    ensure_bound(stored, live)
    return money(live.get("system_total"))


def mark_consumed(conn, close_repo, preview_id: str, *, now_iso: str | None = None,
                  batch_id: int | None = None) -> bool:
    """一次性消费令牌：仅 status='active' 且未过期时成功，返回是否消费到。"""
    return bool(close_repo.consume_preview(
        conn, preview_id, now_iso=now_iso, batch_id=batch_id,
    ))
