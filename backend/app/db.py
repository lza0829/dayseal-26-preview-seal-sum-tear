from __future__ import annotations
import sqlite3
from app.config import DATA_DIR, DB_PATH


def connect(*, immediate: bool = False) -> sqlite3.Connection:
    """连接默认自动提交；封账等写事务显式 BEGIN IMMEDIATE 串行化。

    之前 isolation_level='EXCLUSIVE' 会让每次读后都隐式开一个排他事务，
    提交后的只读回查也会持锁，容易与预览/写流水互相阻塞。
    """
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, isolation_level=None, timeout=10)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    if immediate:
        conn.execute("BEGIN IMMEDIATE")
    return conn
