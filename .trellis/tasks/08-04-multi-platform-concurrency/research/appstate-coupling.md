# AppState 耦合面调查（2026-08-04）

调查范围：`src/web/app.py`(1466 行)、`src/web/worker.py`(591)、`src/api/routes.py`(1177)、
`src/models/adspower_profile.py`、`src/browser/adspower_driver.py`、`frontend/src/stores/app.js`。

## 一句话结论

**数据层不用动**（上个任务已按平台隔离并在生产验证），改动集中在运行时状态。
但有 **10 个隐藏耦合陷阱**，其中 3 个会造成**静默错误**而不是报错——那才是真正的风险。

## AppState 属性三分类

### A 类：必须保持全局单例

`db`(app.py:72)、`models`(:73)、`open_browsers`(:93)、`account_registry`(:107)、
`payment_registry`(:108)、`proxy_registry`(:109)、
`_adspower_client/_adspower_pool/_adspower_lock`(:113-115)、`_hotmail_map`。

理由各不相同，不能一概而论：
- registry 三兄弟——Chrome profile 目录按 email 命名、代理出口 IP 是物理资源
- AdsPower 客户端/池——见陷阱 4，拆开会活锁

### B 类：必须按平台拆

`platform`(:79)、`is_running`(:81)、`stop_requested`(:82)、
`success_count/fail_count`(:83-84)、`current_action`(:85)、
`logs`+`lock`(:86-87)、`parallel_mode`(:104)、
`workers/active_worker_count/_workers_lock`(:98-100)、`_adspower_started`(:118)。

### C 类：需要判断

- `current_card_task_id`(:90) —— **死字段**。全仓库唯一写点是 `__init__`，
  唯一读点是 `routes.py:218`，永远是 `None`。可直接删，零风险。
- `_adspower_lock`(:115) —— 同时保护「客户端惰性构造」（该共享）与
  `_adspower_started`（该按平台）。**必须一分为二**。

## 阻塞 AC1 的直接位置

五处 `if state.is_running: return 400`：
`routes.py:121`(/api/start)、`:144`(/api/stop)、`:393`(单账号充值)、
`:1027`(每日充值)、`:1084`(每日订阅)。

---

## 陷阱清单（按危险程度排序）

### ★1. `AccountRegistry.snapshot()` 不区分 owner —— 轮转永不收敛

`run_daily_pipeline._try_claim`(app.py:817-818) 用
`if self.account_registry.snapshot(): return 'wait', None`
判断「本轮还有账号在飞」。registry 必须全局共享，于是
**A 平台会把 B 平台正在跑的账号看成自己这轮在飞**，
`'wait'` 分支每 5 秒空转，**轮边界(app.py:819-848)永远不触发**
→ 失败账号永不重试、`zero_rounds` 永不递增 → 任务不收敛。

最隐蔽的一个：没有任何报错，只是「跑着跑着就不动了」。

修法：`snapshot()`/`is_claimed` 加 owner 过滤，或改用「本 ctx 领取集合」判断。

### ★2. `release_all()` 三连在收尾处无条件全清

`app.py:954-956` 与 `app.py:1330-1332`：
```python
self.account_registry.release_all()   # 清掉另一平台正持有的账号
self.payment_registry.release_all()   # 无参 = 连 _in_flight 一起清(worker.py:186-189)
self.proxy_registry.release_all()     # 清掉另一平台正用的出口 IP
```
一个平台跑完，另一个平台的**所有排他保护瞬间蒸发**。
三个 registry 都需要「按 owner 释放」的新方法。

`multi-platform-guidelines.md` 已经点名过这个坑。

### ★3. `worker_id` 是裸 `'W1'`，两平台必撞

- `ProxyRegistry.try_acquire` 的持有者比对是 `holder != worker_id`(worker.py:219-222)
  → 两平台的 `W1` 互相认成自己，**同一个代理被同时发给两个平台**，
  反关联白做，且**不会有任何日志**。
- `card_binding_model.claim_batch(task_id, worker.worker_id, n)`(app.py:404-405,437-438)
  以 worker_id 字符串为 DB 归属键。
- `AppState.get_worker` 缺省回落主 worker(app.py:208-212)，
  前端拿错平台的 W1 日志不会 404。

修法：worker_id 改成 `f'{platform}:W{i}'`。前端 `syncWorkers`(stores/app.js:48-76)
本来就是按 id 匹配，逻辑无需改。

### 4. AdsPower 客户端/池不能跟着 ctx 拆

