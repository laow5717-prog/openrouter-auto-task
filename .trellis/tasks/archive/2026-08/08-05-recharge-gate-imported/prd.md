# 充值任务启动门放行待注册 imported 账号

## Goal

让「账号列表里只有刚导入、尚未注册的邮箱」这个开局场景能正常启动每日充值任务：
由流水线自己完成 **注册 GitHub → 登录平台 → 充值**，而不是在 API 启动门就被
「无可充值账号」400 掉。

## Background

`POST /api/daily/start` 的启动门（`src/api/routes.py`）判定「有没有账号可用」时只数两类：

1. **可充值账号** —— 要求 `login_password` 非空且身份/平台状态非终态；
2. **可复用的已充值账号** —— 平台状态 `recharged` 且余额未达 `balance_cap`。

刚导入的账号是 `identity_status='imported'`，**GitHub 还没注册，因此天然没有
`login_password`**（那个密码是注册流程写回去的）。于是两类计数都是 0，接口直接返回：

> 无可充值账号（需有登录密码、身份与平台状态均非终态），也无余额未满的已充值账号可复用，无事可做

而流水线本身**早就支持补号**：`AppState.run_daily_pipeline` 的同一道门
（`src/web/app.py`，`if not accounts and not imported_pending and not reusable_pending`）
把待注册 imported 也算进去了，`_try_claim` 领不到可充账号时会领一个 imported 走
`_register_one_account`，注册成功者下一轮即以 registered 身份进入充值。

也就是说：能力已实现，只是被 API 启动门挡在门外，一次都跑不起来。

订阅端点 `POST /api/daily/subscribe/start` 没有这个缺陷——它的启动门不要求
`login_password`，所以 imported 账号能过。本任务不改它。

## Requirements

- R1 `POST /api/daily/start` 的启动门必须把**待注册 imported 账号**算作「有活可干」。
- R2 待注册的判据必须与流水线的 `run_daily_pipeline._registerable_imported()` 逐条一致：
  - `identity_status == 'imported'`；
  - `_hotmail_for_account(a)` 能取到收码数据（DB 的 `email_verify_link`，或 `hotmail.xlsx` 命中）。
  - 判据不一致会重新制造这个 bug 的镜像版本：门说有、流水线说没有，任务空跑一轮就收敛。
- R3 三类账号（可充值 / 待注册 / 可复用）**全为 0** 时才拒绝启动，错误文案要提到待注册这一类。
- R4 启动成功的响应体要回显待注册账号数，与既有的 `accounts` / `reusable_accounts` 并列，
  让用户在 UI 上看得见「这次是靠补号跑起来的」。
- R5 不改动任何账号数据、不改流水线内部逻辑、不改订阅端点。

## Non-Goals

- 不重置现有 22 个 `failed` 账号（用户明确要求本次只改代码）。
- 不调整 `_registerable_imported()` 的判据本身。
- 不动 `POST /api/daily/subscribe/start`。

## Acceptance Criteria

- [ ] AC1 账号列表**只有**一个 `imported` 且带 `email_verify_link` 的账号时，
      `POST /api/daily/start` 返回 200 并真的起了流水线线程。
- [ ] AC2 该场景下响应体含待注册账号计数（值为 1）。
- [ ] AC3 `imported` 但**无收码数据**的账号不计数：账号列表只有这种账号时仍返回 400
      （与流水线一致——它领不走这种账号）。
- [ ] AC4 三类全为 0 时仍返回 400，错误文案覆盖「无待注册账号」。
- [ ] AC5 既有 registered 账号的启动路径行为不变（现有测试全绿）。
- [ ] AC6 `pytest tests/` 全量通过。
