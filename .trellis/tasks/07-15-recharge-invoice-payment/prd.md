# 充值放行绑卡账号并执行账单支付

## Goal

让每日流水线的批量充值阶段，对所有已绑卡（绑卡数 ≥ 1）的账号——**即使今天已经提交过充值**——也进入充值流程，由内部逻辑决定是执行 Top-up 还是转去执行「未付账单支付（Unpaid invoice）」。同时修复由此暴露的记账误报：只付账单、未实际充值的场景不得被记为「$10 充值成功」。

## Background

- 现状 bug：批量充值候选筛选 [app.py:643-648](../../../src/web/app.py#L643-L648) 用 `has_today_record(email)`（账号级）把今天有任何充值记录的账号整个排除，导致绑卡账号根本进不了 `recharge_account`，其内部本已具备的账单支付逻辑（[registration.py:543-565](../../../src/services/registration.py#L543-L565)）从未被触发。
- 现网数据佐证：4 个有 success 绑卡的账号今天都已有充值记录，故充值候选为 0；用户期望这些账号仍要检查并支付待付账单。
- 单纯删掉外层过滤会引入**误报**：`_recharge_one_account` 在 [app.py:416](../../../src/web/app.py#L416) 预建一条 `amount=10` 的 log，而「只付账单」分支返回 `success=True, responses=[]`，会走到 [app.py:481](../../../src/web/app.py#L481) 的 `elif success:` 被 `mark_success` 成假的 $10 充值成功。真正的账单支付在 registration 内部已通过 `_on_invoice_paid`/`_on_invoice_failed` 独立记账。

## Requirements

- R1：批量充值候选筛选去掉 `has_today_record` 条件，放行所有 `cf_password` 存在且绑卡数 ≥ 1 的账号（保留原有 `cf_password`、绑卡数条件；`banned` 等既有排除规则如适用则保留）。
- R2：`recharge_account` 需让调用方能明确区分三种结局：①实际 Top-up 成功；②未 Top-up、仅执行了账单支付（含「无待付账单」的 no-op）；③失败。不得再靠 msg 字符串猜测。
- R3：「仅账单支付/跳过充值」场景不得产生假的 $10 充值成功记录：预建 log 需被删除或标记为非充值终态，`recharge_success_total` 不得因未实际充值而 +1。
- R4：内层按卡的 `has_today_record(email, card_last4)` 判定保持不变——今天充过的卡不重复 Top-up，直接进入账单支付；今天没充过的卡正常 Top-up。
- R5：不新增「多卡分别 Top-up」逻辑——绑 2 张卡的账号仅处理 Top-up 弹窗默认显示的那张卡 + 账单支付（用户已确认）。

## Acceptance Criteria

- [ ] AC1：绑卡数 ≥ 1 且今天已充过的账号会进入 `recharge_account`（日志能看到其被列为充值候选并实际登录处理），不再被外层直接跳过。
- [ ] AC2：对今天已充过的账号，日志体现执行了 Unpaid invoice 检查/支付流程；有待付账单时被支付，无待付账单时安全 no-op。
- [ ] AC3：仅执行账单支付、未实际 Top-up 的账号，`recharge_logs` 中不新增 `status='success'` 的 $10 充值记录；流水线结尾统计的「充值成功」数不包含这些账号。
- [ ] AC4：今天未充过的绑卡账号仍能正常 Top-up 并记为成功（原有成功路径不回归）。
- [ ] AC5：账单支付本身的记账（`_on_invoice_paid`/`_on_invoice_failed`）不受影响，无重复或丢失。

## Out of Scope

- 多张卡分别 Top-up / 弹窗内切换支付卡。
- 「今天 Top-up 失败是否应重试」的策略调整（`has_today_record` 不区分成功失败的既有行为本次不改）。
- 前端展示层改动。

## Notes

- 关键约束：账单支付的记账已在 registration 内部完成，app 层预建 log 仅服务于 Top-up 记账，二者不能重复计充值。

---

# 追加范围（2026-07-15 实跑发现，R1–R5 已于 commit 11dfbef 落地）

> R1–R5 已实现并提交。开启每日任务实跑（2026-07-15 18:28，4 个绑卡账号）暴露出 **Top-up 路径本身的记账缺陷**，是上一轮"仅账单误记成功"修复未覆盖的另一类问题。本次实跑证据：`gcjpmyg59l / qz7515q5al / vrmgdaffev` 三个账号被记为「充值成功」，但三者真实 `credits_balance` 全为 `0.00`——本轮 Top-up 成功判定 100% 假阳性。

## 追加 Background

- **Bug A — Top-up 拒付被误记成功**：`fill_topup_and_confirm`（[driver.py:1680](../../../src/browser/driver.py#L1680)）只要"点了 Confirm 按钮 + 收到响应"就 `return True`，不解析 Stripe `payment_intents/confirm` 是否拒付；`_recharge_one_account`（[app.py:458-468](../../../src/web/app.py#L458)）挑响应时先命中 CF `topup`（HTTP 200、`success:true`，仅表示"已创建支付意图"）即 `mark_success`，Stripe 的 402 `card_declined` 从不检查。`gcjpmyg59l` 实测拿到 Stripe 402 `transaction_not_allowed`，仍记成功。invoice 路径已有的 `decline_code` 解析 / 余额记录 / 标坏卡逻辑，Top-up 路径完全没有。
- **Bug B — Top-up 后账单漏检**：充值后返回 Credits 页检查 Unpaid invoice（[registration.py:577-591](../../../src/services/registration.py#L577)）是"一次性"查询——`handle_unpaid_invoices` 第一轮查到 0 行即 `break` 报"未发现"（[driver.py:2267-2270](../../../src/browser/driver.py#L2267)）。刚做完 Top-up 时新欠费账单尚未渲染/落库，单次查询扑空。`qz7515q5al` 实测 `dismiss_overdue_dialog` 两次都关掉了欠费弹窗（反证存在欠费），却查到 0 行；同一轮 `fl58bpop4a` 用**相同选择器**成功找到账单 `IN-71574523`——证明是时序问题而非选择器问题。
- **脏数据**：`recharge_logs` id 70/71/72 = 假 success（余额均 $0）；`valid_cards` id 8（0217/qz）、id 9（7772/vrmgdaffev）是本次误插的 payment 有效卡（`INSERT OR IGNORE` + `UNIQUE(card_number,source_type)`，4673 因 id=3 已存在故本次未新增，id=3 为 07-14 旧记录、保留不动）。

## 追加 Requirements

- **R6**：Top-up 成功判定必须以 Stripe `payment_intents/confirm` 为权威。CF `topup` 200/`success:true` 不足以判成功。Stripe confirm 命中 402 / `decline_code` / `last_payment_error` 即判**失败**，`mark_failed` 并带拒付原因，**不得** `valid_card.record`。
- **R7**：余额兜底。当未捕获到 Stripe confirm 响应（仅拿到 topup 200）时不得直接判成功；以充值后读回的 Credits 余额是否较充值前增长作为兜底，未增长则判失败/存疑，不记成功。
- **R8**：Top-up 后账单检查健壮化。不得"查一次就 break"：需等待账单表格加载，并在 0 行时有限次重载重试；`dismiss_overdue_dialog` 返回 True（存在欠费弹窗）却查到 0 行时，必须继续重载重试而非判"无账单"。
- **R9**：订正已落库脏数据（用户已确认随代码修复一并处理）。`recharge_logs` id 70/71/72 由 success 改 failed（注明订正原因）；删除 `valid_cards` id 8、id 9；id 3（4673）不动。
- **R10**：Top-up 因卡本身被拒（`card_fault=True`：`card_declined`/`transaction_not_allowed`/`expired`/需 3DS 等）时，把该账号对应的**成功绑定卡记录**标记为失效（用户已确认），使其：①不再计入账号有效绑卡数、②不被后续充值/账单选用、③不被重新绑定。仅当拒付归因于卡本身时标记；瞬时错误（`processing_error`/`try_again_later`）与脚本侧问题不标记（复用 invoice 路径 `_extract_payment_error` 的 `card_fault` 语义）。

## 追加 Acceptance Criteria

- [ ] AC6：Top-up 时 Stripe confirm 返回 402/拒付的账号被记为 failed（带拒付原因），`recharge_logs` 不出现该账号的 `status='success'`，`valid_cards` 不新增该卡。
- [ ] AC7：仅捕获到 topup 200、无 Stripe confirm 且充值后余额未增长的账号，不被记为成功。
- [ ] AC8：Top-up 后存在待付账单（含刚生成、首查未渲染）时，经重载重试能被检出并进入支付流程；确无账单时安全 no-op，不误判、不死循环。
- [ ] AC9：脏数据订正完成——id 70/71/72 状态为 failed，valid_cards id 8/9 已删，id 3 仍在；订正为一次性操作，不随每次运行重复执行。
- [ ] AC10（回归）：今日未充、且卡确实扣款成功的绑卡账号仍能正常 Top-up 并记为 success，余额如实增长。
- [ ] AC11：Top-up 因卡本身被拒的账号，其对应绑定卡记录被标记失效——`count_by_emails` 不再计入它、后续不再被选来充值、也不会被重新绑定；瞬时/脚本类失败不触发标记。

## 追加 Out of Scope

- 对 Top-up 弹窗默认卡以外的卡做多卡 Top-up（沿用既有 Out of Scope）。
- 修改 Cloudflare 侧的默认支付卡 / 解绑已绑卡——R10 只在本系统数据层把拒付卡标记失效，不去 CF 改绑定关系。
- 3DS 银行验证的自动完成（`fl58bpop4a` 的账单因需 3DS 而失败属既有行为）。
