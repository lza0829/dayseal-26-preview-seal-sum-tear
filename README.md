# Dayseal（日封台）

收银流水按营业日归集，日结封账后锁定；长短款可查。

- 前端 http://127.0.0.1:5700
- API http://127.0.0.1:10700

## 封账策略：预览快照绑定（全局唯一）

- `GET /api/close/preview` 返回展示数据的同时签发 `preview_id`，固化当时的
  笔数、系统合计、口径版本（`caliber_version`）、流水指纹（`line_fingerprint`）
  与实盘声明（可选 `declared_cash`），令牌 300 秒有效、一次性使用。
- `POST /api/close/seal` 必须携带 `preview_id`。服务端在同一写事务内重算指纹并
  逐项校验：令牌存在且未消费、未过期、营业日一致（禁止跨日复用）、流水指纹/笔数/
  合计未变、口径未变、实盘声明未变；任一不符返回 409 并要求重新预览，不落批次。
- 校验通过后写批次、长短款、消费令牌并提交，回执从库内批次回读，
  保证「回执合计 == 库内批次合计」。
- 已封账营业日自动锁定：禁止再封、禁止增删改流水。

## 验证

```
python backend/tests/test_preview_seal_binding.py
```

## 启动

```powershell
cd backend
py -3.11 -m venv .venv
.\.venv\Scripts\pip install -r requirements.txt
.\.venv\Scripts\uvicorn app.main:app --host 127.0.0.1 --port 10700 --app-dir .
```

```powershell
cd frontend
npm install
npm run dev -- --host 127.0.0.1 --port 5700
```
