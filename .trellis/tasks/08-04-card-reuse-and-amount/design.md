# 技术设计 — 卡片复用策略与充值金额可配

## 设计总览

四条需求落在三个层次上，边界刻意划清：

| 层 | 承担什么 | 不承担什么 |
|----|---------|-----------|
| **模型层** `card_payment_state` | 「这张卡在这个平台连续失败几次了」的持久化真值 | 不知道阈值是多少、什么时候该判废 |
| **策略对象** `RechargeConfig` | 金额区间、余额上限、失败阈值、冷却时长这四个**可调参数** | 不知道它们怎么被用 |
| **编排层** `registration.recharge_account` | 判定与分派：什么时候冷却、什么时候判废、什么时候停止循环 | 不知道参数从哪来（UI 还是 yaml） |

平台适配器（`opencode` / `infron`）**一行都不改**——`top_up(…, amount=…)` 早就是接口的一部分，
只是编排层此前从没传过。

---

## 1. 数据模型：连续失败计数（R1）

### 为什么新增列，而不是从 `recharge_logs` 派生

派生看起来更省事（`recharge_logs` 已经逐卡记了成功/失败），但口径对不上：
`unknown` 结果也会写一条 `status='failed'` 的日志（见 `registration.py:298`），而 `unknown`
按硬约束是**不消耗卡**的。派生就必须在 SQL 里区分「哪种 failed 算数」，而那个信息只存在
`api_response` 的 JSON 里。把它变成一个显式计数列，判定逻辑留在编排层，SQL 保持诚实。

### Schema V17

```sql
ALTER TABLE card_payment_state ADD COLUMN fail_streak INTEGER DEFAULT 0;
ALTER TABLE card_payment_state ADD COLUMN last_fail_at TEXT;
```

挂在 `card_payment_state` 而不是 `card_pool`：主键已经是 `(card_number, platform)`，
正是 R1 要求的隔离粒度；且这张表的语义本就是「这张卡在这个平台的支付侧状态」。
`card_pool.status` 只装平台无关的 `expired`，不能放。

走既有迁移机制：`_MIGRATIONS[17] = _SCHEMA_V17`，`_apply_migration` 对 `ADD COLUMN`
已有幂等跳过。

### `CardPaymentStateModel` 新增方法

```python
def bump_fail_streak(self, platform, card_number) -> int   # 计数 +1，返回新值
def reset_fail_streak(self, platform, card_number)         # 归零（成功时调）
def get_fail_streak(self, platform, card_number) -> int
```

`bump_fail_streak` 必须是**一次原子的读-改-写**。`Database.execute` 每次调用各自持锁并
commit，两次调用之间另一个 worker 可以插进来，于是两次并发失败只让计数 +1。用
`self.db.transaction()` 把 upsert 与回读合成一个事务（注意事务块内只能用 yield 出来的
`conn`，不能调 `self.db.execute`——`_lock` 不可重入，会死锁，这条在 `database.py:524`
写着）。

```sql
INSERT INTO card_payment_state (card_number, platform, fail_streak, last_fail_at, updated_at)
VALUES (?, ?, 1, datetime('now','localtime'), datetime('now','localtime'))
ON CONFLICT(card_number, platform) DO UPDATE SET
  fail_streak  = COALESCE(card_payment_state.fail_streak, 0) + 1,
  last_fail_at = excluded.last_fail_at,
  updated_at   = excluded.updated_at
```

既有的 `set_cooldown` 的 `DO UPDATE SET` 只列了 `tds_until / tds_reason / updated_at`，
不会误清 `fail_streak`；它的 INSERT 分支不写 `fail_streak`，列默认值 0 生效。两边互不干扰，
不需要改 `set_cooldown`。

`get_state_map` 顺带带出 `fail_streak`，供前端后续展示（本期不做 UI，但接口先备好，
成本为零）。

---

## 2. 策略对象：`RechargeConfig`（R1/R2/R3/R4 的参数）

放在 `src/config.py`，与既有的 `ConcurrencyConfig` / `AdsPowerConfig` 同构：

```python
@dataclass
class RechargeConfig:
    amount_min: int = 20
    amount_max: int = 100
    balance_cap: float = 200.0      # R3：单账号余额上限，达到即换账号
    max_fail_streak: int = 3        # R1
    fail_cooldown_hours: int = 24   # R2

    def pick_amount(self) -> int    # random.randint(min, max)，已夹紧
    def with_overrides(self, **kw) -> 'RechargeConfig'   # UI 覆盖，非法值忽略
```

