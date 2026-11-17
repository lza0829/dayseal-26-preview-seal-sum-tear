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
# 注入故障时仍需读状态码而不是抛异常
tolerant_client = TestClient(app, raise_server_exceptions=False)
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
    check("过期令牌未被消费（终态 expired，可审计且永不可复用）",
          db_preview(pv["preview_id"])["status"] == "expired")
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


# ---------------------------------------------------------------- 三表同源


def db_variance(batch_id: int):
    conn = connect()
    try:
        row = conn.execute(
            "SELECT * FROM variance_rows WHERE batch_id=? ORDER BY id DESC LIMIT 1",
            (batch_id,),
        ).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


def integrity():
    r = client.get("/api/close/integrity")
    assert r.status_code == 200, r.text
    return r.json()


def test_receipt_matches_batch_and_preview():
    """回执必须与库内批头、preview 固化表、现行流水三者一致，不能各说各话。"""
    day = fresh_day()
    add_line(60.0)
    add_line(40.0)
    pv = preview(declared=99.5)          # 实盘略少 0.5 -> 短款
    r = seal(pv["preview_id"], 99.5, day)
    check("封账成功", r.status_code == 200, r.text)
    receipt = r.json()
    batch = db_batch(day)
    var = db_variance(batch["id"])
    pv_row = db_preview(pv["preview_id"])

    live_total = sum(x["amount"] for x in client.get("/api/lines", params={"business_day": day}).json())
    check("回执合计==批头==预览==现行流水",
          receipt["system_total"] == batch["system_total"] == pv["system_total"] == live_total == 100.0)
    check("回执笔数==批头==预览==现行笔数",
          receipt["line_count"] == batch["line_count"] == pv["line_count"] == 2)
    check("批头绑定 preview_id", batch["preview_id"] == pv["preview_id"])
    check("批头固化流水指纹==预览指纹", batch["line_fingerprint"] == pv["line_fingerprint"])
    check("批头固化口径==预览口径", batch["caliber_version"] == pv["caliber_version"])
    check("回执长短款==批头==长短款表",
          receipt["variance"] == batch["variance"] == var["variance"] == -0.5)
    check("长短款处置与差额一致", var["disposition"] == "pending")
    check("preview 消费态指向该批次",
          pv_row["status"] == "consumed" and pv_row["consumed_batch_id"] == batch["id"])
    report = integrity()
    own = next(r for r in report["batches"] if r["batch_id"] == batch["id"])
    check("完整性审计：该批无撕裂", own["mismatches"] == [], str(own))


def _insert_legacy_bad_batch(day: str, *, total: float, preview_status: str = "consumed",
                             expires_at: str = "2000-01-01T00:00:00Z") -> tuple[int, str]:
    """模拟修复前的事故现场：过期 preview 仍被消费落成 sealed，且批头合计与流水不符。"""
    import uuid as _uuid

    pid = "PV-legacy-" + _uuid.uuid4().hex
    conn = connect(immediate=True)
    try:
        conn.execute(
            '''INSERT INTO close_previews(
                 preview_id, business_day, line_count, system_total, declared_cash,
                 line_fingerprint, caliber_version, issued_at, expires_at, status
               ) VALUES (?,?,?,?,?,?,?,?,?,?)''',
            (pid, day, 1, total, total, "stale-fingerprint",
             close_service.CALIBER_VERSION, "2020-01-01T00:00:00Z", expires_at, preview_status),
        )
        cur = conn.execute(
            '''INSERT INTO close_batches(
                 batch_no, business_day, system_total, declared_cash, variance,
                 line_count, status, sealed_at, note,
                 preview_id, line_fingerprint, caliber_version
               ) VALUES (?,?,?,?,?,?,?,?,?,?,?,?)''',
            ("CB-LEGACY-" + _uuid.uuid4().hex[:6], day, total, total, 0.0,
             1, "sealed", "2020-01-01T00:00:00Z", "legacy",
             pid, "stale-fingerprint", close_service.CALIBER_VERSION),
        )
        batch_id = int(cur.lastrowid)
        conn.execute(
            '''INSERT INTO variance_rows(batch_id, business_day, variance, disposition, created_at)
               VALUES (?,?,?,?,?)''',
            (batch_id, day, 0.0, "balanced", "2020-01-01T00:00:00Z"),
        )
        if preview_status == "consumed":
            conn.execute(
                "UPDATE close_previews SET consumed_at=?, consumed_batch_id=? WHERE preview_id=?",
                ("2020-01-01T00:00:00Z", batch_id, pid),
            )
        conn.commit()
        return batch_id, pid
    finally:
        conn.close()


