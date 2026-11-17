"""封账预览-封账绑定验证（全局策略：预览快照绑定）。

运行：
  DAYSEAL_DATA_DIR 由本脚本自动指向临时目录，直接执行即可：
  python backend/tests/test_preview_seal_binding.py
也可被 pytest 收集（函数名 test_ 开头，断言失败即非零退出）。
"""
from __future__ import annotations

import os
import sys
import tempfile
import uuid
from pathlib import Path

# 必须在导入应用配置前指定隔离数据目录
_TMP = Path(tempfile.mkdtemp(prefix="dayseal-test-"))
os.environ["DAYSEAL_DATA_DIR"] = str(_TMP)

BACKEND = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(BACKEND))

from fastapi.testclient import TestClient  # noqa: E402

from app.main import app  # noqa: E402
from app.repositories import closes as close_repo  # noqa: E402
from app.db import connect  # noqa: E402
from app.services import close_service  # noqa: E402

client = TestClient(app)
PASS, FAIL = [], []
_DAY_SEQ = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    (PASS if cond else FAIL).append(name)
    print(f"{'PASS' if cond else 'FAIL'}  {name}" + (f"  -- {detail}" if detail else ""))
    assert cond, f"{name}: {detail}"


def fresh_day() -> str:
    global _DAY_SEQ
    _DAY_SEQ += 1
    # 使用 12 月日期，避开种子数据所在的 11-15/11-16，保证每个用例营业日干净
    day = "2026-12-" + f"{_DAY_SEQ:02d}"
    client.put("/api/settings/business-day", json={"business_day": day})
    return day


def add_line(amount: float, method: str = "现金") -> dict:
    r = client.post("/api/lines", json={"till_code": "TILL-A", "amount": amount, "pay_method": method})
    assert r.status_code == 200, r.text
    return r.json()


def preview(day: str | None = None, declared: float | None = None):
    params = {}
    if day:
        params["business_day"] = day
    if declared is not None:
        params["declared_cash"] = declared
    r = client.get("/api/close/preview", params=params)
    assert r.status_code == 200, r.text
    return r.json()


def seal(preview_id=None, declared=None, day=None):
    body = {"declared_cash": declared}
    if preview_id is not None:
        body["preview_id"] = preview_id
    if day is not None:
        body["business_day"] = day
    return client.post("/api/close/seal", json=body)


def db_batch(day: str):
    conn = connect()
    try:
        rows = conn.execute(
            "SELECT * FROM close_batches WHERE business_day=? ORDER BY id DESC LIMIT 1", (day,)
        ).fetchall()
        return dict(rows[0]) if rows else None
    finally:
        conn.close()


