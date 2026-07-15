# 技术设计 — 每日流水线 top-up 补生成账单（轮询式）

## 1. 涉及模块与边界

| 层 | 文件 | 改动 |
|---|---|---|
| 浏览器 | `src/browser/driver.py` | ① 新增 `fetch_today_invoice_count()`；② `handle_unpaid_invoices` 增 `max_invoices` 参数 |
| 业务 | `src/services/registration.py` | `recharge_account` 增单步（round-robin）模式：读当日账单数 → 1 次 Top-up → 付 1 张 |
| 编排 | `src/web/app.py` | 阶段2 由「顺序遍历一遍」改为「轮询多轮」；`_recharge_one_account` 透传单步参数与返回 |
| 配置 | `src/config.py` | 新增 `INVOICE_DAILY_CAP=30`、`TOPUP_AMOUNT=10` 常量（或就近常量） |

数据流：`run_daily_pipeline` 轮询 → `_recharge_one_account(single_step=True, cap=30)` →
`registration.recharge_account(invoice_daily_cap=30)` → driver 读账单数 + Top-up + `handle_unpaid_invoices(max_invoices=1)`。

## 2. driver 层

### 2.1 `fetch_today_invoice_count(driver, account_id) -> int | None`
- 在已登录的 CF 页面上下文用 `page.evaluate` 执行 `fetch(invoice-history URL, {credentials:'include'})`，
  解析 JSON。URL：`/api/v4/accounts/{account_id}/ai-gateway/billing/invoice-history`。
- 计数：`result.invoices[]` 中 `created`（unix 秒）落在**本地当日 [今天0点, 明天0点)** 的条数，
  paid + open 全部计入。当日边界在 Python 侧用本地时区计算传入，或在 JS 内用页面本地时区；
  统一以「运行机器本地日」为准（与项目 `datetime('now','localtime')` 口径一致）。
- 需处理分页：示例 `per_page=8`、`pagination.has_more`。当日账单数上限 30，可能跨页。
  实现：循环 `page=1..N` 直到 `has_more=false` 或已翻过当日最早记录（invoices 按 created 倒序，
  一旦某页最后一条 `created < 今日0点` 即可停止翻页）。
- 失败（网络/解析/非 200/`success!=true`）→ 返回 `None`，调用方保守处理（视为无法判定，
  按「未达上限」继续但由连续无进展兜底，或直接跳过该账号本轮——见 §4 决策）。

### 2.2 `handle_unpaid_invoices(..., max_invoices=None)`
- 新增可选参数 `max_invoices`：一次调用**成功付掉**的 invoice 数上限。默认 `None`＝不限（现状）。
- 语义：`max_invoices=1` 表示「付掉 1 张 invoice 即返回」。一张 invoice 内部的换卡重试/脚本重试
  语义不变（复用 `done_ids`/`_give_up`/`_script_fail`）；计数只在一张 invoice **成功付掉**（`on_paid` 触发）
  时递增，累计达 `max_invoices` 即跳出主循环 return。
- 放弃（`_give_up`，重试耗尽）是否计入 `max_invoices`？**不计**——只有成功付掉才占额度，
  否则一张付不掉的坏账单会让单步空转。但需与 §4「连续无进展」配合防止反复选中同一张坏账单：
  `done_ids` 在单次 `recharge_account` 调用内有效，坏账单本次调用已进 `done_ids` 不再重选；
  但**跨轮次**（下一轮该账号新的 `recharge_account` 调用）`done_ids` 重置——坏账单会被重新选中。
  → 由 driver 无法跨调用记忆；防坏账单空转靠 §4 的连续无进展阈值在编排层兜底。

## 3. registration 层：`recharge_account` 单步模式

新增参数 `invoice_daily_cap: int | None = None`。为 `None` 时＝现状全量行为，完全不改。
为整数（如 30）时进入**单步模式**，流程：

1. `create_driver` + `login_cloudflare`（同现状）。
2. 复用现有选卡资格闸门（R1/R2/R3）、`_get_card`、`_on_invoice_paid`、`_on_invoice_failed`、
   `_classify_topup`、`_record_final_balance` 等闭包，**不改**。