- `AdsPowerClient._throttle` 的限流状态是**实例级**(services/adspower.py:92-105)。
  两个实例 = 两倍请求速率，直接撞 AdsPower 接口频率限制。
- `AdsPowerProfilePool._lock`(adspower_driver.py:102) 串行化整条
  「挑代理→建环境→撞配额→回收→重试」。其 docstring(:84-88) 明确写了拆开会产生
  **A 刚删出的配额被 B 抢走**的活锁。
- `is_busy=self.account_registry.is_claimed`(app.py:145) 必须指向**共享**账号注册表，
  否则 reclaim 会删掉另一平台正在用的环境，那个 worker 的浏览器凭空消失。

### 5. captcha 求解器是进程级全局

`services/captcha.py:15-17` 的 `_solver/_api_key/_server` 是模块全局，
`init_solver`(:73-90) 在 `_subscribe_one_account`(app.py:1107-1108) 里按本次运行的
key/server 重设。两平台用不同 key 或不同 server（Multibot vs 2captcha）时
**后启动的会静默改掉先启动的**。

### 6. 同一个 email 不能同时在两个平台跑（硬约束）

Chrome profile 目录按 email(driver.py:641-642) + `_clear_singleton_locks` 无条件删锁；
AdsPower 环境按 email 且刻意不按平台拆(adspower_profile.py:10-18)。

所以「两平台并发」实际是「**两平台各跑不同账号**」。这不是缺陷，是必须写进设计的前提。

### 7. `routes.py:1053` / `:1106` 已存在的平台错读

```python
platform = _req_platform(required=True)          # :1040 / :1097
eligible = len(state._eligible_cards(group_id))  # :1053 / :1106  ← 没传 platform
```
`_eligible_cards` 在 `platform=None` 时回落 `self.platform`(app.py:541)。
单平台下「碰巧对」，**启动前的可选卡数校验其实算的是另一个平台的卡**。并发后必然出错。

顺带：这里用了默认 `exclude_used=True`，与 concurrency-guidelines
「计数调用点必须传 `exclude_used=False`」相违。

### 8. `stop_requested` 三个写点语义不同

`app.py:926` 与 `app.py:1301` 的 `AdsPowerError` 分支（配额耗尽 → 置全局 stop），
注释明说「环境配额是全局的，下一个账号必然撞同一堵墙」。

按平台拆之后这条**只会停自己**，而配额是两平台共用的资源 →
会出现「A 平台饿死在等待、B 平台反复抛错自杀」。
做了配额仲裁器后这段必须整体重写。

### 9. `is_running` 兼两职

同时是「UI 显示」和「任务互斥闸门」。
`/api/start` 与 `/api/accounts/recharge` 共用主 worker W1、
共用 `state._stop_screenshot_loop()`(routes.py:462)，
**同平台内部仍需一个互斥语义**。不要假设「拆成 per-platform 就自动安全」。

### 10. `parallel_mode` 由 `WorkerPool.__init__` 副作用写入

`worker.py:477` `app_state.parallel_mode = not self.is_serial`，
`set_action`(app.py:278-286) 与 `dispatch_print`(app.py:302) 都读它。
两个 pool 写同一个 AppState 会互相覆盖 →
串行平台的日志突然带上 `[W1]` 前缀、`current_action` 停止更新。

---

## 日志劫持：会双重串台

`_patch_prints`(app.py:1346-1376) **是全局替换 print**——不是替换 `builtins.print`，
而是给一批模块注入模块级 `print` 名字。被打补丁的模块包括
`src.payments.stripe_checkout`，而 opencode 与 infron 的 `module_names()` **都含它**。

两平台同时跑的串台有两层：

1. **模块级 print 被最后一次 `_patch_prints` 覆盖**。两个实例各自 patch，
   `registration.print` 只能指向其中一个的绑定方法 → 所有平台的 print 进同一个 `self.logs`。
2. **contextvar 只知道 worker，不知道平台**。`dispatch_print`(app.py:290-306) 里
   `self` 是绑定死的，即使 worker 分栏正确，聚合流也全落到一个实例。

修法：把 hook 换成**只装一次的模块级 dispatcher**，靠新增的
`contextvars.ContextVar('run_context')` 解析归属；
在**每个流水线启动线程的入口**显式绑定——`contextvars` 不跨线程继承
(worker.py:11-13,26-31)，`WorkerPool._run_in_worker`(worker.py:483-500)
已有 token set/reset 范式可照抄。请求线程路径(routes.py:447 + :451-465 的 `_do_recharge`)
同样要绑。

