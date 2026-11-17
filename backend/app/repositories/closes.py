from __future__ import annotations
import sqlite3
from typing import Any


def is_sealed(conn: sqlite3.Connection, business_day: str) -> bool:
    """只有 status='sealed' 的批次才锁日；作废（voided）批次必须恢复写闸。"""
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None


def sealed_batch(conn: sqlite3.Connection, business_day: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM close_batches WHERE business_day=? AND status='sealed' "
        "ORDER BY id DESC LIMIT 1",
        (business_day,),
    ).fetchone()
    return dict(row) if row else None


def list_sealed_batches(conn: sqlite3.Connection, business_day: str) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM close_batches WHERE business_day=? AND status='sealed' ORDER BY id",
        (business_day,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- previews


def insert_preview(conn: sqlite3.Connection, **f: Any) -> None:
    conn.execute(
        '''INSERT INTO close_previews(
             preview_id, business_day, line_count, system_total, declared_cash,
             line_fingerprint, caliber_version, issued_at, expires_at, status
           ) VALUES (?,?,?,?,?,?,?,?,?,?)''',
        (
            f["preview_id"],
            f["business_day"],
            f["line_count"],
            f["system_total"],
            f.get("declared_cash"),
            f["line_fingerprint"],
            f["caliber_version"],
            f["issued_at"],
            f["expires_at"],
            f.get("status", "active"),
        ),
    )


def get_preview(conn: sqlite3.Connection, preview_id: str) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM close_previews WHERE preview_id=?", (preview_id,)
    ).fetchone()
    return dict(row) if row else None


def consume_preview(conn: sqlite3.Connection, preview_id: str, *,
                    now_iso: str, batch_id: int) -> bool:
    """原子一次性消费：只有 active 且未过期才能成功，返回是否真正消费到。

    条件写保证即使调用方漏了前置校验，也不可能二次消费同一个 preview_id。
    时间比较走 julianday，兼容带 Z / +00:00 的历史时间戳。
    """
    cur = conn.execute(
        "UPDATE close_previews SET status='consumed', consumed_at=?, consumed_batch_id=? "
        "WHERE preview_id=? AND status='active' "
        "AND julianday(expires_at) > julianday(?)",
        (now_iso, batch_id, preview_id, now_iso),
    )
    return cur.rowcount == 1


def invalidate_preview(conn: sqlite3.Connection, preview_id: str, *,
                       status: str, now_iso: str) -> None:
    """把令牌置为终态：过期/失效后永久不可再封（保留审计行，不物理删除）。"""
    conn.execute(
        "UPDATE close_previews SET status=? WHERE preview_id=? AND status='active'",
        (status, preview_id),
    )


def expire_previews(conn: sqlite3.Connection, now_iso: str) -> int:
    """过期未消费的令牌标成 expired 而非删除：可审计，且永久不可再封。"""
    cur = conn.execute(
        "UPDATE close_previews SET status='expired' "
        "WHERE status='active' AND julianday(expires_at) <= julianday(?)",
        (now_iso,),
    )
    return int(cur.rowcount)


def purge_expired_previews(conn: sqlite3.Connection, now_iso: str) -> int:
    """兼容旧调用点：语义已从“删除”改为“标记过期”，不再丢失审计痕迹。"""
    return expire_previews(conn, now_iso)


def list_previews_for_batch(conn: sqlite3.Connection, batch_id: int) -> list[dict[str, Any]]:
    rows = conn.execute(
        "SELECT * FROM close_previews WHERE consumed_batch_id=? ORDER BY issued_at",
        (batch_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------- batches


def next_batch_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT COALESCE(MAX(CAST(substr(batch_no, 4) AS INTEGER)), 0) m FROM close_batches"
    ).fetchone()
    return f"CB-{int(row['m']) + 1:04d}"


def create_batch(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        '''INSERT INTO close_batches(
             batch_no, business_day, system_total, declared_cash, variance,
             line_count, status, sealed_at, note,
             preview_id, line_fingerprint, caliber_version
           ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
        (
            fields["batch_no"],
            fields["business_day"],
            fields["system_total"],
            fields["declared_cash"],
            fields["variance"],
            fields["line_count"],
            fields["status"],
            fields["sealed_at"],
            fields.get("note"),
            fields.get("preview_id"),
            fields.get("line_fingerprint"),
            fields.get("caliber_version"),
        ),
    )
    return int(cur.lastrowid)


def get_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute("SELECT * FROM close_batches WHERE id=?", (batch_id,)).fetchone()
    return dict(row) if row else None


def void_batch(conn: sqlite3.Connection, batch_id: int, *, reason: str,
               now_iso: str) -> bool:
    cur = conn.execute(
        "UPDATE close_batches SET status='voided', voided_at=?, void_reason=? "
        "WHERE id=? AND status='sealed'",
        (now_iso, reason, batch_id),
    )
    if cur.rowcount != 1:
        return False
    conn.execute(
        "UPDATE variance_rows SET disposition='voided' WHERE batch_id=?",
        (batch_id,),
    )
    conn.execute(
        "UPDATE close_previews SET status='voided' "
        "WHERE consumed_batch_id=? AND status='consumed'",
        (batch_id,),
    )
    return True


def add_variance(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        '''INSERT INTO variance_rows(batch_id, business_day, variance, disposition, created_at)
           VALUES (?,?,?,?,?)''',
        (
            fields["batch_id"],
            fields["business_day"],
            fields["variance"],
            fields["disposition"],
            fields["created_at"],
        ),
    )
    return int(cur.lastrowid)


def get_variance_for_batch(conn: sqlite3.Connection, batch_id: int) -> dict[str, Any] | None:
    row = conn.execute(
        "SELECT * FROM variance_rows WHERE batch_id=? ORDER BY id DESC LIMIT 1",
        (batch_id,),
    ).fetchone()
    return dict(row) if row else None


def list_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM close_batches ORDER BY id DESC").fetchall()]


def list_variances(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()]


def fence_pending_count(conn: sqlite3.Connection) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE disposition='pending'"
    ).fetchone()
    return int(row["c"])


def fence_current_batches(conn: sqlite3.Connection, business_day: str) -> list[dict]:
    """按库内真实状态返回，绝不把 superseded/voided 伪装成 sealed。"""
    rows = conn.execute(
        "SELECT * FROM close_batches WHERE business_day=? ORDER BY id DESC",
        (business_day,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        item["current"] = item.get("status") == "sealed"
        out.append(item)
    return out