3. `today_count = fetch_today_invoice_count(driver, account_id)`。
   - `today_count is not None and today_count >= cap` → 返回 outcome `"cap_reached"`，
     `info={'today_count': today_count}`；不做 Top-up、不付账单。
4. 未达上限（或读取失败按未达处理）：
   - 导航 + 打开 Top-up 弹窗（`navigate_to_ai_credits`），`extract_topup_card_last4`（同现状拿基线/卡四位）。
   - `fill_topup_and_confirm(driver, amount=TOPUP_AMOUNT)` 生成 1 张账单。
   - 返回 credits 页，`handle_unpaid_invoices(..., max_invoices=1)` 付 1 张。
   - `_classify_topup` 判定 Top-up 真伪；拒付卡失效标记同现状。
   - 结束时**再读一次** `today_count_after = fetch_today_invoice_count()` 放进 `info`，
     供编排层判定是否达上限 / 是否有进展。
   - 返回 outcome `"stepped"`，`info={'today_count': today_count_after, 'paid': <int>, 'topup_ok': <bool>}`。

### 返回契约变更
现状：`(success, err, responses, card_last4, outcome)` 5 元组。
改为：`(success, err, responses, card_last4, outcome, info)` 6 元组，`info: dict`（全量模式给 `{}`）。
- 唯一调用点：`app.py::_recharge_one_account`（`registration.recharge_account(...)`）。
- outcome 取值扩展：现有 `"topup"/"invoice_only"/"failed"` 之外，单步模式新增
  `"cap_reached"`（当日已达 30）、`"stepped"`（本次做了 1 生成 + 至多 1 付）。

## 4. 编排层：`run_daily_pipeline` 阶段2 轮询化

### `_recharge_one_account` 签名扩展
```
_recharge_one_account(email, cf_password, payment_group_id=None,
                      single_step=False, invoice_daily_cap=None)
    -> (result, err, info)
```
- `single_step=False`（按钮/现状）：行为与返回**完全不变**（内部仍返回 `(result, err)`，
  为兼容可让调用方忽略 info，或统一返回 3 元组并让旧调用点解包前两个）。
- `single_step=True`：把 `invoice_daily_cap` 透传给 `registration.recharge_account`，
  按 6 元组接收，映射 outcome：
  - `"cap_reached"` → `result="cap_reached"`
  - `"stepped"` → `result="stepped"`（成功付了 1 张则同时视为一次成功记账，已在 driver 内完成）
  - `"failed"` → `result="failed"`
  并把 `info`（含 `today_count`）返回给轮询循环。

### 阶段2 轮询主循环（替换现有 `for acct in recharge_targets`）
```
DAILY_CAP = 30
targets = [绑卡≥1 的账号]
done = {}           # email -> 原因（cap_reached / abandoned）
no_progress = {}    # email -> 连续无进展次数
MAX_NOPROG = 3      # 单账号连续无进展阈值
MAX_ROUNDS = DAILY_CAP + 2   # 兜底（正常每轮每账号 +1 张，30 轮内必达上限）

# 若支付卡分组无可用卡 → 直接跳过整个轮询（无卡可付，生成无意义）
for round in range(MAX_ROUNDS):
    if stop_requested: break
    if 所有 targets 都在 done: break
    progressed_any = False
    for acct in targets:
        if stop_requested: break
        if acct.email in done: continue
        result, err, info = _recharge_one_account(email, pwd, payment_group_id,
                                                   single_step=True, invoice_daily_cap=DAILY_CAP)
        today_count = info.get('today_count')
        if result == "cap_reached" or (today_count is not None and today_count >= DAILY_CAP):
            done[email] = 'cap_reached'
            continue
        if result == "stepped" and (info.get('paid') or info.get('topup_ok') or 生成了新账单):
            no_progress[email] = 0
            progressed_any = True
            recharge_success_total += ...    # 记账口径见下
        else:   # failed 或无进展
            no_progress[email] = no_progress.get(email, 0) + 1
            if no_progress[email] >= MAX_NOPROG:
                done[email] = 'abandoned'
    if not progressed_any and not stop_requested:
        break   # 全轮无进展兜底
```