## 截图：帧不是单例，但寻址是

真正的帧缓冲在 `WorkerState`(worker.py:264-265,303-309,323-360)，天然按 worker 隔离。
AppState 上的同名方法只是「委托给主 worker W1」的兼容壳(app.py:240-257)。

问题在寻址：`/video_feed?worker=W2`(app.py:1459-1464) 用裸 worker id 且缺省回落 W1。
需要加平台维度。前端 `Workbench.vue:126` 干脆写死 `/video_feed`。

## PaymentCardRegistry 的键（与 PRD 的卡互斥要求对应）

| 结构 | 键 | 平台语义 |
|---|---|---|
| `_in_flight`(worker.py:132) | **裸 card_number** | **全局，不按平台隔离**。理由是发卡行 velocity 风控 |
| `_used`(worker.py:143) | `(platform, card_number)` | 按平台隔离，纯选卡去重启发式 |

`release_all` 两种模式(worker.py:183-192)：
- `release_all(platform)` —— 轮边界，只删该平台 `_used`，`_in_flight` 不动。调用点 app.py:842
- `release_all()` —— 任务收尾，两个全清。调用点 app.py:955、app.py:1331 ← 陷阱 2

**PRD 要的「卡全局互斥」现状已满足**，本任务不需要改 `_in_flight` 语义，
只需要修陷阱 2 那个无差别全清。

## AdsPower 配额：代码里根本没有上限常量

12 只存在于注释与文档(adspower_profile.py:6-7,25-26；adspower_driver.py:8-9；
services/adspower.py:17；config.yaml:49)。

唯一可配的量是 `reclaim_batch`(默认 3，adspower_driver.py:47；config.py:142)。

配额是**被动发现**的：`client.create_profile` 返回 `code=-1` 且 msg 匹配
→ `AdsPowerQuotaExceeded`(services/adspower.py:70-87,165-168)。

**PRD 要的 7/4/11 软配额与「等待而非报错」(AC5-AC8) 目前完全不存在，是唯一的真·新功能。**

分配链：
`browser_factory()`(app.py:149-169) → `_ensure_adspower()`(:126-147)
→ `create_driver_adspower`(adspower_driver.py:276-348)
→ `pool.ensure_profile`(:139-149) → `_create_profile`(:151-175)
→ 撞配额则 `reclaim(exclude={email})` 后重试一次，仍失败抛 `AdsPowerQuotaExceeded`。

回收链：
`pool.reclaim(exclude, limit)`(adspower_driver.py:177-216)
→ `AdsPowerProfileModel.reclaim_candidates(limit)`(models/adspower_profile.py:93-159)
→ 过滤 `exclude` 与 `is_busy(email)` → `_stop_all` → `client.delete_profiles`。

`reclaim_candidates` **已经是平台感知的**(`NOT EXISTS` + `EXISTS` 双条件，:141-149)。

## 前端

运行状态字段：`isRunning:6`、`currentAction:7`、`successCount:8`、`failCount:9`、
`logs:11`+`logIndex:12`、`workers:16`、`parallelMode:17`。

平台字段：`platform:23`(localStorage 持久化)，`switchPlatform:39-44` 注入到
`api/index.js:6-10` 的模块级 `currentPlatform`，所有请求自动带 `platform` 参数。

轮询：`poll()`(:97-120) 每 1000ms 打 `/api/status`，全量覆盖运行状态字段。

要改的 UI 耦合点：`App.vue:45` 平台下拉 `:disabled="store.isRunning"`（AC11）、
`App.vue:54-55` 全局运行指示灯、`Workbench.vue`、`Dashboard.vue:61-64`、
`SidebarControls.vue:37`。

## 推荐的最小改动结构

**把 AppState 一分为二，方法留在原处、只换 `self` 指向的对象**：

- 新增 `SharedResources`：A 类字段全放这里
- 现 `AppState` 降级为 `PlatformRunContext`，每平台一个实例，持 B 类字段，
  并把 A 类字段做成 `@property` 委托给 `shared`

这样 `app.py` 里约 200 处 `self.db` / `self.models` / `self.account_registry`
/ `self.payment_registry` **一行都不用改**——这是把 diff 压到最小的关键。

`create_app`(app.py:1389-1466) 改成建 1 个 shared + N 个 ctx，
保留 `app.config['APP_STATE']` 指向默认平台 ctx，让老测试
(tests/test_reaper.py:83、tests/test_api_workers.py:17)少改。