`config.yaml` 新增 `recharge:` 段，`_parse_config` 增一个分支。`cfg.recharge` 是缺省值，
UI 传来的值经 `with_overrides` 产出一个**新实例**——不改全局单例，避免两个平台并发时
互相覆盖参数（这正是多平台改造一路在消除的竞态源）。

### 为什么 `balance_cap` 必须是新配置项，不能复用 `recharge_skip_balance`

`recharge_skip_balance` 两平台都是 20，语义是**登录后的归档预检阈值**。若拿它当 R3 的
循环上限，第一笔充 20–100 后余额必然 ≥20，循环立刻停——R3 直接失效。两者语义不同，
数值也不该相同。

### `pick_amount` 的取值粒度

按**每笔**取，不是按账号取。需求原文是「每次充值金额保持在 20-100 之间随机」，
每笔独立随机也更像真人。取整数美元：infron 的 `select_amount` 对整数会先试预设档位
再回退自定义输入框，opencode 的 `press_sequentially(str(int(amount)))` 本来就只认整数。

---

## 3. 编排层重构：`recharge_account` 的主循环（R1/R2/R3/R4）

现有循环是「试卡 → 成功即 `return`」。改成「试卡 → 累计 → 按停止条件跳出 → 单一出口」。

### 循环状态

```python
paid_count    = 0      # 本次会话成功笔数
session_topped= 0.0    # 本次会话累计充值额（balance_after 读不到时的兜底判据）
stop_note     = ''     # 跳出原因，写进返回的 err/日志
```

### 成功分支（R2 + R3 + R4）

```
mark_status_by_number(paid) ─┐
valid_card.record            ├─ 保持不变
platform_account.update_*    ┘
card_state.reset_fail_streak(platform, num)   ← 新增（R1：成功清零）
_log_card_attempt(card, True, '', result, amount)   ← amount 改为实际值（R4）
paid_count += 1 ; session_topped += amount
若余额达上限 → stop_note = '余额已达上限' ; break
否则 continue                                  ← 关键改动（R3：不再 return）
```

**不进冷却**——这是 R2 已确认的口径，也是 R3 能在同一账号内连充的前提。

余额达标判据（两条，**取或**，先判余额后判累计额）：
- `balance_after is not None and balance_after >= balance_cap`
- `session_topped >= balance_cap`

第二条不是「`balance_after` 为 None 时才用」的窄兜底，而是无条件的第二道判据。
最初写成窄兜底，复核时发现那会给新适配器留一条**不成文的要求**：
`PaymentResult.balance_after` 是 `Optional[float]`，只要有个平台把 `success` 判成功
却回了个陈旧或偏低的余额，第一条判据就永远不成立，循环会一路刷到
`max_card_attempts`——单账号能吃掉 `8 × amount_max` = $800。加上无条件的第二条之后，
`balance_cap` 才是**硬**上限，不依赖任何适配器把余额读对。

副作用是符合意图的：余额低于上限但累计投入超了也会停，那说明账号在一边充一边烧
credits，余额永远追不上上限，而我们的投入是实打实的。

### 失败分支（R1 + R2）

`outcome == 'failed'`（明确拒付 / 3DS 挑战 / 3DS 失败）时：

```
card_state.set_cooldown(platform, num, hours=cfg.fail_cooldown_hours,
                        reason='充值失败，冷却')          ← 无条件（R2 扩大触发面）
streak = card_state.bump_fail_streak(platform, num)      ← 新增
if streak >= cfg.max_fail_streak:
    card_pool.mark_invalid_by_number(platform, num)      ← 达阈值才判废（R1）
_log_card_attempt(card, False, reason, result, amount)
```

原先「按 `prior_success` 分岔成冷却 or 判废」的判断**整段删掉**。取而代之的是：
冷却对所有失败一视同仁，判废只看计数。好卡的豁免不再靠这里的 `prior_success` 查询，
而是靠 `mark_invalid_by_number` 底层那道 `valid_cards` 守卫——它本来就是「所有标无效
入口的最终收口」，现在成了唯一收口，反而更干净。副作用：少了一次
`recharge_log.last_success_at` 查询。

`error` / `needs_captcha` / `unknown` 三个分支**完全不动**，硬约束原样保留。

### 停止条件与单一出口（R3）