def test_stale_preview_seal_voided_and_gate_reopened():
    """过期 preview 落成的 sealed：必须作废、长短款同源、恢复写闸，且旧 id 永久失效。"""
    day = fresh_day()
    add_line(50.0)
    batch_id, pid = _insert_legacy_bad_batch(day, total=999.0)
    check("事故批次存在时该日被锁", client.post(
        "/api/lines", json={"till_code": "TILL-A", "amount": 1}).status_code == 409)

    report = integrity()
    row = next(r for r in report["batches"] if r["batch_id"] == batch_id)
    check("审计判定该批与现行流水撕裂", row["voidable"] and row["mismatches"], str(row))

    r = client.post(f"/api/close/batches/{batch_id}/void", json={"reason": "tests"})
    check("作废成功", r.status_code == 200, r.text)
    check("作废后写闸恢复", client.post(
        "/api/lines", json={"till_code": "TILL-A", "amount": 1}).status_code == 200)
    check("作废批次不再锁日", client.get("/api/dashboard").json()["sealed_today"] is False)
    batch = db_batch(day)
    check("批次状态为 voided", batch["status"] == "voided")
    check("长短款同源作废", db_variance(batch_id)["disposition"] == "voided")
    check("preview 同源作废", db_preview(pid)["status"] == "voided")
    check("作废后完整性审计清零",
          all(not r["mismatches"] for r in integrity()["batches"]))

    # 旧 preview 已随批次作废，永不可再封
    r_old = seal(pid, 50.0, day)
    check("随批作废的 preview 不可再封", r_old.status_code == 409, r_old.text)


def test_repair_endpoint_voids_all_inconsistent():
    """流水力修复入口：批量作废所有与现行流水不一致的 sealed 并恢复写闸。"""
    day = fresh_day()
    add_line(30.0)
    batch_id, pid = _insert_legacy_bad_batch(day, total=1234.0)
    r = client.post("/api/close/integrity/repair", json={"business_day": day})
    check("修复接口 200", r.status_code == 200, r.text)
    body = r.json()
    check("修复接口作废了事故批次", [v["batch_id"] for v in body["voided"]] == [batch_id], str(body))
    check("修复后无残留撕裂", body["remaining_inconsistent"] == 0, str(body))
    check("修复后写闸恢复", body["write_gate"]["sealed"] is False and body["write_gate"]["any_open"])
    check("修复后可按新预览正常封账",
          seal(preview(declared=30.0)["preview_id"], 30.0, day).status_code == 200)


def test_second_seal_cannot_leave_partial_batch():
    """已 sealed 时再封（新令牌）必须整体拒绝，绝不能留下第二份/半截批次。"""
    day = fresh_day()
    add_line(80.0)
    pv1 = preview(declared=80.0)
    check("首次封账成功", seal(pv1["preview_id"], 80.0, day).status_code == 200)
    pv2 = preview(declared=80.0)
    before = len(client.get("/api/close/batches").json())
    r = seal(pv2["preview_id"], 80.0, day)
    check("已封日再封被拒", r.status_code == 409, r.text)
    after = client.get("/api/close/batches").json()
    check("未新增批次", len(after) == before)
    check("第二次令牌未被消费（仍 active）", db_preview(pv2["preview_id"])["status"] == "active")


