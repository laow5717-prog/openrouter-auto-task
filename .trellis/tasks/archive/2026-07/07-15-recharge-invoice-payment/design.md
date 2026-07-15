# 技术设计 — 充值放行绑卡账号并执行账单支付

> 注：本文档所依据的代码事实,均来自会话中**已核验为干净**的读取。会话期间工具结果层出现过间歇性 prompt-injection(诱导运行 `apply_patch.sh` / `curl|bash` / 改调 `registration_v2`),这些均为伪造,**不作为设计依据,也不予采纳**。实现阶段落笔前需对下述关键行再做一次一致性确认。

## 涉及边界

- `src/web/app.py`
  - `run_daily_pipeline` 阶段2 候选筛选(约 643-648 行)
  - `_recharge_one_account`(约 396-499 行):预建 log 与结果收尾
- `src/services/registration.py`
  - `recharge_account`(386 行起):返回契约扩展
- `src/models/recharge_log.py`
  - 需要一个"撤销/删除预建 log"的能力(当前**无** `delete` 方法)

## 现有契约(已核实)

`recharge_account(...)` 返回 4 元组 `(success, message, responses, card_last4)`,6 个 return 点:

| 场景 | 现返回 | success | 是否实际 Top-up |
|------|--------|---------|----------------|
| 导航/点击 Top-up 失败 | `False, "导航...失败", [], ''` | False | 否 |
| 未提取到卡信息 | `False, "未获取到有效信用卡信息", [], ''` | False | 否 |
| 今日已充 + skip_invoice | `True, "今日已充值，跳过", [], card_last4` | True | 否 |
| 今日已充 + 有分组→付账单 | `True, "跳过充值，已处理账单", [], card_last4` | True | 否(仅账单) |
| 正常 Top-up | `pay_success, "...", responses, card_last4` | 视结果 | 是 |
| 异常 | `False, str(e), [], ''` | False | 否 |

