# 执行计划：多平台并发运行

## 分阶段原则

**每个阶段结束都必须能单独验证并回滚**，且**单平台 opencode 流程始终可跑**（AC14）。
不允许出现「中间几个 commit 是坏的，最后一个才修好」的情况——
`app.py` 里跑通的 opencode 流程是这个项目最贵的资产。

基线：**304 passed**。

---

## Stage 0：先修既有缺陷（不涉及并发）

这些是调查中发现的、**当前单平台下就已经错或将错**的问题。
先单独修掉，好处是它们的回归测试在并发改造前就绿了，
后面出问题时能确定不是这几处引起的。

- [ ] **0.1** `routes.py:1053` / `:1106` 的平台错读：
  `_eligible_cards(group_id)` 没传 platform，回落 `self.platform`。
  单平台下「碰巧对」，实际算的是另一个平台的卡。
  一并把 `exclude_used` 改成 `False`（计数调用点的既有规则）。
- [ ] **0.2** 删死字段 `current_card_task_id`（`app.py:90`，唯一读点 `routes.py:218`，
  永远是 None）。
- [ ] **0.3** 测试：平台错读的回归测试（传 A 平台、断言算的是 A 的卡数）。
- [ ] **0.4** 全量测试 + 单平台 opencode 实跑冒烟。

**提交点。** 此时行为应与改造前完全一致，除了修掉的两个 bug。

---

## Stage 1：AppState 一分为二（纯结构，不改语义）

- [ ] **1.1** 新增 `SharedResources`，把 A 类字段搬进去：
  `db`/`models`/`open_browsers`/三个 registry/`_adspower_client`/`_adspower_pool`/
  `_hotmail_map`。
- [ ] **1.2** `AppState` 更名为 `PlatformRunContext`，B 类字段留下，
  A 类字段改成 `@property` 委托给 `self.shared`。
  **关键：方法体不动**，`self.db` / `self.models` / `self.account_registry`
  等约 200 处调用应当一行不改。
- [ ] **1.3** `_adspower_lock` 一分为二：
  客户端惰性构造锁进 `shared`，`_adspower_started` 的保护留在 ctx。
- [ ] **1.4** `create_app` 建 1 个 shared + N 个 ctx（N = 已注册平台数）。
  `app.config['APP_STATE']` 保留指向默认平台 ctx。
  `app.config['RUN_CONTEXTS']` 新增。
- [ ] **1.5** 全量测试。**此时仍是单平台语义**——所有请求都落到默认 ctx，
  行为必须与 Stage 0 完全一致。
- [ ] **1.6** 单平台 opencode 实跑冒烟（AC14 的第一道关）。

**提交点。** 这一步的验收标准是「什么都没变」——纯结构重构。

**回滚点**：如果 property 委托方案导致 diff 失控（超过预期很多），
在这里停下来重新评估，不要硬推到 Stage 2。

---

## Stage 2：三个静默错误（★，并发正确性的前提）

必须在同一批里一起做，漏一个都会让并发「看起来能跑，实际是错的」。
**每一个都要有回归测试**——它们都不报错，靠实跑观察发现不了。

- [ ] **2.1** 三个 registry 引入 owner 概念（owner = platform slug）：
  `snapshot(owner=None)` / `release_all(owner=None)` / `is_claimed`。
  逐个调用点判断语义：
  - `_try_claim` 的「本轮还有没有在飞」→ **传** owner
  - 排他判定（「这 email/代理被占了吗」）→ **不传** owner，保持全局
- [ ] **2.2** 收尾的三连 `release_all()` 改成传 owner
  （`app.py:954-956`、`:1330-1332`）。
  `payment_registry.release_all()` 无参模式保留但限定进程退出时用，docstring 写死。
- [ ] **2.3** `worker_id` 改成 `f'{platform}:W{i}'`。
  影响面：`get_worker` 回落、`/video_feed?worker=`、`/api/workers/<id>/logs`、
  `card_binding_model.claim_batch` 归属键。
- [ ] **2.4** 测试：
  - owner 隔离——A 平台的 `snapshot(owner='A')` 看不见 B 的在飞账号
  - 收尾不误清——A 跑完后 B 持有的账号/卡/代理仍在
  - worker_id 唯一——两平台的 W1 不会被 `ProxyRegistry` 认成同一个持有者
    （这条直接钉住「同一代理发给两个平台」这个静默错误）
