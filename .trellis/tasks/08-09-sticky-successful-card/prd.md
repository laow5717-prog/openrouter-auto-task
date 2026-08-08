# 支付成功卡优先复用

## Goal

一张卡在某平台支付成功过，就应该被**继续使用**，而不是刷完一笔立刻换下一张。
当前两处逻辑都在做相反的事：

- `src/services/registration.py` 连充循环：一笔付成后 `continue`，下一笔换**下一张卡**；
- `src/web/app.py::_eligible_cards`：排序是 `fresh + good`，**新卡优先**，成功过的好卡排在最后。

结果是好卡被闲置、坏卡被反复拿去试，拒付率与账号风控压力都被推高。

## Requirements

### R1 — 会话内粘卡（`registration.recharge_account`）

- 一张卡 `top_up` 成功后，**下一笔继续用同一张卡**，不进入下一张。
- 退出粘卡（换下一张卡）的唯一条件是这张卡**本身失败**：`outcome ∈ {failed, unknown, error, needs_captcha}`，
  各自的既有处理（冷却 / 计数 / 判废 / 记账 / 立即停手）逐字保持不变。
- 以下既有的**停手判据全部保持原语义**，且必须能中断粘卡循环：
  - `should_stop()` 用户停止；
  - `attempts >= max_attempts`（试卡上限，防 velocity 风控）——**每一笔支付都计一次 attempt**，
    粘同一张卡不能绕过这个上限；
  - `balance_cap`（`balance_after >= cap` 或 `session_topped >= cap`）；
  - `needs_captcha` 立即 break，不再换卡。
- `payment_registry` 的 acquire / release 仍是**每张卡一次**：粘卡期间持续持有该卡的 in-flight 占用，
  离开这张卡时（成功走完或失败换卡）在 `finally` 里释放一次，不得泄漏。
- 卡状态语义不变：成功的卡不冷却、不消耗，标 `paid` + 记 `valid_cards` + `reset_fail_streak`。

### R2 — 选卡排序反转（`AppSharedState._eligible_cards`）

- 排序由 `fresh + good` 改为 `good + fresh`：**本平台成功付款过的卡排在队首**，新卡垫后。
- 分类判据（`recharge_log.all_success_card_numbers`、去空格比对）与冷却过滤逻辑不变。
- `_exclude_used_this_run` 的剔除行为**不改**：它防的是「同一张卡在一轮里被多个账号各刷一次」，
  那是盗刷特征、会撞发卡行风控。跨账号复用同一张好卡不在本次范围内。

### 范围外（明确不做）

- 订阅流程 `app.py::_subscribe_one_account` 不做任何改动。它本就是「付成一张即 return」，
  不存在换卡问题；但它复用 `_eligible_cards`，会**顺带**得到 R2 的好卡优先排序（已确认可接受）。
- 不新增配置项。「粘卡最多连充几笔」由既有的 `balance_cap` 与 `max_card_attempts` 兜住。

## Acceptance Criteria

- [ ] 单账号会话内一张卡连续付成 N 笔时，`responses` 里这 N 条的 `card_last4` 全部相同。
- [ ] 该卡失败一次后，后续尝试落到下一张卡，且失败卡照常进冷却 + `fail_streak +1`（达阈值判废）。
- [ ] 粘卡时每笔支付都让 `attempts` +1；`attempts` 达 `max_card_attempts` 时循环终止并写入上限提示。
- [ ] `balance_cap` 仍是硬上限：`session_topped >= cap` 时立即 break，不因粘卡被绕过。
- [ ] `needs_captcha` 时立即 break，且不标该卡无效。
- [ ] 无论成功连充、失败换卡还是抛异常，`payment_registry.release` 对每张卡恰好被调用一次。
- [ ] `_eligible_cards` 返回列表中，成功过的卡全部排在新卡之前；冷却中的卡仍被过滤掉。
- [ ] 既有测试全部通过（`pytest tests/`）。