`needs_captcha` 分支从「直接 `return (False, …)`」改成「设 `stop_note` 后 `break`」，
让它与其它跳出路径汇合到同一个出口。出口处：

```python
if paid_count:
    _grab_apikey(session, wid)          # 从成功分支移到这里：整次会话只抓一次
    return (True, stop_note, responses, last4, 'topup')
return (False, '所有支付卡均未成功：' + ' | '.join(errs), responses, last4, 'failed')
```

这条改动让 AC10 自然成立：`needs_captcha` 打断循环时，之前已成功的笔数照常算 `topup`，
而不是被一次风控拦截抹掉。

`_grab_apikey` 上移是顺带的正确性修复——原先每笔成功都调一次，R3 之后单账号可能成功
5 笔，就会白白多导航 4 次页面。

### 签名变化

`recharge_account(..., recharge_cfg=None)`，`None` 时回落 `cfg.recharge`。
返回契约 **5 元组不变**——`app.py` 和现有测试都按 5 个值解包，改成 6 个会波及一片。
上层要知道成功笔数就从 `responses` 里数 `ok=True` 的条目（`responses` 每笔都 append）。

---

## 4. 参数透传链（R4 + R3 的 balance_cap）

```
Workbench.vue (localStorage)
   └─ POST /api/daily/start  { amount_min, amount_max, balance_cap, … }
        └─ routes.start_daily_pipeline   ← 校验 + cfg.recharge.with_overrides()
             └─ AppState.run_daily_pipeline(…, recharge_cfg)
                  └─ AppState._recharge_one_account(…, recharge_cfg)
                       └─ registration.recharge_account(…, recharge_cfg)
                            └─ adapter.top_up(…, amount=recharge_cfg.pick_amount())
```

`/api/accounts/recharge`（单账号手动充值）走同一套解析，共用一个
`_recharge_cfg_from(data)` 辅助函数，避免两处校验逻辑漂移。

**校验**在 API 层做一次，模型层不重复：`amount_min/max` 必须是正整数且 `min <= max`；
越界（比如 min < 1 或 max > 1000）夹紧并在响应里带回实际生效值，让前端能提示。
非法输入返回 400 而不是静默夹紧——静默修正会让用户以为配的是 20-100，实际跑的是别的。

### 前端

`stores/settings.js` 加三个 ref（`amountMin` / `amountMax` / `balanceCap`），
沿用既有的 `localStorage` 持久化模式。`Workbench.vue` 侧栏加一行「充值金额」
（两个 number input，min–max）和一行「账号余额上限」，与现有 `.settings-row` 同构。
`handleStart` 组 body 时带上。

---

## 5. 日志与文案

- `app.py:845` `f"{email} AI Credits 充值 $20 成功"` → 改成按 `responses` 统计的
  「成功 N 笔、合计 $M」。
- `app.py:850-851` 的 `余额≥$20` → 用 `adapter.recharge_skip_balance` 的实际值。
- `registration.py` 里换卡提示补上本笔金额，排障时能一眼看出金额是否被正确传下去。

---

## 兼容性与回滚

- **DB**：只加列不改列，旧代码读不到新列也能跑；`user_version` 回退不会丢数据。
- **配置**：`recharge:` 段缺失时 `cfg.recharge` 全用默认值，行为等价于「20–100 随机、
  3 次判废、24h 冷却、余额上限 200」。要退回旧行为可配 `max_fail_streak: 1`
  + `amount_min: 20, amount_max: 20`；`balance_cap` 设很小的值（如 1）即可让 R3
  退化成「充一笔就换账号」。
- **前端**：新字段不传时后端用默认值，旧版前端 + 新版后端可正常工作。

## 风险

| 风险 | 说明 | 缓解 |
|------|------|------|
| 卡池收敛变慢 | R1+R2 让判废一张坏卡最快需 3 天 | 已在 PRD「Known Side Effects」记录，属预期 |
| 单轮卡池消耗加快 | R3 让一个账号吃掉多张卡 | `max_card_attempts` 仍是硬护栏 |
| `bump_fail_streak` 并发丢计数 | 两个 worker 同时失败同一张卡 | 事务内原子 upsert；且 `PaymentCardRegistry` 的 in-flight 排他本就不允许同卡并发 |
| 大额充值触发风控 | 单笔 $100 比 $20 更容易被审 | 金额区间可配，用户可自行收窄 |
