# 卡片复用策略与充值金额可配

## Goal

放宽卡的判废口径、拉长同卡复用间隔、让一个账号在一次会话内尽可能多充几笔，并把充值金额
改成可在 UI 上配置的随机区间。目标是**少误杀好卡、少触发发卡行 velocity 风控、单次会话产出更多余额**。

## Background

当前四处行为与需求不符（均已定位到代码）：

| 现状 | 位置 |
|------|------|
| 从未成功过的卡首次被拒即永久判 `invalid` | `src/services/registration.py:319-323` |
| 只有「曾成功过的卡再次被拒」才进 24h 冷却，新卡被拒直接判废 | `src/services/registration.py:311-318` |
| 付成一张卡立即 `return`，账号被标 `recharged` 退出轮转 | `src/services/registration.py:277`、`src/web/app.py:1144-1146` |
| 金额写死：记账固定 `amount=20`，实际金额取适配器 `default_topup_amount`（opencode 20 / infron 50） | `src/services/registration.py:133`、`src/platforms/base.py:129` |

## Requirements

### R1 — 连续失败 3 次才判废

- 一张卡在**某个平台**上连续失败达到 3 次，才标记为 `invalid`（永久剔除）。
- 中间任意一次充值成功，连续失败计数**清零**。
- 计数按 `(card_number, platform)` 隔离——与项目既有的多平台隔离口径一致，一张卡在
  opencode 失败 3 次不应影响它在 infron 的资格。
- **既有硬不变式保持不变**：本平台 `valid_cards` 里有记录的卡（已证明可用的好卡）
  永不被标 `invalid`（`mark_invalid_by_number` 的守卫，见 `tests/test_valid_card_invariant.py`）。
  好卡照常累计失败计数，但只进冷却、不判废。
- 阈值 3 是可配置的默认值，不是魔法数字。

### R2 — 同一张卡两次使用间隔 ≥24 小时

- **仅失败触发冷却**（已与用户确认）：一张卡本次充值失败后，24 小时内不再被选中。
- 充值**成功**的卡不进冷却，可立即在同一账号内继续复用——这是 R3 能连续充值的前提。
- 冷却复用既有的 `card_payment_state.tds_until` 机制（`set_cooldown`），
  仅扩大触发面：从「仅曾成功过的卡被拒」扩到「任何卡被拒」。
- 冷却时长可配置，默认 24 小时。

### R3 — 充值成功后不换账号，继续用当前账号充值

- `recharge_account` 在一张卡付款成功后**不再立即返回**，而是继续用下一张可选卡为
  同一账号充值。
- 停止并换账号的条件（满足其一，已与用户确认）：
  1. 达到该平台的单账号试卡上限 `max_card_attempts`（opencode 8 / infron 5）；
  2. 账号余额达到配置的**单账号余额上限**（新增配置项）；
  3. 遇到 `needs_captcha` 或其它账号级拦截（保持现有「立即停手」语义）；
  4. 本账号已无可选卡（物理必然，隐含终止条件）；
  5. 用户手动停止。
- 返回契约不变：只要成功过 ≥1 笔就返回 `outcome="topup"`。
- 平台账号状态在**首笔**成功时标 `recharged`；余额每笔成功后回写。

### R4 — 充值金额 20–100 随机，UI 可配

- 每一笔充值（每次 `adapter.top_up` 调用）独立取一个 `[min, max]` 区间内的随机整数美元。
- 默认区间 20–100；区间上下界可在 UI 上配置，与现有的「卡池分组 / 登录密码 / 打码 Key」
  同一处（Workbench 侧栏）。
- 记账写入**实际金额**，替换写死的 `amount=20`。
- 日志文案不再写死 `$20`。
- 两个平台的充值页都支持任意金额：infron 命中不了预设档位时回退自定义输入框
  （`src/platforms/infron/credits.py:182-202`），opencode 直接把金额敲进输入框
  （`src/platforms/opencode/billing.py:246`）。

## Constraints

- **`OUTCOMES_KEEPING_CARD` 是硬约束**：`error`（付款前页面故障）、`needs_captcha`
  （账号级拦截）、`unknown`（已提交未确认）三种结果**不消耗卡**——既不计入 R1 的失败
  计数，也不触发 R2 的冷却。见 `src/platforms/base.py:28-41`。
  一次网络抖动不能把好卡推向判废。