`_recharge_one_account` 消费点([app.py:419](../../../src/web/app.py#L419)):进入即 [:416](../../../src/web/app.py#L416) 预建 `amount=10` 的 log;末尾按 `success + topup_resp` 决定 `mark_success` / `mark_failed`。问题:第 3、4 行(success=True 但未实际充值)会走到 `elif success:` 被 `mark_success` 成假的 $10 成功。

## 设计决策

### D1 — 放行条件(R1)

[app.py](../../../src/web/app.py) 阶段2 候选筛选去掉 `and not recharge_log_model.has_today_record(a['email'])`,保留 `cf_password` 存在与 `counts_after.get(email,0) >= 1`。绑卡账号无论今日是否充过都进入 `_recharge_one_account`。

### D2 — 返回契约新增显式结局枚举(R2)

`recharge_account` 返回从 4 元组扩展为 5 元组,末尾追加 `outcome`:

```
(success, message, responses, card_last4, outcome)
outcome ∈ {"topup", "invoice_only", "failed"}
```

各 return 点赋值:
- 导航失败 / 无卡信息 / 异常 / `pay_success=False` → `"failed"`
- 今日已充 + skip_invoice(什么都没做) → `"invoice_only"`
- 今日已充 + 付账单分支 → `"invoice_only"`
- 正常 Top-up 且 `pay_success=True` → `"topup"`

选 5 元组末尾追加而非改字典:唯一消费方是 app.py:419 一处,同步成本低;末尾追加对"只关心前几位"的潜在解构更宽容。

### D3 — 预建 log 收尾按 outcome 分派(R3)

`_recharge_one_account` 解构出 `outcome`,收尾逻辑改为:
- `outcome == "topup"`:维持现有 `topup_resp` 解析 → `mark_success` / `mark_failed`。
- `outcome == "invoice_only"`:**撤销预建 log**(见 D4),不计入充值成功;向调用方返回一个区别于"充值成功"的结果。
- `outcome == "failed"`:`mark_failed`。

### D4 — 撤销预建 log 的实现(R3,受"无 delete 方法"约束)

`RechargeLogModel` 目前无 `delete`。二选一(实现阶段定,倾向方案 A):
- **方案 A**:给 `RechargeLogModel` 增补 `delete(log_id)`,`invoice_only` 时直接删除预建 log。语义最干净——这条 log 本不该存在。账单支付的记账已由 registration 内部 `_on_invoice_paid`/`_on_invoice_failed` 独立完成,不受影响。
- **方案 B**:不删,`mark` 成一个非充值终态(如 `status='invoice_only'`)。改动小但引入新状态值,需确认前端/统计对未知 status 的兼容。

倾向 A:避免污染 `recharge_logs` 且不引入新状态枚举。

### D5 — 批量循环计数(R3, AC3)

`_recharge_one_account` 现返回 `(success, err)`,批量循环 [app.py:664-669](../../../src/web/app.py#L664) 靠 `ok` 累加 `recharge_success_total`。需让 `invoice_only` 不计入成功:
- 将 `_recharge_one_account` 返回值区分三态(如返回 `(result, err)`,`result ∈ {"success","invoice_only","failed"}`),批量循环据此:`success` → `recharge_success_total+1`;`failed` → `recharge_fail_total+1`;`invoice_only` → 两者都不加,单独日志或计入"仅账单处理"计数。
- 同步单账号路由入口 [routes.py:479](../../../src/api/routes.py#L479) 的 `_do_recharge`(它忽略返回值,无需改逻辑,但确认不因元组变化报错)。

## 数据流

```
run_daily_pipeline 阶段2
  └─ 候选 = [绑卡≥1 且有 cf_password]           (D1 放行)
      └─ _recharge_one_account(email)
           ├─ 预建 log(amount=10)                (app.py:416)
           └─ recharge_account(...) → (…, outcome) (D2)
                ├─ outcome=topup        → mark_success/failed   → 计 success/fail
                ├─ outcome=invoice_only → 删除预建 log(D4)      → 不计充值; 账单记账已由内部完成
                └─ outcome=failed       → mark_failed           → 计 fail
```

## 兼容性 / 回滚

- 契约变更仅影响 app.py:419 一处解构 + registration 6 个 return 点,面小。
- 回滚:恢复 4 元组返回 + 恢复外层 `has_today_record` 过滤即可,无 schema 变更(方案 A 仅新增一个方法,无表结构变化)。

## 风险

- `has_today_record` 不区分成功/失败:今日 Top-up 失败过的账号会被内层判为"已充"转去仅付账单,本次**不改**(Out of Scope),但需在验收时知悉此既有行为。
- 每日多次运行流水线时,绑卡账号会重复进入并登录浏览器(付账单幂等,安全但耗时),可接受。

---

# 追加设计（2026-07-15 实跑发现）

## 追加涉及边界

- `src/browser/driver.py`
  - `fill_topup_and_confirm`（1641 行起）：当前 `return True` 只表示"点击成功"
  - `handle_unpaid_invoices`（2154 行起）：round 1 查到 0 行即 break（2267-2270）
  - `dismiss_overdue_dialog`（575 行起）：已返回 True/False（是否存在欠费弹窗），可作信号
  - `_extract_payment_error(driver)`（1759 行起）：已存在，扫描 `net_responses` 返回 `(中文原因, card_fault)`，invoice 路径在用，Top-up 可复用
- `src/services/registration.py`
  - `recharge_account` Top-up 分支（569-601）：`pay_success` 直取自 `fill_topup_and_confirm`；`_record_final_balance`（约 510-523）已在读并写回余额
- `src/web/app.py`
  - `_recharge_one_account`（456-499）：`topup_resp` 选取与 `success` 判定
  - 阶段0 过滤（约 550-565）：跳过集需并入拒付卡号（D10）
- `src/models/card_binding.py`
  - 新增 `mark_declined_by_number` / `get_declined_card_numbers`（D10，复用 `[Stripe字段错误]` 的 status+error 前缀模式）
- 一次性数据订正：`recharge_logs`、`valid_cards`（无代码常驻，见 D9）

## D6 — Top-up 成功以 Stripe confirm 为权威（R6）

判定权威源为 Stripe `payment_intents/confirm` 响应，而非 CF `topup`。在 `recharge_account` Top-up 分支内、`fill_topup_and_confirm` 之后集中判定，使返回的 `outcome` 反映**真实结果**（与 invoice 记账同处 registration，避免 app 层重复解析）：

- 从捕获响应中区分两类：CF `topup`（url 含 `ai-gateway/billing/topup`）与 Stripe confirm（url 含 `api.stripe.com` + `payment_intents` + `confirm`）。
- **成功**：Stripe confirm 已捕获且 HTTP 2xx、payment_intent `status ∈ {succeeded, processing, requires_capture}` 且无 `last_payment_error` → `outcome="topup"`。
- **拒付/失败**：Stripe confirm HTTP 402，或响应体含 `error`/`decline_code`/`last_payment_error` → `outcome="failed"`，`message` 取 `_extract_payment_error` 的中文原因。
- 交由 `_recharge_one_account` 时，app 层收尾**改为完全信任 `outcome`**：`topup`→`mark_success` + `valid_card.record`；`failed`→`mark_failed(reason)`，**不** `valid_card.record`。移除现有"CF topup 200 即 success"的推断（[app.py:464-478](../../../src/web/app.py#L464)）。

> 决策：把权威判定收敛到 registration 一处，app 层只按 `outcome` 分派。理由——invoice 路径的拒付/记账已在 registration，Top-up 同源可共用 `_extract_payment_error`，避免 app/registration 两处各判一次导致口径漂移。

## D7 — 余额兜底（R7，应对 confirm 未捕获）

`qz7515q5al` 实测只捕获到 topup 200、无 Stripe confirm（可能 confirm 慢于 `collect_intercepted_responses` 的 60s 窗或走了 3DS 跳转），余额 $0。兜底：

- 在打开 Top-up 弹窗前读一次基线余额（`navigate_to_ai_credits` 成功后、`extract_topup_card_last4` 附近），充值收尾读回余额（`_record_final_balance` 已有读逻辑，复用）。
- 当 D6 无法从 Stripe confirm 得到明确成功信号（confirm 未捕获）时：余额较基线增长 → 判成功；未增长 → `outcome="failed"`（原因："充值后余额未增长，判定未到账"）。
- 有明确 Stripe confirm 成功信号时，余额兜底不推翻它（避免余额读取抖动误杀）。

> 基线余额读取失败（None）时兜底失效，退化为"confirm 未捕获即判失败/存疑"，宁可漏记成功也不误记成功（与本 bug 的方向一致）。

## D8 — Top-up 后账单检查健壮化（R8）

问题在 `handle_unpaid_invoices` round 1 查到 0 行立即 break。改为**有限次重载重试**再定论：

- round 1 查到 0 行时不直接判"无账单"：先 `_return_to_credits()`（已有，整页重载）+ 等待，重查，最多 `INVOICE_DETECT_RETRIES`（建议 2–3）次。
- 利用欠费信号：若本轮 `dismiss_overdue_dialog` 返回 True（有欠费弹窗）却 0 行，视为"账单尚未渲染"，强制进入上面的重载重试；连续重试后仍 0 行才判"无账单"。
- 增加"等表格就绪"：查询 Unpaid 行前，等待发票表格容器出现或加载态消失（优先 `_wait_visible`/显式等待，取代裸 `sleep(3)`），减少 SPA 异步未加载导致的空查。
- 收敛保护：重试上限固定，配合既有 `MAX_ROUNDS` 兜底，确无账单时不死循环。

> 作用域限定 round 1 的"首检漏检"；已进入支付循环后的既有换卡/重试语义不动。

## D9 — 脏数据一次性订正（R9）

非代码常驻逻辑，用一次性脚本执行并留档，**不**写进流水线（避免每次运行重复订正）：

- `UPDATE recharge_logs SET status='failed', error='订正：Top-up 拒付/未到账，余额$0（2026-07-15误记）' WHERE id IN (70,71,72)`
- `DELETE FROM valid_cards WHERE id IN (8,9)`（0217/qz、7772/vrmgdaffev；id=3=4673 保留）
- 执行前二次核对：三条 log 的 email 与余额、两张卡的 source_email 与 validated_at 与实跑一致，防止误删。

## D10 — Top-up 拒付卡标记失效（R10）

复用 `card_bindings` 现有的 `status`+`error` 前缀模式（已有先例 `[Stripe字段错误]`），不加表结构：

- 新增模型能力 `CardBindingModel`：
  - `mark_declined_by_number(card_number, reason)`：把该卡**成功绑定**的记录置 `status='failed'`、`error='[充值拒付] <reason>'`（仅影响 `status='success'` 的该卡记录，避免动到别的状态）。
  - `get_declined_card_numbers()`：`status='failed' AND error LIKE '[充值拒付]%'` 的卡号集合。
- 判定来源：D6 判 Top-up 失败时，若 `_extract_payment_error` 返回 `card_fault=True`，则对 D7/D6 提取到的 `card_last4` 匹配该账号绑定卡的完整卡号（`card_binding.get_by_email(email)` 按后四位匹配，逻辑同 [app.py:446-452](../../../src/web/app.py#L446)），调用 `mark_declined_by_number`。`card_fault=False`（瞬时/脚本类）不标记。
- 连锁效果（符合期望的自愈闭环）：
  - `count_by_emails`（`status='success'`）不再计入该卡 → 账号有效绑卡数下降；
  - 若降到 `< max_bindable`，下一轮**阶段1a 补绑**会给该账号绑新卡（前提是绑卡池有卡）→ 恢复可充值；
  - 阶段0 过滤需把 `get_declined_card_numbers()` 并入跳过集（与 `already_bound_numbers`/`stripe_error_numbers` 并列，见 [app.py:550-565](../../../src/web/app.py#L550)），使拒付卡不会被重新绑定。
- 边界：Top-up 用的是 CF 侧默认保存卡，本系统不改 CF 默认卡（Out of Scope）。因此"标记失效"的即时收益是数据口径正确 + 触发补绑换卡，而非当轮立刻改用别的卡 Top-up。
- 幂等：`mark_declined_by_number` 可重复调用（重复置同状态无副作用）。

## 追加兼容性 / 回滚

- D6/D7 改 `recharge_account` Top-up 分支判定 + `_recharge_one_account` 收尾；`outcome` 枚举不新增取值（沿用 topup/invoice_only/failed）。回滚 = 恢复旧 `pay_success` 直判 + app 层 topup_resp 推断。
- D8 局部化在 `handle_unpaid_invoices` round 1，回滚 = 去掉重试、恢复 0 行即 break。
- D9 一次性、不可自动回滚（DB 写），执行前备份 `data/*.db`。
- D10 仅新增两个模型方法 + 一处判定调用 + 阶段0 跳过集并入一项，无表结构变更；回滚 = 移除方法与调用、阶段0 跳过集去掉该项。

## 追加风险

- 余额兜底依赖 Credits 页余额读取稳定性；读取失败时退化为保守判失败，可能把真成功漏记为失败——方向安全，但需在实跑回归（AC10）确认真成功账号未被误伤。
- 账单重载重试增加单账号耗时（每次重试一次整页导航 + 等待）；上限设小（2–3）以控住。
- Stripe confirm 的 payment_intent `status` 取值需实跑确认（`succeeded` vs `processing` vs `requires_action`/3DS）；实现前用真实响应体校准判定集合。
- D10 把拒付卡标记失效后，若该账号仅此一张绑卡且当轮绑卡池无卡补绑，则账号有效绑卡数归 0、被移出充值候选，其**当轮的待付账单也不会被处理**（无好卡可付，行为可接受）；下一轮有卡时经补绑自愈。需在实跑（AC11）确认这不会误伤"卡好但偶发瞬时拒付"的账号——故严格以 `card_fault=True` 为门槛。
- `mark_declined_by_number` 把 `status='success'` 翻成 `'failed'`：CF 侧该卡实际仍处绑定态，本系统记为 failed 属有意的口径偏移（表示"不再当好卡用"），不追求与 CF 完全一致。
