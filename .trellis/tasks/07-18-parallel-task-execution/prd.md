# 并行任务执行：多浏览器实例安全并发

## Goal

让「每日一键流水线」能同时驱动多个互不关联的浏览器实例处理不同账号，在 1000+ 条卡数据的场景下显著缩短总耗时，同时保证**任何一条数据不会被两个 worker 同时消费**。

## Background

当前三个任务入口全部单线程串行：
- 批量注册 `run_batch_task` — `src/web/app.py:150`
- 注册+绑卡 `_register_bind_loop` — `src/web/app.py:217`
- 每日一键流水线 `run_daily_pipeline` — `src/web/app.py:450`（docstring 明写"串行跑在单个后台线程"）

浏览器层本身**已经支持并行**：`create_driver`（`src/browser/driver.py:377`）每次独立 `sync_playwright().start()`，走 stdio pipe 无固定端口，`user_data_dir` 按 email 分目录（`driver.py:397`）。真正的阻塞点全在应用层。

## Scope

**In scope**：`run_daily_pipeline` 及其三个阶段（1a 补绑 / 1b 注册 / 2 充值）的并行化，以及为此所需的数据领取、状态隔离、前端展示改造。

**Out of scope**（本任务不改）：
- `run_batch_task` 批量注册入口
- `/api/accounts/recharge` 单账号充值入口
- 多进程 / 多机部署
- 浏览器层 `create_driver` 本身

上述入口继续走现有串行路径，与并行流水线共用 `is_running` 全局互斥。

## Requirements

### R1 并发执行
- R1.1 流水线由 worker 池驱动，并发度 `max_workers` 从 `config.yaml` 读取，默认 **2**，允许范围 **1-4**，越界值夹紧并记日志。
- R1.2 `max_workers=1` 时行为必须与当前串行实现**等价**（回归安全阀）。
- R1.3 并行的基本单位是**账号（email）**，不是卡。同一账号在任一时刻只能被一个 worker 持有。

### R2 数据消费的原子性
- R2.1 `card_bindings` 引入 `processing` 中间态，配 `worker_id` + `claimed_at`。
- R2.2 领取卡必须是原子的「查询+占位」单步操作，禁止 select-then-update 两步。
- R2.3 worker 结束时必须释放未使用的已领取卡（回到 `pending`），不得泄漏。
- R2.4 账号领取需排他，且必须与用户手动打开的浏览器会话（`AppState.open_browsers`，`app.py:62`）互斥双向生效。

### R3 故障回收
- R3.1 `processing` 记录超过 `claim_timeout_minutes`（默认 20）无进展，由回收线程自动重置为 `pending`。
- R3.2 服务启动时把所有残留 `processing` 重置为 `pending`。
- R3.3 回收动作必须写日志，说明回收了哪些记录、原因是超时。

### R4 状态隔离
- R4.1 每个 worker 有独立的 `current_action`、日志缓冲、实时截图帧、活跃 driver 引用。
- R4.2 现有全局 `print` 劫持（`app.py:781`）改为按 worker 路由，日志不得串台。
- R4.3 全局层保留聚合计数（成功/失败总数）与 `is_running`。
- R4.4 停止请求（`force_stop`，`app.py:117`）必须能让**所有** worker 在各自检查点安全退出并关闭各自浏览器。

### R5 前端
- R5.1 工作台按 worker **分栏并列**展示，每栏含独立日志区 + 实时截图。
- R5.2 栏位数量由后端返回的 worker 列表驱动，不写死。
- R5.3 `max_workers=1` 时视觉上与当前单栏布局保持一致。

### R6 反封控
- R6.1 保留阶段 2 现有的「每账号每轮只生成 1 张账单」轮询语义，并行只是让**不同账号**同时推进，不得让**同一账号**在一轮内被多次推进。
- R6.2 保留现有随机间隔逻辑，每个 worker 独立计时。

## Constraints

- C1 **Playwright sync API 线程绑定**：`BrowserSession` 必须在创建它的线程内使用到底，禁止跨线程传递 driver 对象。
- C2 **Chrome profile 单实例**：`_clear_singleton_locks`（`driver.py:429`）无条件删锁，其注释明确依赖"同一 profile 单实例"前提。同一 email 并发会互删锁导致随机崩溃 —— R1.3 是硬约束不是优化。
- C3 **浏览器是有头的**：`headless=False` 硬编码（`driver.py:461`，反检测要求）。每实例约 300-500MB 内存，这是 `max_workers` 上限 4 的由来。
- C4 **单进程**：所有 worker 是同一进程内的线程，共享 `Database` 的单连接 + 全局锁（`database.py:165-166`）。数据库层的原子性由该锁天然保证，不引入多进程。
- C5 数据库变更必须走现有 `_MIGRATIONS` 机制（`database.py:181`），向前兼容已有数据。

## Acceptance Criteria

图例：**[自动]** 已由测试覆盖 · **[待实跑]** 需真实浏览器，只能由用户验证

已通过：

- [x] AC2 **[自动]** 无一张卡被两个 email 绑定成功。
      → `test_daily_pipeline.py::test_pipeline_completes_in_parallel` 断言 `dupes == 0`
- [x] AC3 **[自动/部分]** 串行与并行最终账面一致（成功数/失败数/账号数）。
      → `test_serial_and_parallel_produce_same_ledger`。**仅覆盖账面**，真实浏览器行为仍靠 AC1 佐证
- [x] AC5 **[自动]** 重启后残留 `processing` 全部回到 `pending` 且有日志。
      → `test_reaper.py::test_startup_resets_leftover_processing`
- [x] AC8 **[自动]** 同一 email 的并发领取被拒。
      → `test_registry.py`；已做变异验证：删掉排他逻辑后该文件 4 项断言变红
- [x] AC9 **[结构保证]** 阶段 2 单账号一轮内只推进一次。
      → `pool.map` 的 barrier 语义 + 每轮账号列表无重复；调度语义由 `test_map_is_a_barrier` 覆盖

待用户实跑（自动化测试全部打桩了浏览器，以下无法在测试中体现）：

- [ ] AC1 **[待实跑]** `max_workers: 2` 时确实出现 2 个 Chrome 窗口，各自处理不同账号。
- [ ] AC4 **[待实跑]** kill 掉一个 worker 的 Chrome 后，其卡在 20 分钟后回到 `pending` 并被消费。
      回收逻辑本身已由 `test_reaper.py` 覆盖；待验的是"Chrome 被 kill 后 worker 确实会失联"这一前提。
- [ ] AC6 **[待实跑]** 前端两栏日志互不混杂、截图分别对应各自浏览器。
      日志隔离已由 `test_api_workers.py` 覆盖；待验的是真实截图流与视觉呈现。
- [ ] AC7 **[待实跑]** 并发中点停止，两个浏览器都安全退出关闭，无残留 Chrome 进程，无卡死。

## Open Questions

两个都**仍未解决**，且都只能靠实跑回答：

- OQ1 `claim_timeout_minutes` 默认 20 分钟是否匹配真实单账号耗时？需跑一轮统计 P95。
      定小了会误伤慢 worker（见 design §5 已知限制），定大了失联恢复变慢。
- OQ2 并发是否会因同 IP 请求密度上升触发 Cloudflare 风控？
      **这是本次改造最大的未知数**——所有测试都打桩掉了浏览器，风控行为无从体现。
      若触发，缓解顺序：降 `max_workers` 到 1 → 加大 `batch.interval_*` → 在 R6 基础上加全局节流。
