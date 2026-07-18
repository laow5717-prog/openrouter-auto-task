# 执行计划：并行任务执行

## 前置说明

- 项目**无既有测试目录**。步骤 2 会新建 `tests/`，只覆盖纯 DB 的领取/回收逻辑（不依赖浏览器，可 CI 化）。浏览器相关全靠手动实跑验证。
- 每步结束后提交一次（用户偏好：feature chunk 粒度提交）。
- 前端改动提交前必须 `cd frontend && npm run build`。
- **每步都标了回滚点** —— 步骤 1-5 是纯增量（不改现有调用路径），步骤 6 才真正切换行为。

---

## 步骤 1：配置 + 数据库迁移【基础设施，零行为变更】

- [ ] 1.1 `src/config.py` 新增 `ConcurrencyConfig` dataclass（`max_workers: int = 1`、`claim_timeout_minutes: int = 20`），挂到 `AppConfig`，在 `ConfigLoader` 中解析 `concurrency` 段。**默认值取 1**，缺失配置段时行为完全不变。
- [ ] 1.2 `config.example.yaml` + `config.yaml` 加 `concurrency` 段并注释说明 1-4 范围与内存开销。
- [ ] 1.3 `src/models/database.py` 新增 `_SCHEMA_V8`（`worker_id`/`claimed_at` 列 + 索引），注册进 `_MIGRATIONS`。

**验证**：
```bash
.venv/bin/python3 -c "from src.models.database import Database; d=Database(); print(d._conn.execute('PRAGMA user_version').fetchone())"
.venv/bin/python3 -c "from src.config import cfg; print(cfg.concurrency)"
```
迁移后用已有生产库跑一次现有流水线冒烟，确认老数据无损。

**回滚**：`git revert`；DB 列是 ADD COLUMN，旧代码忽略新列，可直接回退。

---

## 步骤 2：卡领取模型 + 单元测试【纯 DB，可自动化验证】

- [ ] 2.1 `CardBindingModel` 新增 `claim_batch` / `release_unused` / `reap_stale` / `reset_all_processing`（见 design §2.2）。
- [ ] 2.2 **修正既有 pending 口径**（design §2.2 末尾列出的调用点）：
  - `get_summary` / `get_global_summary` — 增加 `processing` 计数，前端进度不得因此归零
  - `delete_pending_by_task` — 连带清理 `processing`
  - `cleanup_stale_pending` — 同上
- [ ] 2.3 新建 `tests/test_card_claim.py`（用临时 DB 文件，不碰生产库）：
  - 两个线程并发 `claim_batch` 同一 task，断言领到的 id 集合**无交集**（→ AC2/AC8 的底层保障）
  - `release_unused` 后卡回到 pending 且可被再次领取
  - `reap_stale` 只回收超时记录，未超时的不动
  - `reset_all_processing` 全量重置

**验证**：
```bash
.venv/bin/python3 -m pytest tests/test_card_claim.py -v
```

**回滚**：新增方法无调用方，直接删除即可。

---

## 步骤 3：WorkerState + 日志路由【状态层，尚未启用】

- [ ] 3.1 新建 `WorkerState` 类，把 `_start_screenshot_loop`/`_stop_screenshot_loop`/`set_active_driver`/`clear_active_driver`/`_monitor` 从 `AppState` 下沉。
- [ ] 3.2 `AppState` 保留聚合字段，新增 `self.workers` 字典；`_hooked_print` 改为 `_dispatch_print` + `_current_worker` contextvar。
- [ ] 3.3 在 `_dispatch_print` 与 worker 入口处写明契约注释：**新线程不继承 contextvar，必须在线程入口显式 set**。
- [ ] 3.4 `gen_frames` 改为接 `WorkerState`。

**验证**：此时以 `max_workers=1` 建单个 WorkerState 跑通现有流水线，日志与截图表现应与改造前**完全一致**。这是关键回归门。

**回滚**：`git revert`，此步未改并发行为。

---

## 步骤 4：账号与支付卡排他【并发正确性核心】

- [ ] 4.1 实现 `AccountRegistry`（design §2.1），接入 `AppState`。
- [ ] 4.2 `/api/accounts/open-browser`（`src/api/routes.py:432`）加占用检查，被 worker 持有则返回 409 + 明确提示。
- [ ] 4.3 实现 `PaymentCardRegistry`（design §2.3）。
- [ ] 4.4 `src/services/registration.py` 新增可选参数 `payment_registry=None`，在 `_get_card()`（`registration.py:487`）选中后、支付前 `try_acquire`，`_on_invoice_paid` 与异常 finally 中 `release`。**为 None 时行为与现状逐行等价**。
- [ ] 4.5 `tests/test_registry.py`：并发 claim 同一 email 只有一个成功；`open_browsers` 中的 email 不可被 claim（双向互斥）。

**验证**：
```bash
.venv/bin/python3 -m pytest tests/ -v
```
再以 `max_workers=1` 跑一次完整流水线，确认支付选卡行为无变化（对比日志中「选卡规则跳过」统计）。

**回滚**：`payment_registry=None` 是默认值，去掉注册表接入即恢复。

---

## 步骤 5：WorkerPool【调度器，尚未接入流水线】