- [ ] **2.5** 全量测试 + 单平台实跑冒烟。

**提交点。**

---

## Stage 3：日志 dispatcher

- [ ] **3.1** 新增 `_RUN_CTX = contextvars.ContextVar('run_context')`。
- [ ] **3.2** `dispatch_print` 从绑定方法改成**模块级函数**，
  从 contextvar 解析归属；无归属时如实退化到 `builtins.print`，**不猜平台**。
- [ ] **3.3** `_patch_prints` 改成进程内**只装一次**（幂等）。
- [ ] **3.4** 逐个绑定点显式 set/reset（`contextvars` 不跨线程继承）：
  - 三条流水线的启动线程入口
  - `WorkerPool._run_in_worker`（已有范式，照抄）
  - `routes.py` 的 `_do_recharge` 线程
- [ ] **3.5** 测试：**每个绑定点一条**。漏绑的表现是「日志跑到另一个平台去了」，
  不报错，所以必须逐点覆盖。
  另加一条：`src.payments.stripe_checkout`（两平台共享模块）的 print
  能正确按当前 ctx 分流。
- [ ] **3.6** 全量测试 + 单平台实跑（看日志是否仍正常）。

**提交点。**

---

## Stage 4：配额仲裁器（唯一的真·新功能）

- [ ] **4.1** 新增 `AdsPowerQuota`：`TOTAL=11`、
  `RESERVED={'opencode':7,'infron':4}`，支持借用与协作式归还。
  **归还是协作式的**——发归还请求，对方在下一个账号边界不再申请新额度，
  绝不强制中断正在跑的账号（中断可能留下「钱扣了但状态不明」）。
- [ ] **4.2** 接入 `browser_factory()` → `_ensure_adspower()` 链路：
  在 `pool.ensure_profile(email)` **之前** `quota.acquire(platform)`。
  ⚠️ 仲裁器的锁必须在 `_adspower_pool._lock` **外层**，
  否则「持池锁等配额」会死锁。
- [ ] **4.3** 释放：环境被 `reclaim`/`release` 时 `quota.release(platform)`。
- [ ] **4.4** 重写 `app.py:926` / `:1301` 的 `AdsPowerError` 分支：
  从「置全局 stop」改成「走仲裁器等待路径 + 超时才判失败」（AC8）。
- [ ] **4.5** 测试：
  - 总占用任何时刻不超过 11（AC5）
  - 各平台默认不超 7/4（AC6）
  - 借用与归还（AC7）
  - 配额耗尽是等待不是崩溃（AC8）
  - **并发压力测试**：多线程同时 acquire，断言不超限、不死锁
- [ ] **4.6** 全量测试。

**提交点。**

---

## Stage 5：API 按平台寻址

- [ ] **5.1** `get_app_state()` → `get_run_context(platform)`。
- [ ] **5.2** `_req_platform()` 的兜底从「隐式猜 `self.platform`」
  改成必须显式或明确 400。
- [ ] **5.3** 五处 `is_running` 闸门改按平台判断
  （`routes.py:121, 144, 393, 1027, 1084`）——**这是 AC1 的解锁点**。
  注意 Stage 保留同平台内部的互斥语义（`/api/start` 与
  `/api/accounts/recharge` 共用主 worker W1）。
- [ ] **5.4** `/api/status` 不带 platform 时返回**全平台汇总**（不猜）。
- [ ] **5.5** `/video_feed`、`/api/workers/<id>/logs` 加平台维度。
- [ ] **5.6** 测试：两平台可同时 `is_running`（AC1）、
  停一个不影响另一个（AC2）、状态不串（AC3）。
- [ ] **5.7** 全量测试。

**提交点。**

---

## Stage 6：前端

- [ ] **6.1** store 按平台分路：运行状态字段改成按平台键控。
- [ ] **6.2** `App.vue:45` 去掉平台下拉的 `:disabled="store.isRunning"`（AC11）。
- [ ] **6.3** 切换平台后日志/进度/计数显示该平台的（AC12）。
- [ ] **6.4** 另一平台运行中的可见提示（AC13）——
  否则它出问题时用户看不见。
- [ ] **6.5** `Workbench.vue:126` 写死的 `/video_feed` 加平台参数。
- [ ] **6.6** 前端构建 + 手动验证。

