# 已充值账号按余额上限复用，取代「每次运行一次」的限制

## Goal

让平台状态为 `recharged` 的账号**只要余额没到 `balance_cap` 就能持续参与轮转**，
而不是每次运行只被复用一次。用户诉求原话：「只要没有达到余额上限 都可以参与任务」。

## Background

`run_daily_pipeline._reusable_recharged()`（`src/web/app.py`）当前有两道闸：

1. `bal is not None and bal >= cap` —— 真正想要的那道（余额闸）；
2. `a['email'] in reused_this_run` —— **每次运行每个账号只复用一次**（次数闸）。

第 2 道让那 10 个余额只有 $20–$93 的账号（`balance_cap` 默认 200）每轮只能充一笔就
出局，钱铺不开，正是要去掉的。

但它不能直接删。代码注释记着代价：

> **不这样会死循环。** 判据里的 DB 余额 `credits_balance` 可能是 NULL：`balance_after`
> 读不到时 `update_balance` 会跳过（infron 常态，opencode 偶发）。余额永远是 NULL 的
> 账号会永远满足「未达上限」，被一轮轮反复领走反复充，钱全堆到一个号上，而且任务不收敛。

`PlatformAccountModel.update_balance` 确认了这个前提：`if balance is None: return`。
NULL 之外还有同构的坏法——**余额停在旧值不再更新**，账号永远显示 $20、永远满足 `< 200`，
同样无限重领。

所以本任务不是「删掉次数闸」，而是**换一道能自我收敛的闸**。

现场佐证（2026-08-05）：一次运行跑到第 113 轮仍不收敛，每轮只有 2 个账号在打转，
`success=2 / fail=147`，可选卡从 3426 被烧到 2796。收敛兜底「连续 2 轮零进展」在
「卡集合每轮都变化」时判定失效——本任务的安全网必须独立于那条兜底。

## Requirements

- R1 `recharged` 账号的参与资格改由**余额**决定：
  `有效余额 = (DB 余额 or 0) + 本次运行已给该账号成功充值的累计金额`，
  `有效余额 < balance_cap` 即可参与。
- R2 移除 `reused_this_run` 次数闸。
- R3 **收敛性必须有保证**，且不依赖 DB 余额是否被更新：
  每次成功复用都会让「本次运行已充金额」增加至少 `amount_min`（≥$1），
  因此单账号最多被复用 `ceil(cap / amount_min)` 次后必然出局。
- R4 **不设次数上限**。判据只有余额一条，充值金额保持现有的区间随机不变。
  理由：`_payable_now` 那档本来就没有次数闸，复用池没有理由更严；失败路径的收敛
  由轮层面的机制负责（见 R8），而不是给某一档单独加限制。
- R5 「本次运行已充金额」的口径：`recharge_logs` 中 `platform` 与 `email` 匹配、
  `status='success'`、`created_at >= 本次运行开始时刻` 的 `amount` 之和。
- R6 **可复用账号与可充值账号合并为同一档**，一起参与领取；待注册 imported 仍排其后。
  现状是三档串行（可充值 → 待注册 → 可复用），库里 49 个 imported 会把 10 个余额
  未满的老账号饿死——worker 一直忙着注册，老账号几乎永远轮不到。
  注册耗时且易被 GitHub flag，现成账号优先更稳。
- R7 不改 `_recharge_one_account` / `registration.recharge_account` 的签名。
- R8 修复「连续零进展」收敛兜底：**可选卡变少不再算作进展**。
  现状把「可选卡集合有任何变化」都算进展，而卡被逐张标 invalid 每轮都在变，
  于是 `zero_rounds` 永远被清零、任务永不收敛（2026-08-05 跑到第 113 轮，
  烧掉 630 张卡）。改为只有「付成了卡」或「可选卡集合有**新增**」才算进展——
  后者保留原意（冷却到期有新卡可用时值得再试一轮）。
  去掉 R4 的次数闸后，这是失败路径唯一的收敛保障，必须同期修好。

## Non-Goals

- 不动 `balance_cap` 的默认值与 UI 覆盖机制。
- 不动充值金额的随机区间逻辑。
- 不动余额读取本身（`update_balance` 遇 None 跳过的行为保持不变）。
- 不动 `failed_this_round` 的本轮跳过语义。
- 不动待注册 imported 的领取逻辑本身（只调整它在顺序中的位置）。

## Acceptance Criteria

- [ ] AC1 余额 $20、cap $200 的 `recharged` 账号，一次运行内可被复用**多次**，
      而不是一次。
- [ ] AC2 累计充到 cap 后该账号退出轮转，不再被领取。
- [ ] AC3 **DB 余额恒为 NULL** 的账号仍然收敛：靠本次运行累计金额推进到 cap 后出局，
      任务正常结束而非挂死。
- [ ] AC4 **DB 余额停在旧值不更新**的账号同样收敛（与 AC3 同一条路径）。
- [ ] AC5 可复用账号与可充值账号**同档**领取：两类都有货时，worker 不必等新账号
      跑完就能领到余额未满的老账号；待注册 imported 仍排其后。
- [ ] AC6 充值全失败、可选卡持续被标废的情形下，任务在**有限轮内收敛**
      （复现 113 轮那个场景，断言轮数有界）。
- [ ] AC7 可选卡集合有**新增**（如冷却到期）时仍算进展，不被误判为零进展而提前收敛。
- [ ] AC8 现有回归测试 `test_recharged_accounts_are_reused_at_most_once` 按新语义改写，
      不是删除——它守的「必须收敛」这条不变，变的只是收敛方式。
      `test_reuse_only_kicks_in_after_new_accounts_are_exhausted` 因 R6 改变了顺序语义，
      需按新顺序改写。
- [ ] AC9 `pytest tests/` 全量通过。