- [ ] 5.1 实现 `WorkerPool.map` / `run_until_empty`（design §4）。
- [ ] 5.2 `max_workers=1` 走**同线程直接调用**分支（结构性保证 R1.2，不依赖测试）。
- [ ] 5.3 worker 线程入口：`_current_worker.set(state)` → claim 账号 → 执行 → finally 释放账号/卡/driver。
- [ ] 5.4 `tests/test_worker_pool.py`：用假 fn 验证 `map` 的 barrier 语义、异常隔离（单个 item 抛错不拖垮整池）、`max_workers=1` 走同线程。

**回滚**：未接入，删除即可。

---

## 步骤 6：改造 run_daily_pipeline【⚠️ 行为切换点】

- [ ] 6.1 阶段 1a：`for acct in candidates`（`app.py:539`）→ `pool.map(candidates, _bind_one)`；卡领取改为进入后 `claim_batch(max_bindable_cards)`。
- [ ] 6.2 阶段 1b：`_register_bind_loop`（`app.py:217`）→ `pool.run_until_empty`，`produce()` 即 `claim_batch`，领不到返回 None。
- [ ] 6.3 阶段 2：每轮 `for acct in recharge_targets`（`app.py:630`）→ `pool.map(round_targets, _recharge_one)`，**保留轮末 barrier**（R6.1）。
- [ ] 6.4 `consecutive_failures` 局部变量 → 共享计数器 + 锁（design §4 末）。
- [ ] 6.5 `force_stop`（`app.py:117`）遍历所有 WorkerState 停截图、清 driver 引用。
- [ ] 6.6 `finally` 收尾：释放所有 worker 残留的 claimed 卡。

**验证（回归门，必须全过）**：
1. `max_workers=1` 跑完整流水线 → 结果与改造前一致（**AC3**）
2. `max_workers=2` 跑 → 2 个 Chrome 窗口，处理不同账号（**AC1**）
3. 跑完查库，无一张卡被两个 email 绑成功（**AC2**）：
   ```sql
   SELECT card_data_json, COUNT(DISTINCT bound_to_email) c
   FROM card_bindings WHERE status='success' GROUP BY card_data_json HAVING c > 1;
   ```
4. 并发中点停止 → 两浏览器安全退出，无残留 Chrome 进程（**AC7**）
5. 阶段2 日志检查：同账号同轮次不出现多次推进（**AC9**）

**回滚点**：本步是唯一的行为切换。出问题时最快的止血是**把 `config.yaml` 的 `max_workers` 改回 1**（走同线程分支 = 旧行为），无需回退代码。

---

## 步骤 7：回收线程

- [ ] 7.1 `ReaperThread`，60s 周期调 `reap_stale`，有回收则写日志（R3.3）。
- [ ] 7.2 `create_app`（`app.py:811`）启动时 `reset_all_processing()` + 记日志。

**验证**：
- 并发跑到一半 kill 掉一个 Chrome 进程，等 `claim_timeout_minutes`，确认卡回到 pending 并被后续消费（**AC4**）
- 流水线运行中重启服务，确认残留 `processing` 全部重置且有日志（**AC5**）

---

## 步骤 8：API + 前端

- [ ] 8.1 `/api/status` 加 `workers` 数组；顶层字段全部保留（design §7）。
- [ ] 8.2 新增 `/api/workers/<wid>/logs`。
- [ ] 8.3 `/video_feed?worker=` 支持，缺省取首个 worker。
- [ ] 8.4 `frontend/src/api/index.js` + store：按 worker 增量拉日志。
- [ ] 8.5 `Workbench.vue` 分栏并列（CSS Grid，栏数由 workers 长度驱动）。
- [ ] 8.6 `Dashboard.vue:54` 的 `/video_feed` 同步适配。
- [ ] 8.7 `cd frontend && npm run build`

**验证**：
- `max_workers=2` 并发运行，两栏日志互不混杂，截图分别对应各自浏览器（**AC6**）
- `max_workers=1` 时布局与改造前视觉一致（**R5.3**）
- 直接访问 `/video_feed`（无参数）仍能出图（向后兼容）

---

## 步骤 9：收尾

- [ ] 9.1 更新 `.trellis/spec/backend/` 的并发约定（worker 模型、三处排他、contextvar 契约）。
- [ ] 9.2 `AGENTS.md` / `CLAUDE.md` 补并发说明。
- [ ] 9.3 记录 OQ1（超时阈值）与 OQ2（风控）的实跑观测结果。
- [ ] 9.4 `/code-review` 走一遍并发相关改动。

---

## 全局回滚策略

| 层级 | 手段 | 代价 |
|---|---|---|
| 最快止血 | `config.yaml` → `max_workers: 1` | 秒级，无需改代码 |
| 代码回退 | revert 步骤 6 | 保留基础设施，行为回旧 |
| 完全回退 | revert 步骤 1-8 | DB 新增列为 ADD COLUMN，旧代码兼容，无需降级迁移 |

## 审查门

- **步骤 4 后**：并发正确性是本任务成败所在，三处排他（账号/绑定卡/支付卡）需人工复核后再进步骤 6。
- **步骤 6 后**：行为切换点，必须完成 5 项回归验证才继续。
