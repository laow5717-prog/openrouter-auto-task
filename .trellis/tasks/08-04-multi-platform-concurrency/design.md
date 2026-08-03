# 技术设计：多平台并发运行

## 设计的出发点

调查（`research/appstate-coupling.md`）确认了一个关键事实：**数据层完全不用动**。
`platform_accounts`、`card_platform_state`、各 model 的平台隔离在上个任务已做完并在
生产验证过。改动被限制在**运行时状态**与 **UI**。

但同一份调查也找出 10 个隐藏耦合陷阱，其中三个（★标记）会造成**静默错误**——
不报错、不留日志，只是行为悄悄变错。它们才是本任务的真正难点，不是「拆状态」本身。

## 核心结构：AppState 一分为二

### 为什么这样拆

朴素做法是把所有状态塞进一个 `dict[platform]`，然后把 `app.py` 里几百处
`self.xxx` 改成 `self.ctx[platform].xxx`。那会产生一个上千行的 diff，
而 `app.py` 里跑通的 opencode 流程是这个项目最贵的资产（AC14 是安全底线）。

改用**双对象 + property 委托**：

```python
class SharedResources:
    """跨平台共享的单例资源。拆到这里的依据不是「看起来像全局」，
    而是每一项都有具体的物理理由——见下表。"""
    db, models, open_browsers
    account_registry, payment_registry, proxy_registry
    _adspower_client, _adspower_pool, _adspower_client_lock
    quota            # 新增：配额仲裁器
    _hotmail_map

class PlatformRunContext:
    """每个平台一个实例。持有「这一次运行」的全部状态。"""
    platform, is_running, stop_requested
    success_count, fail_count, current_action
    logs, log_lock, parallel_mode
    workers, active_worker_count, _workers_lock
    _adspower_started

    # A 类字段一律 property 委托给 shared
    @property
    def db(self): return self.shared.db
    @property
    def models(self): return self.shared.models
    # …account_registry / payment_registry / proxy_registry 同理
```

**收益**：`app.py` 里约 200 处 `self.db` / `self.models` / `self.account_registry`
**一行都不用改**。方法体几乎原样保留，`self` 从 AppState 变成 PlatformRunContext，
语义自然正确。

`create_app` 建 1 个 `shared` + N 个 ctx（N = 已注册平台数）。
`app.config['APP_STATE']` 保留，指向默认平台的 ctx——让既有测试
（`tests/test_reaper.py:83`、`tests/test_api_workers.py:17`）少改。

### 为什么这些必须共享（逐项理由）

| 资源 | 理由 |
|---|---|
| `db` / `models` | `Database` 自带锁 + `check_same_thread=False`，本身线程安全；model 方法已全部显式收 `platform` |
| `account_registry` | Chrome profile 目录**按 email 命名**，同一 email 不能同时被两个平台打开 |
| `payment_registry` | `_in_flight` 全局是硬要求——同卡在两处同时授权是盗刷特征 |
| `proxy_registry` | 出口 IP 是物理资源，反关联的全部意义就在于不重复 |
| `_adspower_client` | `_throttle` 限流状态是**实例级**，两个实例 = 两倍请求速率，撞接口频率限制 |
| `_adspower_pool` | `_lock` 串行化「挑代理→建环境→撞配额→回收→重试」；拆开会**活锁**（A 刚删出的配额被 B 抢走），其 docstring 已写明 |

顺手删掉 `current_card_task_id`——全仓库唯一写点是 `__init__`，永远是 `None`，是死字段。

## 三个静默错误的修法

这三个必须在同一批改动里一起解决，漏一个都会让并发「看起来能跑，实际是错的」。

### ★1 owner 感知的 registry

**问题**：`_try_claim` 用 `if account_registry.snapshot(): return 'wait'` 判断
「本轮还有账号在飞」。registry 必须全局，于是 A 平台把 B 平台在跑的账号
看成自己的 → `'wait'` 每 5 秒空转 → **轮边界永不触发** → 任务不收敛。
没有任何报错，只是「跑着跑着不动了」。