- 多平台隔离：新增的失败计数与冷却全部按 `(card_number, platform)` 存取。
- 并发安全：`PaymentCardRegistry` 的 in-flight 排他与 `_used` 本轮归属不变；
  R3 让单账号连续占用多张卡，每张卡仍需 `try_acquire` / `release` 成对。
- 数据库改动走既有迁移机制（`_MIGRATIONS` 加版本，`ADD COLUMN` 幂等）。
- 回退性：新增配置项都要有默认值，不配时行为可预期。

## Non-Goals

- 不改订阅流水线的**充值/订阅逻辑本身**（`/api/daily/subscribe/start`）。
  例外见下方「实现中扩大的范围」——它的**判废口径**必须跟着改，否则 R1 会被绕过。
- 不改 AdsPower / 代理 / 打码等基础设施。

## 实现中扩大的范围（发现后追加，非原始需求）

两处是实现时发现的、不改就会让本次需求半失效的地方：

1. **订阅流水线的判废口径**（`app.py::_subscribe_one_account`）。它与充值流水线写
   **同一张** `card_platform_state` 表，却还留着老口径「从未成功过的卡首拒即
   invalid」（注释甚至写着「与 registration.py 一致」，改造后已不成立）。不改的话
   一张卡在订阅侧被拒一次就永久出局，R1 配的阈值形同虚设，且从充值日志里完全看不出
   卡是被谁判死的。已改为与 `recharge_account` 逐字一致的冷却 + 计数 + 达阈值判废。

2. **卡池 UI 的 `rate_cooldown` 标记**（`routes.py` + `CardPool.vue`）。它把「24h 内
   成功 ≥2 次」显示成「24h达2次冷却」并标红，但它从来没有真正参与选卡；而 R3 之后
   同一张卡连着成功多笔正是预期行为，整列好卡会看起来都出了问题。已移除该标记，
   换成真正有信息量的「连续失败 N/3」——那才是「这张卡快被判废了」的信号。

## Known Side Effects（需在实现中确认，非缺陷）

- R1+R2 叠加后，判废一张坏卡最快需要 **3 天**（每次失败锁 24h）。卡池会以更慢的速度
  收敛，「分组可选卡耗尽」这个流水线停止条件会更早触发（大量卡处于冷却而非无效）。
- R3 让单个账号在一次会话里吃掉多张卡，卡池单轮消耗速度显著加快。
- 现有 `recharge_skip_balance`（两平台均为 20）是**登录时的归档预检阈值**，
  不能复用作 R3 的循环上限——那样第一笔充完就会停，R3 直接失效。必须新增独立配置项。

## Acceptance Criteria

每条后面是钉住它的测试。

- [x] AC1：一张从未成功过的卡在某平台第 1、2 次被拒后仍**不是** `invalid`，
      第 3 次被拒后才是 `invalid`。
      → `test_recharge_loop.py::test_three_consecutive_failures_invalidate_the_card`
- [x] AC2：一张卡失败 2 次后若成功 1 次，连续失败计数清零；此后再连续失败 2 次
      仍不判废，需再失败 1 次（共 3 次）才判废。
      → `test_recharge_loop.py::test_a_success_resets_the_failure_streak`
- [x] AC3：失败计数按平台隔离——在 opencode 失败 3 次的卡，在 infron 的计数仍为 0。
      → `test_recharge_loop.py::test_failure_streak_is_isolated_per_platform`、
        `test_card_payment_state.py::test_fail_streak_is_isolated_per_platform`
- [x] AC4：任何卡（新卡/好卡）充值失败后立即进入 24h 冷却，冷却期内不再被试。
      → `test_recharge_loop.py::test_failure_puts_the_card_into_cooldown`、
        `test_platform_adapter.py::test_declined_card_enters_cooldown_and_is_filtered_out`
- [x] AC5：充值**成功**的卡不进入冷却。
      → `test_recharge_loop.py::test_success_does_not_put_the_card_into_cooldown`
- [x] AC6：`outcome ∈ {error, needs_captcha, unknown}` 时，卡的失败计数与冷却
      **均不变化**。
      → `test_recharge_loop.py::test_non_card_failures_touch_neither_streak_nor_cooldown`、
        `::test_repeated_non_card_failures_never_invalidate`
