# 技术设计

## 改动边界

- `src/models/card_payment_state.py` — `set_cooldown` 的到期时刻计算
- `src/services/registration.py` — 试卡循环里加一道实时复查
- `config.example.yaml` / `config.yaml` — `fail_cooldown_hours` 的注释改写
- `tests/` — 新增冷却边界与并发复查的用例

不碰 `_eligible_cards`、`PaymentCardRegistry`、判废逻辑、成功路径。

## 一、到期时刻：max(次日零点, now + N 小时)

现状：

```sql
tds_until = datetime('now','localtime','+24 hours')
```

改为在 SQL 里直接取两者较大值——字符串比较对 `YYYY-MM-DD HH:MM:SS` 是正确的
（定长、零填充、字典序等于时间序），不需要绕到 Python 侧算：

```sql
tds_until = MAX(
    datetime('now','localtime','start of day','+1 day'),   -- 次日 00:00
    datetime('now','localtime', ?)                         -- now + N hours
)
```

两个分支各自成立的场景：

| 失败时刻 | 次日零点 | now+24h | 取 max | 说明 |
|---|---|---|---|---|
| 15:00 | 次日 00:00 | 次日 15:00 | **次日 15:00** | ⚠️ 见下 |
| 23:59 | 次日 00:00 | 次日 23:59 | 次日 23:59 | 下限起作用 |

**注意**：`fail_cooldown_hours` 默认 24 时，`now+24h` 永远晚于次日零点，max 恒取它，
自然日规则等于没生效。**默认值同期改为 12**，两个分支才都有戏：

| 失败时刻 | 次日 00:00 | now+12h | 取 max |
|---|---|---|---|
| 09:00 | 次日 00:00 | 当日 21:00 | **次日 00:00** ← 自然日生效 |
| 12:00 | 次日 00:00 | 次日 00:00 | 次日 00:00 |
| 15:00 | 次日 00:00 | 次日 03:00 | 次日 03:00 ← 下限生效 |
| 23:59 | 次日 00:00 | 次日 11:59 | 次日 11:59 |

即：**中午前失败的卡次日零点回来，中午后失败的按 12h 滑动**。这个默认值的变更要写进
config 注释，否则改完看不出效果。

## 二、并发窗口：试卡循环里实时复查

`recharge_account` 入口处的一次性过滤保留（它是廉价的预筛），但不能只靠它：

```python
cards = [c for c in payment_cards if not card_state_model.in_cooldown(platform, num)]
```

这份快照在整个会话内不变，而会话可能持续很久。补一道实时复查，位置在
`try_acquire` 成功之后、`top_up` 之前：

```python
if payment_registry is not None and not payment_registry.try_acquire(platform, num, email):
    continue
try:
    # 入口的冷却过滤是**会话开始时**的快照，而一次会话可能跑很久。期间别的 worker
    # 完全可能把这张卡刷失败并设上冷却——快照里它还在，照刷就违反了「当日失败不再用」。
    # in_flight 挡不住（对方早已释放），_used 只覆盖同一轮。所以这里按 DB 实时再问一次。
    # 异常时放行：安全网不该比它保护的流程更脆弱（R5）。
    if card_state_model is not None:
        try:
            if card_state_model.in_cooldown(platform, num):
                continue          # 不计入 attempts，这不是一次尝试
        except Exception:
            pass
    attempts += 1
    ...
```

`continue` 放在 `attempts += 1` 之前：跳过冷却卡不该消耗「单账号最多试几张卡」的额度，
与上面 `try_acquire` 失败时 `continue` 的处理一致。

⚠️ `continue` 会跳过 `finally` 里的 `payment_registry.release(num)` 吗？不会——
`try_acquire` 与 `try:` 之间没有别的语句，`continue` 在 `try` 块内，`finally` 照常执行。
但**必须把复查写在 `try:` 之内**，写在 `try_acquire` 和 `try:` 之间会导致占用不被释放。

## 三、为什么不在 `try_acquire` 里拦

`PaymentCardRegistry.try_acquire` 的 docstring 明确写着它只做并发排他，不管「本轮是否
已被别的账号试过」——在那里硬拦会让卡池偏紧时其它 worker 一张都拿不到，被 registration
误判成「卡池已耗尽」而永久放弃账号（`test_release_lets_a_waiting_worker_proceed` 守着这条）。
冷却复查同理，放在获取之后、使用之前，不改变准入语义。

## 四、兼容性与回滚

- `set_cooldown` 的签名不变（仍收 `hours`），只是语义变成下限。调用方无需改动。
- 无 schema 变更；已有的 `tds_until` 值继续有效，按新规则重新计算只影响此后的失败。
- 回滚 = 还原两个文件的 diff + config 默认值。

## 五、风险

冷却变宽松（多数卡比原来更早回到可选集）会**提高烧卡速度**。当前卡池成功率本就低，
这个改动会让失败卡更快地回来再被拒一次。这是用户明确要的规则，但上线后应观察
`fail_streak` 达阈值判废的速度是否明显加快。
