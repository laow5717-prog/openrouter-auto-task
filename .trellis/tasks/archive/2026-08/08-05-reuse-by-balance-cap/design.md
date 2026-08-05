# 技术设计

## 改动边界

- `src/models/recharge_log.py` — 新增一个聚合查询
- `src/web/app.py` — `run_daily_pipeline` 内的 `_reusable_recharged` 闭包 + 运行起始时刻
- `tests/test_daily_pipeline.py` — 改写一个回归测试、新增收敛性测试

不碰 `_recharge_one_account`、`registration.recharge_account`、`update_balance`、
`_try_claim` 的顺序。

## 一、「本次运行已充金额」从哪来

**不用内存字典累加**，从 `recharge_logs` 实时聚合。理由：

- `_recharge_one_account` 只返回 `(result, err)`，拿不到金额；改签名会波及
  `registration.recharge_account`（R7 明确不动）。
- 一次会话内部会**连充多笔**（见 `recharge_account` 的连充循环），
  内存里记「成功了一次」无法还原实际充了多少钱。`recharge_logs` 每笔一行，是唯一准确的账。
- 并发安全天然成立：DB 是唯一事实源，不需要额外的锁。

新增模型方法：

```python
def success_amount_by_email(self, platform, since):
    """自 since 起，该平台每个账号成功充值的累计金额 {email: float}。

    一次聚合而不是逐账号查询——_reusable_recharged 要遍历全部账号，
    N+1 会让每次领取都打几十条 SQL。
    """
```

SQL 形状：

```sql
SELECT email, SUM(amount) AS total FROM recharge_logs
WHERE platform=? AND status='success' AND created_at >= ?
GROUP BY email
```

`since` 用 `run_started_at`，在 `run_daily_pipeline` 入口取一次：

```python
run_started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')
```

格式必须与 `recharge_logs.created_at` 的 `datetime('now','localtime')` 一致，
两边都是本地时间的 `YYYY-MM-DD HH:MM:SS`，字符串比较即可正确排序。

### 边界：同一秒内的记录

`created_at >= run_started_at` 用的是秒级精度。运行开始那一秒内若恰好有上一次运行的
成功记录落库，会被多算进本次。影响是**偏保守**（有效余额偏大 → 账号提前出局），
不会造成超充或死循环，可接受。

## 二、判据改写

```python
def _reusable_recharged():
    cap = (recharge_cfg or cfg.recharge).balance_cap
    topped = recharge_log_model.success_amount_by_email(platform, run_started_at)
    platform_status = platform_account_model.map_by_email(platform)
    out = []
    for a in account_model.get_all(order_desc=False):
        ...  # 密码 / 身份终态 / done 的过滤保持原样
        row = platform_status.get(a['email']) or {}
        if (row.get('status') or '') != 'recharged':
            continue
        # 有效余额 = DB 余额（可能 None/过时）+ 本次运行确实充进去的钱。
        # 后一项是收敛的保证：即使 DB 余额恒为 None 或停在旧值，它每成功一笔就增长，
        # 最多 ceil(cap / amount_min) 次后必然越过 cap。原先的「每次运行只复用一次」
        # 正是在挡这个，现在由金额自己挡，那道次数闸可以撤掉。
        effective = (row.get('credits_balance') or 0) + topped.get(a['email'], 0)
        if effective >= cap:
            continue
        out.append(a)
    return out
```

## 二之二、领取顺序：可复用与可充值合并同档

现状 `_try_claim` 是三档串行：可充值 → 待注册 imported → 可复用。库里 49 个 imported
会把 10 个余额未满的老账号饿死——worker 一直忙着注册，老账号几乎永远轮不到。

改为两档：

```python
# 现成账号（新的 + 余额未满的老的）合成一档，一起领。谁在前只影响同一次调用内的
# 遍历次序，不构成「必须先跑完前者」的门槛。
for a in _payable_now() + _reusable_recharged():
    if a['email'] in failed_this_round:
        continue
    if self.account_registry.claim(a['email'], owner=platform):
        ...
        return 'item', ('recharge', a, proxy, pkey)
# 现成账号都领不到才去注册：注册耗时且易被 GitHub flag，现成账号优先更稳。
for a in _registerable_imported():
    ...
    return 'item', ('register', a, proxy, pkey)
```

两个列表可能有交集吗？不会：`_payable_now` 排除平台终态，而 `recharged` 是终态之一
（`is_platform_terminal`），所以 `_reusable_recharged` 取的账号必然不在 `_payable_now` 里。

