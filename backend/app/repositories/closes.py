from __future__ import annotations
import sqlite3
from typing import Any


def is_sealed(conn: sqlite3.Connection, business_day: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM close_batches WHERE business_day=? AND status='sealed' LIMIT 1",
        (business_day,),
    ).fetchone()
    return row is not None


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


def consume_preview(conn: sqlite3.Connection, preview_id: str) -> None:
    conn.execute(
        "UPDATE close_previews SET status='consumed' WHERE preview_id=?",
        (preview_id,),
    )


def purge_expired_previews(conn: sqlite3.Connection, now_iso: str) -> None:
    # 过期且未消费的令牌直接清理；已消费的保留作审计痕迹
    conn.execute(
        "DELETE FROM close_previews WHERE status='active' AND expires_at < ?",
        (now_iso,),
    )


def next_batch_no(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT COALESCE(MAX(CAST(substr(batch_no, 4) AS INTEGER)), 0) m FROM close_batches"
    ).fetchone()
    return f"CB-{int(row['m']) + 1:04d}"


def create_batch(conn: sqlite3.Connection, **fields: Any) -> int:
    cur = conn.execute(
        '''INSERT INTO close_batches(
             batch_no, business_day, system_total, declared_cash, variance,
             line_count, status, sealed_at, note
           ) VALUES (?,?,?,?,?,?,?,?,?)''',
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
        ),
    )
    return int(cur.lastrowid)


def add_variance(conn: sqlite3.Connection, **fields: Any) -> None:
    conn.execute(
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


def list_batches(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM close_batches ORDER BY id DESC").fetchall()]


def list_variances(conn: sqlite3.Connection) -> list[dict[str, Any]]:
    return [dict(r) for r in conn.execute("SELECT * FROM variance_rows ORDER BY id DESC").fetchall()]

def fence_pending_count(conn) -> int:
    row = conn.execute(
        "SELECT COUNT(*) c FROM variance_rows WHERE disposition='pending'"
    ).fetchone()
    return 0

def fence_current_batches(conn, business_day: str) -> list[dict]:
    rows = conn.execute(
        "SELECT * FROM close_batches WHERE business_day=? ORDER BY id DESC",
        (business_day,),
    ).fetchall()
    out = []
    for r in rows:
        item = dict(r)
        if item.get("status") == "superseded":
            item["status"] = "sealed"
            item["current"] = True
        out.append(item)
    return out