- 「有进展」判定：本次 `today_count_after > 上次记录的 today_count`（生成了新账单），
  或 `info.paid >= 1`（付成了一张）。为此需在编排层缓存每账号上轮 `today_count`。
  简化：`recharge_account` 直接在 `info` 里给 `generated: bool`（本次 Top-up 是否使 today_count 增长）
  和 `paid: int`，编排层据此判进展，避免自己缓存。
- 统计口径：沿用现有 `recharge_success_total / recharge_fail_total`。轮询下建议新增
  `invoice_paid_total`（本次流水线付成的账单数）与 `topup_generated_total`（生成的账单数）用于末尾汇总，
  避免语义混淆（一个账号会被计多次）。

### 连续失败全局熔断
现状阶段2 有 `consecutive_failures>=3` 停整个阶段。轮询下「连续失败」改为**每账号**维度
（`no_progress`），不再用单一全局计数（否则不同账号交替失败会误停）。全局兜底交给
「全轮无进展 break」+ `MAX_ROUNDS`。

## 5. 配置
`src/config.py` 增（或模块级常量）：
- `INVOICE_DAILY_CAP = 30`
- `TOPUP_AMOUNT = 10`
`registration` / `app` 从此引用，便于日后调参。

## 6. 兼容性与回滚
- `invoice_daily_cap=None` / `single_step=False` 时全部旧路径零改动，单账号按钮不受影响。
- 回滚：阶段2 轮询循环可整体还原为原「顺序一遍」循环；driver 新函数/新参数为增量，删除即回退。

## 6.1 实现调整（与初版设计的差异）
- **返回契约**：为最小化对已验证的全量路径的改动，未把全部 return 改 6 元组。改为：
  全量模式（`invoice_daily_cap is None`）仍返回 5 元组；单步模式返回 6 元组
  `(success, err, responses, card_last4, outcome, info)`。`_recharge_one_account` 按 `single_step` 分支解包，
  统一对外返回 3 元组 `(result, err, info)`（全量模式 info={}）。
- **卡池耗尽**：`recharge_account` 单步 info 增 `cards_exhausted`（handle_unpaid_invoices 回 `status='skipped'`
  即判定）；编排层据此把该账号标 `done['no_cards']`，避免继续生成无法支付的账单。
- **无支付卡分组**：阶段2 仅在 `payment_group_id` 存在时走轮询；否则退回原「单遍全量」循环（每账号 Top-up 一次），
  不做 30 次空转生成。
- **进展判定**：`made_progress = generated OR paid>0`；auto-pay（bound 卡成功、无 open invoice）也算进展，
  持续推进到 cap。

## 7. 风险
- **翻页与时区**：invoice-history 分页 + `created` 时区换算错误会导致计数偏差（多充或早停）。
  需按本地日边界过滤并正确翻页（倒序提前终止）。
- **坏账单跨轮空转**：付不掉的 open invoice 每轮被重选，靠 `no_progress` 阈值 + 全轮无进展兜底收敛。
- **封控**：轮询虽分散单账号操作，但反复 login/Top-up 仍有风控风险；单账号每轮仅 1 生成 1 付、
  切换账号是既定缓解手段，必要时可在账号间加随机 sleep（后续可选）。
- **底料卡在轮询下的消耗**：`_get_card` 的 20 张封顶/复用逻辑在**单次** `recharge_account` 调用内重建，
  跨轮次不记忆已用卡序（每轮重新登录、重建闭包）。CF 侧真实约束以 CF 为准，本地选卡资格闸门
  （R1 一卡绑一账号 / R2 24h≤2 次 / R3 3DS 冷却）仍生效，跨轮不会把同一张卡在同一账号上超额使用
  （R2 由 `recharge_logs` 实时统计跨轮亦生效）。
