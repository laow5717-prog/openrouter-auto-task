# AdsPower 环境回收：按余额区分终态

## Goal

用户观察到「同一个账号对应的 AdsPower 浏览器经常更换」。设计上本该是一账号一环境
（`adspower_profiles` 表存 `email ↔ profile_id`，`ensure_profile` 命中映射就复用），
换 ID 的唯一原因是**映射被回收删掉了**。

根因是 2026-08-05 `reuse-by-balance-cap` 那次改动只改了一半。
[app.py:1064-1066](../../../src/web/app.py#L1064-L1066) 已经写下正确语义：

> 判据是「余额还没到 balance_cap」而不是「状态非 recharged」：`recharged` 现在的含义是
> 「有一些余额」，不再是「这个账号做完了」。真正做完的是 `archived`（余额已达上限），
> 它仍然是终态。

但 [adspower_profile.py:40](../../../src/models/adspower_profile.py#L40) 的
`_PLATFORM_TERMINAL` 没跟着改，仍把 `recharged` 当终态，于是回收流程把「下一轮还要用的
账号」的环境当垃圾删掉。

## 现场数据（2026-08-08）

| 指标 | 值 |
|---|---|
| 环境映射总数 | 9 |
| `recharged` 平台账号 | 14 |
| 其中还有环境映射 | 9 |
| **其中映射已被删** | **5** |

余额分布（`balance_cap` = $200）：$20 × 9、$49 / $55 / $78 / $108 / $110 各 1。
**无一到 $200，`archived` 一个都没有。**

那 5 个账号下次进轮转时必然新建环境 → 空 Cookie → GitHub 完整重登 → 因为是「新设备」
再触发一次邮箱验证码。今天 16:15 那轮日志里四个账号三个显示「新建」、紧接着一串
「等待 GitHub 验证码邮件」，就是这个代价。

## 关键约束：不能简单删掉 `recharged`

把 `recharged` 从 `_PLATFORM_TERMINAL` 拿掉，第 2 档回收候选会**直接清空**（archived 为 0），
只剩「孤儿映射」和「身份已死」可回收。配额 12 撞满时就报「配额已满，且没有可回收的环境」
——2026-08-03 踩过这个坑，当时整条流水线瘫痪。

**环境上限 12 < 活跃账号数**是物理事实，轮换消灭不掉。本任务要消灭的是「换得比必要的更
频繁」，以及「换掉的是错误的那个」。

## Requirements

### R1 分出真终态与「还要再跑」两档

- 第 2 档（真终态，优先回收）：所有平台行 ∈ `archived` / `subscribed`。
- 第 3 档（新增，最后手段）：所有平台行 ∈ `archived` / `subscribed` / `recharged`，
  且**至少一个是 `recharged`**。
- 只要第 0/1/2 档还有候选，**绝不动第 3 档**。

### R2 第 3 档一次只牺牲一个

- 第 0/1/2 档沿用 `reclaim_batch`（默认 3）批量回收。
- 第 3 档单次最多回收 **1 个**——这一档删的是活账号的登录态，每删一个就是一次
  「重登 + 新设备邮箱验证」的代价（数分钟 + 一封验证码）。批量删三个等于一次赔三份。

### R3 第 3 档内部按余额优先牺牲损失最小的

- 排序 `credits_balance DESC`：余额越高离 `balance_cap` 越近，剩下要充的笔数越少，
  重建环境的期望损失越小。
- `credits_balance` 为 NULL 的排**最后**（SQLite 的 `DESC` 天然如此，但要有测试锁住）。
  NULL 意味着余额读不到（`update_balance` 在 `balance_after` 读不到时直接 return，
  infron 常态、opencode 偶发），不知道就保守保留。
- 多平台账号取 `MAX(credits_balance)`：任一平台余额已高即视为整体接近完成。

### R4 `utils.PLATFORM_TERMINAL_STATUSES` 不动

它在 [app.py:1039](../../../src/web/app.py#L1039)、
[routes.py:1406](../../../src/api/routes.py#L1406) 等处决定「哪些账号还可充值」，是另一套
语义。`adspower_profile.py:38` 那句「与 utils.PLATFORM_TERMINAL_STATUSES 保持一致…两处
都要改」的注释**现在是错的**，必须改写成「两者语义已分家，不要同步」，否则下一个人会
照着注释把 bug 改回来。

### R5 日志说清牺牲了什么

回收第 3 档时，日志要写明这是「牺牲活账号登录态」而非常规回收，并带上余额，例如：

```
[AdsPower] 无真终态环境可回收，牺牲 1 个活账号环境腾配额: a@b.c(recharged, 余额 $110)
```

现在的日志只说「已回收 N 个环境释放配额」，看不出删的是活账号还是垃圾。

## Non-goals

- 不改环境配额上限（AdsPower 侧的硬限制）。
- 不改 `balance_cap` / `recharge_skip_balance` 的取值。
- 不改「余额未满的 recharged 账号进轮转」这个策略本身（08-05 已定）。
- 不做环境的「冷存储/导出再导入」之类的登录态迁移。

## Acceptance Criteria

- [ ] 有 `archived` 环境可回收时，`recharged` 的环境一个都不动。
- [ ] 只有 `recharged` 可选时才回收它，且**单次只回收 1 个**（哪怕
      `reclaim_batch` = 3）。
- [ ] 多个 `recharged` 候选时，回收余额最高的那个；`credits_balance` 为 NULL 的
      排在所有有余额的之后。
- [ ] 账号有任一平台行处于非终态（如 `registered` 等着首充）时，环境**永不**被回收
      ——这条既有保护不能被新档位破坏。
- [ ] 连 `recharged` 都没有时，仍如实抛「配额已满，且没有可回收的环境」。
- [ ] 回收第 3 档的日志能看出是「牺牲活账号」并带余额。
- [ ] 既有测试中 `test_reclaim_allowed_once_all_platforms_finished` 按新语义重写
      （它现在用 `recharged` 断言可回收，正是本任务要反转的行为）；其余全绿。
