# 技术设计：并行任务执行

## 1. 总体思路

单进程 + worker 线程池。**并行单位是账号（email）**，卡是被账号消费的资源。

```
run_daily_pipeline (协调线程)
  ├─ 阶段0  准备卡池                        [串行，纯 DB]
  ├─ 阶段1a 补绑已有账号   → WorkerPool.map(candidates)
  ├─ 阶段1b 注册新号       → WorkerPool.run_until_empty()
  └─ 阶段2  轮询式充值     → 逐轮 WorkerPool.map(round_targets)
                              轮末 barrier（保证 R6.1 每账号每轮一次）
```

协调线程只做调度、阶段切换、聚合统计，**自己不碰浏览器**。每个 worker 线程内部完整持有 `create_driver → 操作 → close_driver` 的生命周期（约束 C1）。

## 2. 并发正确性：三个资源的排他

这是本设计的核心。有**三处**共享资源会被并发争抢，其中第三处最隐蔽。

### 2.1 账号（email）— 内存排他

驱动因素是约束 C2：同一 profile 目录并发会被 `_clear_singleton_locks` 互删锁。

单进程内用 `AccountRegistry`（`threading.Lock` + `set`）即可，不落库：

```python
class AccountRegistry:
    def __init__(self, app_state):
        self._lock = threading.Lock()
        self._claimed = set()          # 被 worker 持有的 email
        self._app_state = app_state    # 读 open_browsers

    def claim(self, email) -> bool:
        with self._lock:
            if email in self._claimed or email in self._app_state.open_browsers:
                return False
            self._claimed.add(email)
            return True

    def release(self, email):
        with self._lock:
            self._claimed.discard(email)
```

R2.4 的双向互斥：`/api/accounts/open-browser`（`src/api/routes.py:432`）在开浏览器前须调用 `registry.is_claimed(email)`，被占用则返回 409。

### 2.2 绑定卡（card_bindings）— DB 原子占位

**关键简化**：所有 worker 共享 `Database` 的单连接 + 单个 `threading.Lock`（`database.py:165-166`，约束 C4）。`db.execute()` 天然串行，所以一条 `UPDATE` 语句就是原子的，**不需要 `BEGIN IMMEDIATE` 或乐观锁重试**。

Schema 迁移 `_SCHEMA_V8`：
```sql
ALTER TABLE card_bindings ADD COLUMN worker_id TEXT DEFAULT '';
ALTER TABLE card_bindings ADD COLUMN claimed_at TEXT;
CREATE INDEX IF NOT EXISTS idx_cb_status_claimed ON card_bindings(status, claimed_at);
```

状态机：`pending → processing → success | failed`，另有 `processing → pending`（释放/回收）。

新增 `CardBindingModel` 方法：

```python
def claim_batch(self, task_id, worker_id, limit):
    """原子领取至多 limit 张 pending 卡，返回已领取记录（含解析后的 card）。"""
    self.db.execute(
        """UPDATE card_bindings
           SET status='processing', worker_id=?, claimed_at=datetime('now','localtime')
           WHERE id IN (
               SELECT id FROM card_bindings
               WHERE task_id=? AND status='pending' ORDER BY id LIMIT ?
           )""",
        (worker_id, task_id, limit),
    )
    # 回读本 worker 刚占住的记录
    return self._fetch_processing(task_id, worker_id)

def release_unused(self, task_id, worker_id):
    """把该 worker 仍处于 processing 的卡退回 pending，返回行数。"""

def reap_stale(self, timeout_minutes):
    """回收超时的 processing → pending，返回行数。"""

def reset_all_processing(self):
    """启动时全量重置 processing → pending，返回行数。"""
```

**领取粒度**：每个账号一次领 `max_bindable_cards` 张（而非现在的传全量 pending）。这是对现有语义的收敛 —— 现状把整个 pending 列表传进 `bind_cards_to_existing_account`，靠函数内部绑够就停；并行下必须改成预先领定额，否则无法排他。

**注意副作用**：现有代码多处按 `status='pending'` 统计与清理，全部需要连带处理：
- `get_summary` / `get_global_summary`（`card_binding.py:280,221`）—— `processing` 需计入"处理中"或归并到 pending，前端进度条不能因此归零
- `delete_pending_by_task`（`card_binding.py:255`）—— 收尾时须同时清理 `processing`
- `cleanup_stale_pending`（`card_binding.py:263`）—— 同上

### 2.3 支付卡（card_pool）— 运行时 in-flight 排他【易漏】

`registration.py:456-478` 的 `_eligible()` 闸门实现三条规则：R1 一卡绑一账号、R2 单卡 24h≤2 次、R3 3DS 冷却。三条**全部从 DB 实时派生**，且 `_eligible_cards` 在进入时**一次性快照**。