**修法**：三个 registry 全部引入 owner 概念，owner = platform slug。

```python
account_registry.snapshot(owner=None)     # None = 全部（保留给运维视图）
account_registry.snapshot(owner='infron') # 只看自己这一份
account_registry.release_all(owner='infron')
```

调用点按语义逐个判断，**不能全局替换**：
- `_try_claim` 的「本轮还有没有在飞」→ 传自己的 owner
- 排他判定（「这个 email/代理被占了吗」）→ **不传 owner**，保持全局

### ★2 收尾不再无差别全清

`app.py:954-956` / `:1330-1332` 的三连 `release_all()` 无参调用会把另一个平台
正在持有的账号、卡、代理全部释放掉——排他保护瞬间蒸发。

改成 `release_all(owner=self.platform)`。
`payment_registry.release_all()` 的无参模式（连 `_in_flight` 一起清）
**保留但只在进程退出时用**，并在 docstring 里写死这条。

### ★3 worker_id 全局唯一

裸 `'W1'` 会让 `ProxyRegistry.try_acquire` 的 `holder != worker_id` 比对失效——
两平台的 W1 互相认成自己，**同一个代理被同时发给两个平台**，反关联白做且无日志。

改成 `f'{platform}:W{i}'`（如 `opencode:W1`）。
影响面：`get_worker` 的回落、`/video_feed?worker=`、`/api/workers/<id>/logs`、
`card_binding_model.claim_batch` 的归属键。
前端 `syncWorkers` 本来就按 id 匹配，逻辑不用改。

## 日志：从「绑定方法」改成「模块级 dispatcher」

**现状会双重串台**：
1. `_patch_prints` 给模块注入模块级 `print` 名字，两个实例各 patch 一次，
   后者覆盖前者 → 所有平台的 print 进同一个 `logs`。
   `src.payments.stripe_checkout` 尤其致命——opencode 与 infron 的
   `module_names()` **都含它**。
2. `dispatch_print` 里的 `self` 是绑定死的，contextvar 只知道 worker 不知道平台。

**修法**：

```python
_RUN_CTX = contextvars.ContextVar('run_context', default=None)

def dispatch_print(*args, **kw):        # 模块级函数，不绑定任何实例
    ctx = _RUN_CTX.get()
    if ctx is None:
        builtins.print(*args, **kw)     # 无归属时如实退化，不猜平台
        return
    ...                                  # 原有的 worker 路由逻辑
```

`_patch_prints` 改成**进程内只装一次**（幂等），装的是这个模块级函数。

绑定点（`contextvars` **不跨线程继承**，每个入口都要显式绑）：
- 三条流水线的启动线程入口
- `WorkerPool._run_in_worker` —— 已有 token set/reset 范式，照抄
- `routes.py` 的 `_do_recharge` 线程

⚠️ 漏绑一处的表现是「那条链路的日志跑到另一个平台去了」，不会报错。
实现时每个绑定点都要有对应测试。

## 配额仲裁器（唯一的真·新功能）

调查发现：**代码里根本没有配额上限常量**，12 只存在于注释。配额是被动发现的——
`create_profile` 返回 `code=-1` 才抛 `AdsPowerQuotaExceeded`。

### 语义

```python
class AdsPowerQuota:
    """按平台的软配额，可借用、可抢占，总数硬上限。

    硬上限 11 = AdsPower 配额 12 − 它自带的 Default Profile。
    这个 1 不能省：实测配额会卡在 11/12。
    """
    TOTAL = 11
    RESERVED = {'opencode': 7, 'infron': 4}
```

**借用规则**（用户选的「软上限可借用」）：
- 平台自己的 `RESERVED` 额度内，随时可取
- 超出自己额度时，只要 `总占用 < TOTAL` 就可以借
- 借来的额度标记为 `borrowed`

**抢占（归还）规则**——这是最容易写出竞态的地方，故定死：

