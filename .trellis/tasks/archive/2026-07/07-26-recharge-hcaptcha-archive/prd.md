# 充值 hCaptcha 自动解 + 余额≥$20 归档 + 修复误标 failed 账号

## Goal

让每日充值流程（含单账号充值）在 Stripe 支付环节遇到 hCaptcha 时能像订阅流程一样用
multibot 自动解题；充值前判断账号余额，已达 $20 的账号跳过充值并归档；修正账号列表里被误标
`failed`（实际可用）的账号。

## 背景 / 现状

- 充值编排 `services/registration.recharge_account` → `browser/opencode_billing`。
- 订阅编排 `web/app._subscribe_one_account` → `browser/opencode_subscribe` **已打通** hCaptcha
  自动解：靠三件套 ①`create_driver_vanilla`（原生 Playwright，注入才生效）②`captcha.init_solver(key, server="api.multibot.cloud")` ③导航前 `captcha.install_hcaptcha_hook`，再在
  `detect_subscribe_result` 里调 `captcha.solve_hcaptcha`（最多 3 次）。
- 充值流程这三件套**全缺**：用的是 `create_driver`(Patchright，注入被阉割)、无 `init_solver`、
  无 `install_hcaptcha_hook`；`opencode_billing.detect_payment_result` 检测到 hCaptcha 后只提示
  「请人工点 Verify」并干等，超时判 `needs_captcha`。
- `create_driver_vanilla` 与 `create_driver` **复用同一 profile 目录** `data/profiles/<email>`
  （driver.py:783 注释确认"复用已登录态"），故充值切 vanilla 不会丢登录态。
- `credits_balance`（美元）字段已存在于 accounts；`AccountModel` 有 `update_status` /
  `update_balance`；status 是自由字符串，**当前无 `archived` 值**。
- `failed` 状态只在 `app.py:680`（注册失败）写入；充值流程从不写 `failed`。列表里的"失败"是
  历史脏数据。
- 充值接口 `routes.py` daily pipeline 已接收 `captcha_api_key`，缺 `captcha_server` 与向下透传；
  单账号充值接口 `/api/accounts/recharge` 也走 `_recharge_one_account`。

## Requirements

### R1 — 充值支付环节 hCaptcha 自动解（照订阅流程）
- 充值流程改用 `create_driver_vanilla`（复用同 profile，登录态不丢）。
- 传入 `captcha_api_key` 时 `init_solver(key, server=captcha_server)`；默认 server=`api.multibot.cloud`。
- solver 可用时，导航/登录前 `install_hcaptcha_hook(session)`。
- `detect_payment_result` 检测到需交互 hCaptcha 且未进入 3DS 阶段时，调 `solve_hcaptcha` 最多 3 次；
  3 次仍未过返回 `needs_captcha`；solver 不可用才回退「等人工」旧行为。
- `captcha_api_key` + `captcha_server` 从 routes → app → registration 透传（daily 管线与单账号充值）。
- **不改动订阅流程的任何行为**。

### R2 — 余额 ≥ $20 跳过充值并归档
- 充值前（登录拿到 wid 后、试卡前）读**实时余额**（权威，DB 余额会随消耗过时）。
- 实时余额 ≥ $20：不试任何卡、不扣款；把账号 `status` 置为 `archived`，写回 `update_balance`；
  返回新 outcome `archived`。
- `status == 'archived'` 的账号从后续充值轮转与启动门中排除。
- 阈值默认 $20（常量，可留 env 覆盖口）。

### R3 — 修正误标 `failed` 账号
- 一次性把 accounts 表中 `status = 'failed'` 批量改为 `registered`。
- 提供可重复执行的脚本（对齐 `scripts/fix_valid_cards_status.py` 风格），并在本任务执行一次。
- 修正后账号列表不再显示错误的"失败"，充值轮转正常纳入这些账号。

## Acceptance Criteria

- [ ] AC1：充值流程使用 `create_driver_vanilla` 且登录态复用成功（能拿到 wid）。
- [ ] AC2：充值支付页弹 hCaptcha 时，`detect_payment_result` 调 multibot `solve_hcaptcha` 自动解
      （最多 3 次）；日志出现 `[multibot]` 提交/轮询与 token 注入；3 次失败才 `needs_captcha`。
- [ ] AC3：solver 不可用（未配 key）时，充值行为与改造前一致（提示人工、超时 needs_captcha），不报错。
- [ ] AC4：登录后实时余额 ≥ $20 的账号被置为 `archived`，本次不扣款；`archived` 账号在后续轮转/
      启动门中被排除。
- [ ] AC5：余额 < $20 的账号照常进入试卡充值。
- [ ] AC6：执行修复脚本后，DB 中原 `status='failed'` 全部变为 `registered`，脚本打印修改条数。
- [ ] AC7：`captcha_api_key`/`captcha_server` 在 daily 充值接口与单账号充值接口均可传入并生效。
- [ ] AC8：订阅流程（`_subscribe_one_account` / `opencode_subscribe`）代码与行为不受影响。
- [ ] AC9：现有测试（如 `tests/test_daily_pipeline.py`）不因新增参数破坏（新增参数带默认值，向后兼容）。

## 非目标 / Out of Scope

- 不自动发起真实充值跑批（用户手动在界面触发；本任务只交付实现与自测）。
- 不改订阅流程；不改卡池选卡/记账规则；不动 3DS 判定逻辑（仅在其"未进 3DS"分支内接入解题）。
- 不做基于 DB 陈旧余额的预跳过（避免误归档）；归档只以登录后实时余额为准，或已有 `archived` 状态。

## Notes

- 真实跑批按 $20/张真实扣款，由用户手动触发（用户第 3 问选择"先实现"）。
- multibot 直连细节见 `services/captcha._multibot_hcaptcha`（参数名 isInvisible/enterprise/data）。
