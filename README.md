# Dayseal（日封台）

收银流水按营业日归集，日结封账后锁定；长短款可查。

- 前端 http://127.0.0.1:5700
- API http://127.0.0.1:10700

## 封账策略：流水力 + 预览快照绑定（全局唯一）

全局只贯彻一种权威：**现行流水净额与流水指纹**。预览只是签发时冻结的快照凭证，
不是合计来源；批头、回执、长短款、preview 消费态全部由封账事务内的现行流水派生。

### 预览签发

- `GET /api/close/preview` 返回展示数据的同时签发 `preview_id`，固化当时的
  营业日、笔数、系统合计、口径版本（`caliber_version`）、流水指纹
  （`line_fingerprint`）与实盘声明（可选 `declared_cash`）。
- 令牌 300 秒有效、一次性使用。过期令牌被标记为 `expired`（**不物理删除**，
  保留审计行），此后永久不可再封。

### 封账写批

`POST /api/close/seal` 必须携带 `preview_id`，并在**同一个写事务**内：

1. 校验令牌存在且状态为 `active`、未过期、营业日一致（禁止跨日复用）。
2. 用与签发完全相同的 `_snapshot()` 口径重算现行流水快照，逐项比对
   营业日 / 口径 / 流水指纹 / 笔数 / 合计 / 实盘声明；任一不符返回 409，
   **不落批次**，旧 `preview_id` 也不得再用于封账。
3. 写批次（同时固化 `preview_id`、`line_fingerprint`、`caliber_version`）、
   写长短款、以条件写一次性消费令牌（`status='active' AND 未过期`）。
4. 提交前从库内回读批次 / 长短款 / preview 三表，与快照相校；不一致则
   **整体回滚**，不会出现「回执说一套、批头存另一套」。

回执全部取自库内批次，因此恒有：
`回执合计 == 批头合计 == preview 固化合计 == 封账时刻现行流水净额`。

### 作废与写闸

- 作废（`POST /api/close/batches/{id}/void`）把批次置 `voided`，同一事务里把
  对应长短款置 `voided`、把被消费的 preview 置 `voided`，该营业日的写闸随之恢复。
- `GET /api/close/integrity` 用现行流水逐批复核 sealed 批次：批头指纹/合计/笔数/
  口径、长短款金额与处置、preview 消费态与消费时点（过期后才消费也算撕裂）。
- `POST /api/close/integrity/repair` 批量作废所有不一致的 sealed 批次并恢复写闸。
- 已封账营业日自动锁定：禁止再封、禁止增删改流水；看板同时暴露
  `system_total_today`（批头）与 `live_total_today`（现行流水），用
  `total_consistent` 标注二者是否同源，绝不拿一个数掩盖另一个。

## 验证

```
python backend/tests/test_preview_seal_binding.py
```

覆盖路径：正常封账、流水增删改后旧预览被拒、实盘/口径/跨日/过期/复用被拒、
回执==批头==预览==流水、过期后才消费被审计撕裂并作废、二次消费过期 id 被拒、
封账中途失败整体回滚、作废后三表同源且写闸恢复。

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