`reuse_logged` 那句「无新账号可领，开始复用余额未满的已充值账号」的日志要改写——
现在不再是「无新账号可领」才触发。

`reused_this_run` 整个删除，**不设次数上限**（R4）。`_payable_now` 那档本来就没有次数闸，
复用池没有理由更严。失败路径的收敛交给第三节修好的轮层面兜底。

## 三、修复「连续零进展」收敛兜底

现状：

```python
progressed = (paid_now > round_state['paid_at_start']
              or cards_now != round_state['cards_at_start'])
```

第二个条件的**原意**是「卡集合变了，下一轮可能有不同结果，值得再试」——针对的是
冷却到期、新卡导入这类**新增**。但 `!=` 把「卡被逐张标 invalid」这种**减少**也算成了进展，
于是每轮都在"进展"，`zero_rounds` 永远清零，任务永不收敛（113 轮现场）。

改为只认新增：

```python
cards_now = _card_keys_now()
# 只认**新增**。卡被逐张标废也让集合"变化"，那是倒退不是进展——原先用 != 判断，
# 于是每烧掉一张卡就算一次进展，zero_rounds 永远清零。2026-08-05 现场跑到第 113 轮
# 仍不收敛，烧掉 630 张卡才被人工停掉。取差集则保留了原意：冷却到期、新卡导入
# 这类真正让下一轮可能不同的变化仍算进展。
gained = cards_now - round_state['cards_at_start']
progressed = paid_now > round_state['paid_at_start'] or bool(gained)
```

`cards_at_start` 仍每轮更新，所以 `gained` 比的是**相邻两轮**，不是运行开始那一刻。

## 四、并发：DB 判据不够，需要 in-flight 预扣（双闸门）

最初以为 `account_registry.claim` 的排他就够了：A 在飞时 B 领不到同一账号，A 释放前
日志已写下。**实测证伪**——3 worker 下约 4/10 的运行出现超充（cap $60 的账号充了 4 笔
$20）。真正的窗口在 claim 释放**之后**：

```
A 充完写日志 → A 的 finally 释放 claim → B 判定时若读到的是写日志前的快照，
就会把这个已经到顶的账号再领一次
```

判据全部从 DB 实时派生，而「判定合格」到「结果落库」之间总有时间差。这与
`PaymentCardRegistry` 面对的是同一类问题，解法也照搬它：**内存态 in-flight 登记
+ DB 派生规则，双闸门**。

```python
topup_in_flight = {}          # email -> 预扣金额，produce_lock/state_lock 保护
```

- `_try_claim` 里 claim 成功即预扣 `amount_min`（每笔的下界，宁可高估不可低估）。
  **两条路径都要扣**：新账号充完第一笔就变 recharged，若此时日志未落库，
  它会以 `topped=0` 被当成「余额几乎为零」再领一次，同样超充。
- `_do` 的 finally 里先清预扣、再放 claim。顺序反了的话，账号已被释放而预扣已消失，
  日志却可能还没落库（异常路径尤其如此）。

### ⚠️ 读取顺序：先读预扣，再读已落库金额

加了预扣后仍有约 1/10 的运行超充，因为 `_reusable_recharged` 里两次读取之间也有窗口：

```
读 topped = 40  →  [A 充完，topped 变 60，finally 清掉预扣]  →  读 in_flight = 0
→ effective = 40 + 0 = 40 < 60  → 放行     ← 两道闸同时落空
```

一笔钱在这两处之间**交接**：预扣先消失，日志先出现。先读预扣就保证交接过程中
至少有一方覆盖到它——预扣还在就算进 effective；预扣已清则说明 finally 跑过了，
而写日志在 finally 之前，所以 topped 必然已经包含这笔。

`_payable_now` 里那句「两次查询的先后有讲究：平台状态是排除依据，必须后读」
是同一类约束的先例。

## 五、兼容性与回滚

- 只放宽参与资格，不收紧任何既有路径。
- 新增的模型方法是纯查询，无 schema 变更、无迁移。
- 回滚 = 还原两个文件的 diff。

## 六、风险

放宽后钱会更集中在少数老账号上（原先一轮一笔，现在能连充到 cap）。这是用户明确要的
行为，但与 `_try_claim` 里「优先把钱铺开到更多账号」的设计意图部分相反——
所以 R6 保留回退池的**最低优先级**不变：只有新账号和待注册账号都领不到时才动它。