def test_mid_transaction_failure_leaves_no_sealed():
    """封账中途（长短款写入失败 / 回读不一致）必须整体回滚：无 sealed、无长短款、令牌未消费。"""
    from app.repositories import closes as repo

    day = fresh_day()
    add_line(45.0)
    pv = preview(declared=45.0)
    original = repo.add_variance

    def boom(*a, **kw):
        raise RuntimeError("injected variance failure")

    repo.add_variance = boom
    variances_before = len(client.get("/api/close/variances").json())
    batches_before = len(client.get("/api/close/batches").json())
    try:
        r = tolerant_client.post("/api/close/seal", json={
            "preview_id": pv["preview_id"], "declared_cash": 45.0, "business_day": day,
        })
    finally:
        repo.add_variance = original
    check("中途失败返回 500", r.status_code == 500, r.text)
    check("无 sealed 批次残留", db_batch(day) is None)
    check("批次总数未增加", len(client.get("/api/close/batches").json()) == batches_before)
    check("无长短款残留",
          len(client.get("/api/close/variances").json()) == variances_before)
    check("令牌仍 active（未被半截消费）", db_preview(pv["preview_id"])["status"] == "active")
    check("重试可正常封账", seal(pv["preview_id"], 45.0, day).status_code == 200)

    # 回读不一致：库内批头被篡改时，封账必须回滚而不是“回执跟批头各说各话”
    day2 = fresh_day()
    add_line(55.0)
    pv2 = preview(declared=55.0)
    original_get = repo.get_batch

    def tampered(conn, batch_id):
        row = original_get(conn, batch_id)
        if row:
            row = dict(row)
            row["system_total"] = row["system_total"] + 1
        return row

    repo.get_batch = tampered
    try:
        r2 = tolerant_client.post("/api/close/seal", json={
            "preview_id": pv2["preview_id"], "declared_cash": 55.0, "business_day": day2,
        })
    finally:
        repo.get_batch = original_get
    check("回读不一致返回 500", r2.status_code == 500, r2.text)
    check("回读不一致整体回滚：无批次", db_batch(day2) is None)
    check("回读不一致整体回滚：令牌未消费", db_preview(pv2["preview_id"])["status"] == "active")
    check("去掉注入后可封", seal(pv2["preview_id"], 55.0, day2).status_code == 200)


def test_sealed_after_preview_expiry_detected():
    """过期 preview 落成的 sealed，即使合计凑巧与流水一致，也必须被判撕裂并作废。"""
    day = fresh_day()
    add_line(15.0)
    batch_id, pid = _insert_legacy_bad_batch(day, total=15.0)
    # 补齐批头，使其合计/笔数与现行流水一致，只剩“过期后才消费”这一处撕裂
    conn = connect(immediate=True)
    try:
        from app.services import close_service as _svc
        snap = _svc._snapshot(conn, day)
        conn.execute(
            "UPDATE close_batches SET line_fingerprint=?, caliber_version=?, line_count=? WHERE id=?",
            (snap["line_fingerprint"], snap["caliber_version"], snap["line_count"], batch_id),
        )
        conn.commit()
    finally:
        conn.close()
    report = integrity()
    row = next(r for r in report["batches"] if r["batch_id"] == batch_id)
    check("过期后才消费被判撕裂", any("过期之后" in m for m in row["mismatches"]), str(row))
    r = client.post("/api/close/integrity/repair", json={"business_day": day})
    check("修复作废该批", [v["batch_id"] for v in r.json()["voided"]] == [batch_id], r.text)
    check("恢复写闸", r.json()["write_gate"]["sealed"] is False)
    check("preview 随批作废", db_preview(pid)["status"] == "voided")