并发下的失效路径：worker A 与 B 同时进入 → 都快照到卡 X 合格 → A 用 X 付账号1、B 用 X 付账号2 → 违反 R1，且 DB 事后才记录，闸门形同虚设。

解法：增加一层**进程内 in-flight 登记**，与 DB 派生规则叠加：

```python
class PaymentCardRegistry:
    """支付卡的运行时排他。补 DB 派生规则的时间差窗口。"""
    def try_acquire(self, card_number, email) -> bool:
        # 已被别的 worker 正在使用 → 拒绝
    def release(self, card_number): ...
```

接入点：`_get_card()`（`registration.py:487`）选中候选卡后、实际发起支付前 `try_acquire`；`_on_invoice_paid` 记账完成后 `release`；异常路径在 finally 释放。

`registration.py` 需新增可选参数 `payment_registry=None`，为 `None` 时退化为当前行为（保证 `max_workers=1` 与串行入口不受影响，R1.2）。

## 3. 状态隔离

### 3.1 AppState 拆分

`AppState` 保留为全局单例（`app.config['APP_STATE']` 不变，避免大范围改动），新增 worker 维度：

```python
class WorkerState:
    """单个 worker 的隔离状态。"""
    def __init__(self, worker_id):
        self.worker_id = worker_id           # 'W1' / 'W2' ...
        self.current_action = "空闲"
        self.logs = collections.deque(maxlen=500)
        self.log_seq = 0                     # 单调递增，供前端增量拉取
        self.last_frame = None
        self._active_driver = None
        self._screenshot_thread = None
        self._screenshot_stop = threading.Event()
        self.lock = threading.Lock()
        self.frame_lock = threading.Lock()

class AppState:
    # 保留：is_running / stop_requested / success_count / fail_count / logs（聚合）/ open_browsers
    # 新增：
    self.workers = {}                        # worker_id -> WorkerState
    self.account_registry = AccountRegistry(self)
    self.payment_registry = PaymentCardRegistry()
```

`_start_screenshot_loop` / `_stop_screenshot_loop` / `set_active_driver` / `clear_active_driver` / `_monitor` 全部**从 AppState 下沉到 WorkerState**。`_monitor` 作为回调传给 `registration.*` 的签名不变（仍是 `callable(driver, step)`），改为绑定到 WorkerState 实例的方法 —— 对下游零改动。

### 3.2 日志路由：contextvars

现状 `_patch_prints`（`app.py:781`）把各模块的 `print` 指向 `self._hooked_print`，全局单点。

改为 dispatcher + contextvar：

```python
_current_worker = contextvars.ContextVar('current_worker', default=None)

def _dispatch_print(self, *args, **kwargs):
    msg = kwargs.get('sep', ' ').join(map(str, args))
    w = _current_worker.get()
    if w is not None:
        w.add_log(msg)
        self.add_log(f"[{w.worker_id}] {msg}")   # 聚合流带标签
    else:
        self.add_log(msg)
    builtins.print(*args, **kwargs)
```

`_patch_prints` 仍只在流水线启动时调用一次（模块级赋值是全局的），但路由变成按调用线程动态决定。

**契约（必须写进代码注释）**：
- 新线程**不继承** contextvar，每个 worker 线程必须在 `run()` 开头 `_current_worker.set(self.state)`。
- worker 内部再起的子线程（如截图线程）同样不继承 —— 截图线程不打日志，无影响；未来新增子线程需显式 set。

### 3.3 停止语义

`force_stop`（`app.py:117`）的协作式停止模型**保持不变**（其注释已说明跨线程 quit 会导致 sync API 永久 hang）。改动仅是：置 `stop_requested` 后遍历所有 WorkerState 停各自截图线程、清各自 driver 引用。各 worker 在自己的 `_monitor` 检查点抛 `InterruptedError`，冒泡到各自 `finally` 里 `close_driver`。

## 4. WorkerPool

```python
class WorkerPool:
    def __init__(self, app_state, max_workers):
        self.max_workers = max(1, min(4, max_workers))   # R1.1 夹紧

    def map(self, items, fn) -> list:
        """把 items 分发给 worker 并发执行，全部完成后返回（barrier）。
        fn(worker_state, item) 在 worker 线程内执行。"""

    def run_until_empty(self, produce, fn):
        """无界模式：worker 循环调 produce() 取下一份工作，返回 None 则该 worker 退出。
        供阶段1b（注册新号，数量由卡池决定）使用。"""
```

`max_workers=1` 时 `map`/`run_until_empty` 走**同线程直接调用**分支，不起线程池 —— 让 R1.2/AC3 的等价性由结构保证而非靠测试碰运气。