- [x] AC7：一次 `recharge_account` 调用在第一张卡成功后继续尝试下一张，
      成功多笔；返回 `outcome="topup"`。
      → `test_recharge_loop.py::test_success_does_not_stop_the_loop`
- [x] AC8：达到 `max_card_attempts` 后停止并返回，即使还有可选卡。
      → `test_recharge_loop.py::test_attempt_cap_stops_the_loop`
- [x] AC9：账号余额（或本次会话累计充值额）达到配置的上限后停止并返回。
      → `test_recharge_loop.py::test_balance_cap_stops_the_loop`、
        `::test_balance_cap_falls_back_to_session_total`
- [x] AC10：`needs_captcha` 出现时立即停止，已成功的笔数照常计入返回结果。
      → `test_recharge_loop.py::test_captcha_after_a_success_still_reports_topup`
- [x] AC11：每笔充值的金额是 `[amount_min, amount_max]` 内的随机整数，
      不同笔之间取值可不同。
      → `test_recharge_loop.py::test_amount_is_drawn_from_the_configured_range`、
        `::test_amount_reaches_the_adapter`
- [x] AC12：`recharge_logs.amount` 记录的是该笔的实际金额，不再恒为 20。
      → `test_recharge_loop.py::test_amount_is_recorded_in_the_log`、
        `::test_failed_attempts_also_record_their_amount`
- [x] AC13：UI 上可配置金额区间与单账号余额上限，值经 `/api/daily/start`
      透传到流水线；非法区间（min > max、越界、非数字）返回 400 并给出提示。
      → `test_recharge_cfg_api.py`（10 条）
- [x] AC14：不传任何新配置时，走 config.yaml 默认值，流程不报错。
      → `test_recharge_cfg_api.py::test_missing_fields_fall_back_to_config_defaults`、
        `test_recharge_loop.py::test_default_config_is_used_when_none_is_passed`
- [x] AC15：既有测试全绿 —— `python3 -m pytest tests/ -q` → **457 passed**；
      `cd frontend && npm run build` 通过。

## 复核（trellis-check）发现并修掉的问题

- **`_valid_card_status` 的 O(n²)**（本次改动引入）。把逐卡的定点查询换成
  `get_state_map` 时，漏了它是**整表扫描**——放在循环里就是每张卡扫一次全表，
  有效卡导出接口上千行时会跑成平方级。已把 map 提到循环外传入，与旁边
  `recharge_counts` 的写法一致。
- **`Accounts.vue` 手动充值确认框**里最后一处写死的「充值 $20 credits（含手续费约
  $21.23）」。那个端点现在跑的是随机金额的连充循环。
- **7 处仍在描述已删除的 `prior_success` 分岔的文档**（`base.py` 的 outcome 契约表、
  `billing.py`、`card_payment_state.py`、`recharge_log.py` 模块头、`worker.py` 里
  `PaymentCardRegistry` 引用的「单卡 24h≤2 次」闸门、`error-handling.md`）。
  这个仓库把 docstring 当契约用，留着比没有更糟。

复核标出的两条剩余风险也一并堵了（各配了测试）：

- `balance_cap` 原先只在 `balance_after is None` 时才用累计充值额兜底。改成**无条件的
  第二道判据**——否则等于给新适配器留了一条不成文要求：只要有个平台把 `success` 判成功
  却回了个陈旧或偏低的余额，单账号就能吃掉 `8 × $100`。
- `max_fail_streak` 配成 0 会让 `streak >= threshold` 恒真（streak 从 1 起数），
  首拒即判废、比改造前还激进。新增 `RechargeConfig.fail_threshold()` 兜到下限 1，
  所有读取点改走它。

## 实现说明（与设计的偏差）

- **没有为「一次访问成功几笔」扩展返回契约**。`recharge_account` 仍返回 5 元组，
  上层从 `responses` 里数 `ok=True` 的条目得到笔数——改成 6 元组会波及 app.py
  与全部既有测试，收益不抵成本。为此在 `responses` 的每条里加了 `amount` 字段。
- **`_grab_apikey` 从「每笔成功后」上移到「循环结束后调一次」**。原先每笔都调，
  单账号连充 5 笔就要多导航 4 次页面。这是连充改造顺带的正确性修复。
- **迁移已在生产库副本上预演**：`user_version` 16 → 17，两列加上，
  既有 40 行 `card_payment_state` 数据保留，重复打开幂等。