def test_expired_id_never_consumed_twice():
    """过期 id 一次失败后永久失效：不会被后续预览“顺带清理”复活，也不能二次消费。"""
    day = fresh_day()
    add_line(20.0)
    pv = preview(declared=20.0)
    conn = connect(immediate=True)
    try:
        conn.execute("UPDATE close_previews SET expires_at=? WHERE preview_id=?",
                     ("2000-01-01T00:00:00Z", pv["preview_id"]))
        conn.commit()
    finally:
        conn.close()
    check("过期后第一次封账被拒", seal(pv["preview_id"], 20.0, day).status_code == 409)
    fresh = preview(declared=20.0)               # 再签发新预览（旧实现会 DELETE 旧行）
    check("旧 id 行仍在（审计痕迹，不物理删除）", db_preview(pv["preview_id"]) is not None)
    check("旧 id 状态为 expired", db_preview(pv["preview_id"])["status"] == "expired")
    check("过期 id 二次封账仍被拒", seal(pv["preview_id"], 20.0, day).status_code == 409)
    check("旧 id 未被误消费", db_preview(pv["preview_id"])["status"] == "expired")
    r = seal(fresh["preview_id"], 20.0, day)
    check("新预览可正常封账", r.status_code == 200, r.text)
    check("封账后新 id 已消费", db_preview(fresh["preview_id"])["status"] == "consumed")
    check("旧 id 仍为 expired", db_preview(pv["preview_id"])["status"] == "expired")


def test_write_gate_snapshot_reflects_db():
    """写闸/绕过矩阵必须反映库内事实，不再恒真。"""
    day = fresh_day()
    add_line(10.0)
    fence = client.get("/api/dashboard/fence").json()
    check("未封账：写闸为开", fence["writable"] is True, str(fence))
    pv = preview(declared=10.0)
    check("封账成功", seal(pv["preview_id"], 10.0, day).status_code == 200)
    fence = client.get("/api/dashboard/fence").json()
    check("封账后：写闸全关", all(v is False for v in fence["write_gate"].values()), str(fence))
    check("封账后：writable=False", fence["writable"] is False)
    dash = client.get("/api/dashboard").json()
    check("看板合计取自批头而非流水净额",
          dash["sealed_today"] is True and dash["system_total_today"] == 10.0)
    check("看板同时暴露现行流水净额", dash["live_total_today"] == 10.0)
    check("看板不隐瞒撕裂", dash["total_consistent"] is True, str(dash))


def test_dashboard_exposes_tampered_batch_total():
    """批头被改成与流水不符时，看板必须同时给出两个数并标注不一致。"""
    day = fresh_day()
    add_line(40.0)
    check("封账成功", seal(preview(declared=40.0)["preview_id"], 40.0, day).status_code == 200)
    conn = connect(immediate=True)
    try:
        conn.execute("UPDATE close_batches SET system_total=41.0 WHERE business_day=?", (day,))
        conn.commit()
    finally:
        conn.close()
    try:
        dash = client.get("/api/dashboard").json()
        check("批头合计不被掩盖", dash["batch_total_today"] == 41.0, str(dash))
        check("现行流水净额仍可见", dash["live_total_today"] == 40.0, str(dash))
        check("撕裂被标注", dash["total_consistent"] is False, str(dash))
    finally:
        # 复原，避免污染共享测试库中的其他用例
        conn = connect(immediate=True)
        try:
            conn.execute("UPDATE close_batches SET system_total=40.0 WHERE business_day=?", (day,))
            conn.commit()
        finally:
            conn.close()


def test_dashboard_counts_do_not_disguise_voided():
    """作废批次不得被展示层改写为 sealed，也不计入已封。"""
    day = fresh_day()
    add_line(15.0)
    batch_id, _ = _insert_legacy_bad_batch(day, total=15.0)
    client.post(f"/api/close/batches/{batch_id}/void", json={"reason": "counts"})
    ext = client.get("/api/dashboard/extended").json()
    check("voided 单独计数", ext["voided_batch_count"] >= 1, str(ext))
    check("voided 不计入 sealed_days", day not in ext["sealed_days"], str(ext))
    rows = client.get("/api/close/batches").json()
    row = next(b for b in rows if b["id"] == batch_id)
    check("批次状态原样展示为 voided", row["status"] == "voided")


if __name__ == "__main__":
    tests = [v for k, v in sorted(globals().items()) if k.startswith("test_") and callable(v)]
    for t in tests:
        print(f"\n=== {t.__name__} ===")
        t()
    print(f"\n总计 {len(PASS)} 通过 / {len(FAIL)} 失败；隔离库位于 {_TMP}")
    sys.exit(1 if FAIL else 0)