### 各阶段接法

| 阶段 | 模式 | 并发单位 | 卡领取 |
|---|---|---|---|
| 1a 补绑 | `map(candidates)` | 每个候选账号 | 进入后 `claim_batch(max_bindable)` |
| 1b 注册 | `run_until_empty` | 每轮注册一个新号 | 先 `claim_batch`，领不到则退出 |
| 2 充值 | 逐轮 `map(round_targets)` | 每个目标账号 | 走 2.3 的 PaymentCardRegistry |

**阶段2 的轮末 barrier 是刻意的**（R6.1）：`map` 的同步语义天然保证一轮内每账号只被推进一次。代价是每轮末尾有等待慢账号的空转，但反封控语义优先于吞吐。

**连续失败阈值**（现为局部变量 `consecutive_failures`，`app.py:537`）在并发下须改为**共享计数器 + 锁**，语义从"某线程连续失败 N 次"变为"全局连续失败 N 次"。任一 worker 成功即清零。

## 5. 故障回收

`ReaperThread`：daemon 线程，每 60s 执行一次

```python
n = card_binding_model.reap_stale(cfg.concurrency.claim_timeout_minutes)
if n: app_state.add_log(f"[回收] {n} 条卡领取超时，已重置为 pending")
```

启动时（`create_app`，`app.py:811`）调用 `reset_all_processing()` 并记日志，满足 R3.2/AC5。

**已知取舍**：超时回收无法区分"worker 真死了"和"worker 只是很慢"。若一个账号处理超过 `claim_timeout_minutes` 仍在跑，其卡会被回收并可能被另一 worker 领走，造成重复绑定尝试。缓解：默认 20 分钟远大于单账号正常耗时（OQ1 待实跑校准）；且回收后原 worker 的 `mark_success` 仍按 binding_id 更新，不会写坏数据，最坏是一张卡被多绑一次。**如需彻底杜绝，需引入 worker 心跳续期 —— 本期不做，记为已知限制。**

## 6. 配置

`config.yaml` 新增：
```yaml
concurrency:
  max_workers: 2              # 1-4，越界夹紧
  claim_timeout_minutes: 20
```
`src/config.py` 补默认值，缺失时回落 `max_workers=1`（老配置文件零改动可用）。

## 7. API 契约

**向后兼容策略**：所有现有字段保留，新增字段旁挂。

`GET /api/status` 新增：
```json
{
  "is_running": true, "current_action": "...", "success": 10, "fail": 2,
  "logs": ["[W1] ...", "[W2] ..."],
  "workers": [
    {"id": "W1", "current_action": "补绑 a@x.com", "busy": true, "log_seq": 142},
    {"id": "W2", "current_action": "注册账号 3",   "busy": true, "log_seq": 97}
  ]
}
```
- 顶层 `current_action`：`max_workers=1` 时等于 W1 的；多 worker 时为聚合摘要（如 `"并发处理中（2/2 忙）"`）。
- 顶层 `logs` 保留为带 `[Wn]` 前缀的聚合流，老前端不改也能用。

新增 `GET /api/workers/<wid>/logs?index=N` → `{"logs": [...], "next_index": M}`

`GET /video_feed?worker=W1`：`worker` 缺省取第一个 worker，保证老 URL 可用。`gen_frames`（`app.py:802`）改为接 WorkerState。

## 8. 前端

`Workbench.vue`（242 行）改造：
- 监控区由 `v-for="w in appStore.workers"` 驱动，CSS Grid `grid-template-columns: repeat(N, 1fr)`，N 为 worker 数。
- 每栏：worker 标题 + `current_action` + `<img :src="'/video_feed?worker=' + w.id">` + 独立日志区。
- `max_workers=1` → N=1，布局退化为当前单栏（R5.3）。
- store 轮询改为按 worker 各自 `log_seq` 增量拉取。

`Dashboard.vue:54` 也引用 `/video_feed`，同步适配（默认展示 W1）。

## 9. 风险与已知限制

| 风险 | 缓解 |
|---|---|
| 超时回收误伤慢 worker（§5） | 默认 20 分钟；本期不做心跳续期，记为已知限制 |
| 同 IP 并发触发 CF 风控（OQ2） | 默认并发度 2；保留现有随机间隔；实跑观察 |
| 支付卡 in-flight 登记是内存态，进程崩溃即丢 | DB 派生规则仍是第二道闸；重启后自然恢复 |
| `processing` 态影响既有 pending 统计口径 | §2.2 已列出全部调用点，逐一处理并测试 |
| 阶段2 轮末 barrier 空转 | 反封控语义优先，接受 |
