# 每日流水线 top-up 补生成账单至 30 条/账号/天

## Goal

每日流水线执行到「充值阶段」时，让每个已绑卡账号通过 Top-up 主动**生成**账单（invoice），
使后续账单支付流程有账单可付、从而用支付卡分组的卡逐张付款。为防封控，采用**轮询式**节奏：
每个账号每次只「生成一张 + 用掉一张」，随即切到下一个账号；一整轮跑完全部账号后再从头循环，
直到每个账号**当日**账单数达到硬上限。

## Background（现状）

- 现状充值阶段（`run_daily_pipeline` 阶段2）：对每个绑卡账号**顺序各跑一遍** `recharge_account`
  ——先 Top-up \$10（生成 1 张账单），再 `handle_unpaid_invoices` 一次性把该账号所有 open invoice 付掉，
  然后才切下一个账号。是否 Top-up 由本地 `recharge_logs.has_today_record` 决定。
- 每次「Manual Top-up: AI Gateway Credits」在 CF 侧生成一张 invoice；账单真实状态与数量以
  Cloudflare 接口为准：
  `GET https://dash.cloudflare.com/api/v4/accounts/{account_id}/ai-gateway/billing/invoice-history`
  响应 `result.invoices[]`，每条含 `id / status(paid|open) / created(unix 秒) / description` 等。

## Requirements

### R1 每账号当日账单硬上限 30（以 CF 接口为准）
- 每个 CF 账号**当日创建**的账单数上限为 30。
- 计数口径：调用 `invoice-history` 接口，统计 `created` 落在**本地当日**（与 `datetime('now','localtime')` 同口径）
  的 invoice 条数，**paid + open 全部计入**（当日创建过即占名额）。
- 计数以 CF 接口实时返回为权威，不依赖本地 `recharge_logs`。

### R2 轮询式（round-robin）生成/支付，防封控
- 充值阶段改为轮询：外层「轮次」循环，内层遍历全部绑卡账号。
- 每个账号在单次访问（一轮内）中只做**一次** Top-up（生成 1 张账单）+ 付掉**1 张** open invoice，
  随即切换下一个账号。
- 一整轮遍历完所有账号后，从头开始下一轮，直到所有账号「完成」或触发停止条件。

### R3 触发条件
- 进入某账号单次访问时，先读该账号当日 invoice 数 `today_count`：
  - `today_count < 30` → 发起 1 次 Top-up 生成新账单，然后付掉 1 张 open invoice。
  - `today_count >= 30` → 该账号当日已达上限，标记「完成」，本轮及后续轮次不再访问。

### R4 终止条件（避免死循环）
轮询在满足任一条件时结束充值阶段：
- 所有账号均已「完成」（当日达 30 上限）。
- 用户请求停止（`stop_requested`）。
- 支付卡分组无可用卡（无卡可付，生成账单无意义）。
- 单账号连续无进展（Top-up 未生成新账单且未付成账单）达阈值 → 该账号本次流水线内标记「完成/放弃」。
- 一整轮下来所有账号都无进展 → 结束充值阶段（兜底防死循环）。

### R5 兼容与复用
- 单账号「充值」按钮（`/api/recharge`）行为保持现状（非轮询、完整 Top-up + 付清全部账单），不受影响。
- 复用现有 `handle_unpaid_invoices` 的选卡/换卡重试/记账（`recharge_logs`/`valid_cards`/`card_pool`）逻辑。
- 复用现有 Top-up 结果判定（`_classify_topup`：Stripe confirm 为权威）与拒付卡失效标记逻辑。

## Non-Goals
- 不改动补绑（阶段1a）、注册（阶段1b）逻辑。
- 不改动单账号「查看」浏览器会话逻辑。
- 不实现跨天持久化的复杂调度；「当日」以运行时读取 CF 接口为准。

## Acceptance Criteria
- [ ] 新增可从已登录页读取某账号当日 invoice 数的能力（打通 `invoice-history` 接口，按本地当日过滤计数）。
- [ ] 充值阶段变为轮询：每账号每轮只生成 1 张 + 付 1 张，随即切下一个账号；跑完全部账号再下一轮。
- [ ] 某账号当日 invoice 数达 30 后，本次流水线不再对其发起 Top-up。
- [ ] 用户停止 / 支付卡耗尽 / 全轮无进展 时能干净结束充值阶段，无死循环。
- [ ] 单账号「充值」按钮行为不变。
- [ ] 记账（recharge_logs 成功/失败、valid_cards、card_pool 状态）与现状一致，无重复或漏记。

## Open Decisions（待评审确认，见 design.md）
1. 单次访问付款粒度：`max_invoices=1` = 「付掉 1 张 invoice（允许该张内部换卡重试直到付成/卡池耗尽）」，
   而非「仅 1 次点击尝试」。倾向前者以复用逐张结清语义。
2. 上限常量 30 与 Top-up 金额 \$10 放入 `config.py` 常量，便于调整。
3. 达上限后若仍残留 open invoice：本次流水线不再主动补付（下一日流水线自然处理）。