**提交点。**

---

## Stage 7：端到端验证与收尾

- [ ] **7.1** **两平台真实并发实跑**：opencode 与 infron 同时启动，
  观察日志不串台、配额不超限、卡不冲突。
- [ ] **7.2** 停一个平台，确认另一个继续跑（AC2）。
- [ ] **7.3** 制造一个平台的异常，确认另一个不受影响（AC4）。
- [ ] **7.4** 单平台回归实跑（AC14，最后一道关）。
- [ ] **7.5** 全量 AC 走查，如实记录未达成项。
- [ ] **7.6** spec 更新：`multi-platform-guidelines.md` 的
  「Execution model: one platform at a time」一节需要整体改写；
  `concurrency-guidelines.md` 补 owner 语义与配额仲裁器。

---

## 提交划分

| commit | 内容 |
|---|---|
| 0 | 修既有缺陷（平台错读、死字段） |
| 1 | AppState 一分为二（纯结构） |
| 2 | 三个静默错误（owner / 收尾 / worker_id） |
| 3 | 日志 dispatcher |
| 4 | 配额仲裁器 |
| 5 | API 按平台寻址（AC1 在这里解锁） |
| 6 | 前端 |
| 7 | 端到端验证与 spec |

## 风险与回滚点

**最大回滚点在 Stage 1 末尾**：如果 property 委托方案的 diff 失控，
在那里停下重新评估，不要硬推。

**最危险的失败模式不是崩溃，是静默错误**。Stage 2 的三个陷阱都不报错：
- owner 不隔离 → 任务「跑着跑着不动了」
- 收尾误清 → 另一平台的排他保护蒸发
- worker_id 撞名 → 同一代理发给两个平台，反关联白做

所以 Stage 2 的测试**不是可选项**，实跑观察发现不了这些。

**AC14 是安全底线**：每个阶段都要跑单平台 opencode 冒烟，
不能等到 Stage 7 才发现改坏了。

## 留意但不在本任务范围

- captcha 求解器是进程级全局（`services/captcha.py:15-17`），
  两平台用不同 key/server 时后启动的会静默改掉先启动的。
  本任务只做「幂等 + 冲突告警」，按平台多实例要改求解器接口，另开任务。
- 三个及以上平台并发：架构上应自然支持，但不作为本任务验收目标。
- **同一 email 不能同时在两平台跑**是物理约束（Chrome profile 与 AdsPower 环境
  都按 email），不是可以优化掉的东西。「并发」实际是「各跑不同账号」。

---

## Stage 7 走查结果（2026-08-04）

### 真实双平台并发实跑

同时对 infron（briced35）与 opencode（nathalys640）发起单账号充值：

```
两个请求都返回 started          ← 改造前第二个会拿到 400
platforms: infron running=True, opencode running=True
quota: {infron: 1, opencode: 1}  总 2/11，各自在 4 / 7 额度内
```

**日志零串台**：opencode 的日志里搜 `infron` / `Diners` / `briced35` 命中 0 行；
且它走的是「检查 opencode 登录态 → 尝试登录 GitHub」，infron 走的是
「复用环境登录态 → 打开 credits 页」——两条完全不同的链路各自归位。

**AC2 验证**：并发中停 opencode 返回 `{"platform":"opencode","status":"stopping"}`，
infron 仍 `running=True` 并继续跑到填卡。

**AC4 顺带验证**：opencode 因登录失败先行结束时，infron 毫发无损继续——
这正是 ★2（收尾无差别全清）修复的生产验证。

**配额闭环**：两平台全部结束后 `total_held = 0`，无泄漏。

### 逐条 AC

