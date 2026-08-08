# 执行计划 — 支付成功卡优先复用

基线：`python3 -m pytest tests/ -q` → 597 passed（改动前）。

## 1. 连充循环粘卡（`src/services/registration.py`）

现结构：`for idx, card in enumerate(cards)` 内 acquire → 冷却复查 → `top_up` → 成功则 `continue`。

改法：在 `try` 块内、`attempts += 1` 之前套一层 `while True`，把「一次支付」变成「这张卡上的
连续多次支付」：

- 成功且未触发 `balance_cap` → `continue`（内层），仍是这张卡；
- 成功且触发 `cap` → `stop_note` 后 `break` 外层（沿用现逻辑，需能穿透两层）；
- 任何失败分支 → 走完既有处理后 `break` 内层 → `finally` 释放该卡 → 外层换下一张；
- `needs_captcha` → 停手，穿透两层。

两层 break 的穿透用一个 `stop_all` 布尔标志在 `finally` 之后判定，避免 `for/else` 之类
不易读的写法。`should_stop()` 与 `attempts >= max_attempts` 的检查要在**内层**每笔前也做一次，
否则粘卡会绕过试卡上限。

## 2. 选卡排序（`src/web/app.py::_eligible_cards`）

`cards = fresh + good` → `cards = good + fresh`，同步改函数 docstring（当前写的是「新卡优先」）。

## 3. 测试

改动使「一次 success 之后适配器桩会被再次调用」，以下既有用例需按新行为调整：

- `tests/test_recharge_loop.py`
  - 只想验证「恰好一笔」的单卡用例：给桩加 `max_card_attempts=1`
    （`test_a_success_resets_the_failure_streak` 的成功那次、
    `test_success_does_not_put_the_card_into_cooldown`、`test_amount_is_recorded_in_the_log`、
    `test_amount_reaches_the_adapter`、`test_first_charge_*`、`test_reload_charge_*`、
    `test_adapter_that_reports_no_amount_*`、`test_final_balance_*`、
    `test_unreadable_final_balance_*`、`test_default_config_is_used_when_none_is_passed`）。
  - `test_success_does_not_stop_the_loop` / `test_running_out_of_cards_ends_the_loop`：
    按粘卡语义重写断言。
- `tests/test_card_concurrency.py::test_no_card_is_charged_twice_by_the_same_session`：
  该用例断言的正是被本次改动推翻的旧不变量（「按 idx 前进，不回头」），改写为
  「同一会话同一时刻只持一张卡 + 卡与卡之间不回头」。

新增用例：

- 粘卡：一张卡连成 N 笔，`responses` 的 `card_last4` 全同；
- 失败才换卡：`['success','failed','success']` 下第 3 笔落到第二张卡；
- 粘卡不绕过 `max_card_attempts`；
- `_eligible_cards` 好卡排在新卡之前。

## 4. 验证

- `python3 -m pytest tests/ -q` 全绿。