> **只做协作式归还，绝不强制中断正在跑的账号。**
>
> 原主需要额度而对方超额占用时，向对方发一个「归还请求」计数；
> 对方**在下一个账号边界**（当前账号跑完、释放环境时）不再申请新额度，
> 直到把借来的还清。原主在此期间**等待**（AC8：等待而非报错）。

理由：强制抢占意味着中断一个正在付款的浏览器会话——那可能是一笔已提交待授权的
支付，中断会留下不确定状态（这正是 `unknown` outcome 存在的原因）。
省下的几十秒不值得换一个「钱可能扣了但状态不明」的风险。

### 与既有代码的接合

- 获取额度：`browser_factory()` → `_ensure_adspower()` 链路上，
  在 `pool.ensure_profile(email)` **之前**先 `quota.acquire(platform)`
- 释放：环境被 `reclaim`/`release` 掉时 `quota.release(platform)`
- ⚠️ `_adspower_pool._lock` 已经串行化了整条建环境链路。
  配额仲裁器的锁**必须在它外层**，否则「持池锁等配额」会死锁

### 必须重写的一段

`app.py:926` / `:1301` 的 `AdsPowerError` 分支现在是「配额耗尽 → 置**全局**
stop_requested」，注释理由是「配额是全局的，下一个账号必然撞同一堵墙」。

按平台拆之后这条只会停自己，而配额仍是共用资源 →
**A 平台饿死在等待、B 平台反复抛错自杀**。

改成：配额耗尽 → 走仲裁器的等待路径，带超时；超时才判失败。

## API 按平台寻址

- `get_app_state()` → `get_run_context(platform)`，平台从现成的 `_req_platform()` 取
- `_req_platform()` 的兜底目前是 `get_app_state().platform`（隐式猜），
  改成**必须显式**或明确 400——并发下猜错就是写错平台
- 五处 `is_running` 闸门改成按平台判断
- `/api/status` 不带 platform 时**返回全平台汇总**（PRD 明确要求「不要猜」）
- `/video_feed`、`/api/workers/<id>/logs` 加平台维度

### 顺手修掉一个既有缺陷

`routes.py:1053` / `:1106`：
```python
platform = _req_platform(required=True)          # 取到了
eligible = len(state._eligible_cards(group_id))  # 却没传下去
```
`_eligible_cards` 在 `platform=None` 时回落 `self.platform`。单平台下「碰巧对」，
**启动前的可选卡数校验其实算的是另一个平台的卡**。并发后必然出错。

顺带这里用了默认 `exclude_used=True`，与 concurrency-guidelines
「计数调用点必须传 `exclude_used=False`」相违，一并改。

## 其余两处

**captcha 求解器是进程级全局**（`services/captcha.py:15-17`）。
两平台用不同 key/server 时后启动的会静默改掉先启动的。
本任务范围内的最小处理：`init_solver` 改成**幂等 + 冲突时告警**，
不做按平台多实例（那要改求解器接口，超出范围）。

**`parallel_mode` 由 `WorkerPool.__init__` 副作用写入**——随 ctx 下沉即可。

## 一条必须写进文档的前提

**同一个 email 不能同时在两个平台跑。**
Chrome profile 目录按 email，AdsPower 环境也按 email 且刻意不按平台拆。

所以「两平台并发」实际是「**两平台各跑不同账号**」。
这不是缺陷，是物理约束。`_try_claim` 遇到被另一平台占用的账号时，
应当**跳过继续找下一个**，而不是当前的隐式 `'wait'` 语义（与陷阱 1 同源）。

## 权衡与风险

**风控暴露面上升**：两平台同时抢 AdsPower 环境与代理 IP，
并发请求特征比串行明显。这是用户明确要的，如实记录。

**AC14 是安全底线**：opencode 流程是这个项目最贵的资产。
property 委托方案的全部意义就是让它的代码路径**尽可能一行不改**。
每个阶段结束都要跑一次单平台回归。

**最危险的失败模式不是崩溃，是静默错误**：三个 ★ 陷阱都不报错。
因此每一个都必须有对应的回归测试，而不是靠实跑观察。