| AC | 结论 | 依据 |
|---|---|---|
| AC1 两平台可同时运行 | ✅ | 实跑两个请求都 started；单测 + 缺陷注入 |
| AC2 停一个不影响另一个 | ✅ | 实跑验证 |
| AC3 状态不串 | ✅ | 实跑日志零串台；单测覆盖计数/日志/worker 日志 |
| AC4 一个崩了不影响另一个 | ✅ | opencode 提前结束，infron 无影响 |
| AC5 总占用 ≤ 11 | ✅ | 22 线程并发压力测试断言峰值；实跑 2/11 |
| AC6 各平台默认不超 7/4 | ✅ | 单测 |
| AC7 借用与归还 | ⚠️ 仅单测 | 生产未触发（从没同时跑到超过自有额度） |
| AC8 配额耗尽是等待非崩溃 | ⚠️ 仅单测 | 同上，生产未撞过配额上限 |
| AC9 同卡不会两平台同时使用 | ✅ | `_in_flight` 全局语义保留 + 单测 |
| AC10 卡状态仍按平台隔离 | ✅ | 上个任务已验证，本任务未触碰数据层 |
| AC11 切换器不再禁用 | ✅ | 去掉 `:disabled`，前端构建通过 |
| AC12 切平台后显示该平台数据 | ✅ | `switchPlatform` 重置日志/计数并立即 poll |
| AC13 另一平台运行中有提示 | ✅ | 可点击的「XX 运行中」按钮 + 选项 ● 标记 |
| AC14 单平台行为不变 | ✅ | 每阶段都跑单平台冒烟；388 passed |
| AC15 测试全绿 | ✅ | 304 → 388 passed |

### 与设计文档的偏差

**没有把 `worker_id` 改成 `platform:W1`**（design 原本这么写）。Stage 1 拆分后
`workers` 字典已按平台各自持有，`get_worker('W1')` 本就取自己那个；
`claim_batch` 也已被 `task_id` 隔离（一个 task 只属于一个平台）。
真正会碰撞的只剩共享的 `ProxyRegistry`，给它加 owner 即可——比改 66 处引用
代价小得多，且风险更低。

### 意外收获：三个既有缺陷

调查与实现过程中发现的、**当前单平台下就已经错**的问题，已在 Stage 0 单独修掉：

1. 两个流水线启动门的可选卡数算的是**上一次运行那个平台**的卡
2. `current_card_task_id` 从未被赋值 → 清理接口的保护形同虚设，
   任务运行中点一下清理会删掉正在跑的任务的绑卡记录
3. （Stage 5）清理接口只保护一个任务，并发时会删掉另一个平台的

### 待确认

**「opencode 7 个环境」的含义未确认。** 若指并发浏览器数，需要把
`clamp_workers` 的上限从 4 提到 7 并评估内存（7 个有头 Chrome 约 3–5 GB，
加 infron 的 4 个共 11 个）。当前 `max_workers: 1`，未擅自改动。
若指的是配额分配比例，则现状已正确。

### 验证手法

本任务的缺陷绝大多数**不报错**，所以每个修复都做了**缺陷注入**——
把改动还原成旧写法，确认对应测试变红：

- Stage 2：4 个注入（snapshot 漏 owner / 收尾全清 / 领代理漏 owner / 只比裸 worker_id）
- Stage 3：3 个注入（worker 漏绑 / 充值端点绑在请求线程 / 无归属静默丢弃）
- Stage 4：4 个注入（close_driver 不回调 / 配额耗尽置全局 stop / 借用不受归还约束 / release 无下界）
- Stage 5：5 个注入（启动闸门 / 停止 / status / worker 日志退回单例 / 清理只保护当前平台）

共 16 个，全部验证有效。

### 并发度确认（用户答复）

「并发 2 个就行」——即**每个平台 2 个账号**，两个平台合计 4 个浏览器。

`max_workers` 本来就是 2，无需改代码。但注释错了：它写的是「同时驱动的浏览器数」，
而这个值现在是**每平台**的，两个平台并发时要乘以平台数。已更正 config.yaml 与
config.example.yaml。

配额保持 7/4（用户选择）。配额是安全帽不是目标，留寬余量便于以后调高并发度而
不必重配。代价是借用与归还（AC7/AC8）在生产里几乎不会被触发，永远只有单测覆盖——
这也是那两条 AC 留空的原因。

新增两条测试：
- `test_each_platform_gets_its_own_worker_pool` —— 池必须用本平台的 ctx 构造。
  挪到 SharedResources 会让两个平台抢同一批 worker，那不是并发，是把并发度砍半。
- `test_total_browsers_stay_within_the_quota` —— 配置层面的清醒检查。
  现在 2 × 2 = 4 远低于 11；哪天有人把 max_workers 拉到 4 又接第三个平台
  （4 × 3 = 12 > 11），这条会先红，而不是等生产上撞配额。