def db_preview(pid: str):
    conn = connect()
    try:
        row = conn.execute("SELECT * FROM close_previews WHERE preview_id=?", (pid,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def test_happy_path_matches_preview():
    day = fresh_day()
    add_line(100.0)
    add_line(50.5)
    pv = preview(declared=150.5)
    r = seal(pv["preview_id"], 150.5, day)
    check("正常封账 200", r.status_code == 200, r.text)
    data = r.json()
    check("封账合计==预览合计", data["system_total"] == pv["system_total"] == 150.5)
    check("封账笔数==预览笔数", data["line_count"] == pv["line_count"] == 2)
    batch = db_batch(day)
    check("回执合计==库内批次合计", data["system_total"] == batch["system_total"])
    check("回执笔数==库内批次笔数", data["line_count"] == batch["line_count"])
    check("回执实盘==库内实盘", data["declared_cash"] == batch["declared_cash"])
    check("令牌已消费", db_preview(pv["preview_id"])["status"] == "consumed")
    check("日锁定：再封被拒", seal(pv["preview_id"], 150.5, day).status_code == 409)
    check("日锁定：加流水被拒", client.post("/api/lines", json={"till_code": "TILL-A", "amount": 1}).status_code == 409)


def test_line_changed_after_preview_blocked():
    day = fresh_day()
    add_line(100.0)
    pv = preview(declared=100.0)
    # 预览之后改一笔流水金额
    line = client.get("/api/lines", params={"business_day": day}).json()[0]
    patched = client.patch(f"/api/lines/{line['id']}", json={"amount": 120.0})
    check("预览后可改流水（未封账）", patched.status_code == 200, patched.text)
    r = seal(pv["preview_id"], 100.0, day)
    check("过期指纹封账被拒(409)", r.status_code == 409, r.text)
    check("未产生批次", db_batch(day) is None)
    # 重新预览后按新合计可封，且与新预览一致
    pv2 = preview(declared=120.0)
    r2 = seal(pv2["preview_id"], 120.0, day)
    check("重新预览后可封", r2.status_code == 200, r2.text)
    check("封账合计与第二次预览一致", r2.json()["system_total"] == pv2["system_total"] == 120.0)
    check("封账合计不等于第一次预览", r2.json()["system_total"] != pv["system_total"])


def test_line_deleted_after_preview_blocked():
    day = fresh_day()
    add_line(80.0)
    add_line(20.0)
    pv = preview(declared=100.0)
    line = client.get("/api/lines", params={"business_day": day}).json()[0]
    client.delete(f"/api/lines/{line['id']}")
    r = seal(pv["preview_id"], 100.0, day)
    check("删流水后旧预览封账被拒", r.status_code == 409, r.text)
    check("未产生批次", db_batch(day) is None)


def test_line_added_after_preview_blocked():
    day = fresh_day()
    add_line(40.0)
    pv = preview(declared=40.0)
    add_line(60.0)
    r = seal(pv["preview_id"], 40.0, day)
    check("加流水后旧预览封账被拒", r.status_code == 409, r.text)
    check("未产生批次", db_batch(day) is None)


def test_declared_changed_after_preview_blocked():
    day = fresh_day()
    add_line(100.0)
    pv = preview(declared=100.0)
    r = seal(pv["preview_id"], 99.0, day)
    check("实盘与预览声明不符被拒", r.status_code == 409, r.text)
    check("未产生批次", db_batch(day) is None)


def test_missing_or_unknown_preview_id():
    day = fresh_day()
    add_line(10.0)
    check("无 preview_id 被拒(400)", seal(declared=10.0, day=day).status_code == 400)
    check("伪造 preview_id 被拒(400)", seal("PV-" + uuid.uuid4().hex, 10.0, day).status_code == 400)


def test_preview_reuse_blocked():
    day = fresh_day()
    add_line(70.0)
    pv = preview(declared=70.0)
    r1 = seal(pv["preview_id"], 70.0, day)
    check("首次封账成功", r1.status_code == 200, r1.text)
    r2 = seal(pv["preview_id"], 70.0, day)
    check("同一 preview_id 重复封账被拒", r2.status_code == 409, r2.text)


def test_cross_day_preview_blocked():
    day_a = fresh_day()
    add_line(100.0)
    pv = preview(day=day_a, declared=100.0)
    day_b = fresh_day()  # 切换营业日
    add_line(5.0)
    r = seal(pv["preview_id"], 100.0, day_b)
    check("跨日复用 preview_id 被拒", r.status_code == 409, r.text)
    check("A 日未被误封", db_batch(day_a) is None)
    check("B 日未被误封", db_batch(day_b) is None)
    # 令牌绑定的是原营业日：回到 A 日且 A 日流水未变，仍可正常封账
    r_back = seal(pv["preview_id"], 100.0, day_a)
    check("令牌回原营业日仍有效（非跨日令牌）", r_back.status_code == 200, r_back.text)
    check("仅 A 日被封，B 日未封", db_batch(day_a) is not None and db_batch(day_b) is None)


def test_expired_preview_blocked():
    day = fresh_day()
    add_line(100.0)
    pv = preview(declared=100.0)
    # 手工把库内过期时间改到过去，模拟 TTL 到期
    conn = connect(immediate=True)
    try:
        conn.execute(
            "UPDATE close_previews SET expires_at=? WHERE preview_id=?",
            ("2000-01-01T00:00:00Z", pv["preview_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    r = seal(pv["preview_id"], 100.0, day)
    check("过期 preview_id 封账被拒", r.status_code == 409, r.text)
    check("过期令牌未被消费", db_preview(pv["preview_id"])["status"] == "active")
    check("未产生批次", db_batch(day) is None)


def test_caliber_change_blocked():
    day = fresh_day()
    add_line(100.0)
    pv = preview(declared=100.0)
    # 模拟口径版本升级：改库内存档口径，重算口径与之不同
    conn = connect(immediate=True)
    try:
        conn.execute(
            "UPDATE close_previews SET caliber_version=? WHERE preview_id=?",
            ("cal-v0:legacy", pv["preview_id"]),
        )
        conn.commit()
    finally:
        conn.close()
    r = seal(pv["preview_id"], 100.0, day)
    check("口径变更后封账被拒", r.status_code == 409, r.text)
    check("未产生批次", db_batch(day) is None)


def test_strategy_a_counterfactual():
    """对照策略「封瞬间重算」的语义（仅推演，不封账）：
    预览后改流水，策略 A 允许直接封，批次以封瞬间重算为准。
    """
    day = fresh_day()
    add_line(100.0)
    pv_before = preview(declared=100.0)
    line = client.get("/api/lines", params={"business_day": day}).json()[0]
    client.patch(f"/api/lines/{line['id']}", json={"amount": 120.0})
    pv_after = preview()
    print("\n[两策略对照] 预览 100.0 后把流水改成 120.0：")
    print("  策略A 封瞬间重算：允许封，批次合计 =", pv_after["system_total"],
          "（与第一次预览", pv_before["system_total"], "不一致，但该策略允许）")
    print("  策略B 预览快照绑定：旧 preview_id 封账被拒，必须重新预览；",
          "本系统实测拒绝 ->",
          seal(pv_before["preview_id"], 100.0, day).status_code == 409)
    check("策略B实测：旧快照封账拒绝", seal(pv_before["preview_id"], 100.0, day).status_code == 409)
    check("策略B重预览合计=120", pv_after["system_total"] == 120.0)


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n=== {t.__name__} ===")
        t()
    print(f"\n总计 {len(PASS)} 通过 / {len(FAIL)} 失败；隔离库位于 {_TMP}")
    sys.exit(1 if FAIL else 0)
