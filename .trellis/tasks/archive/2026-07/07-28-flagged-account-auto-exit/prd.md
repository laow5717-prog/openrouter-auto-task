# flagged 账号自动标记并退出每日轮转

## Goal

GitHub 被 flag(无法授权第三方 OAuth)的账号在每日充值任务中自动标记
`status='flagged'` 并退出轮转,不再每轮空开浏览器撞同一堵墙。
(背景:07-28 实测 fernandezr701 被 GitHub flag,旧逻辑每轮都会重开浏览器重试。)

## Requirements

- R1 `registration.recharge_account`:`ensure_opencode_session` 返回 flagged 原因时,
  标记账号 `status='flagged'`,返回新 outcome `"flagged"`(契约文档同步)。
- R2 每日充值管线(`run_daily_pipeline`):
  - 账号选取排除 `flagged`(与 banned/archived 并列);
  - 单轮结果 `flagged` 计入 done_emails 退出后续轮转,计入 progressed(账号集收敛);
  - 收尾统计输出 flagged 退出个数。
- R3 与订阅管线既有 flagged 语义保持一致(app.py `_subscribe_one_account` 已标 flagged、
  `run_daily_subscribe_pipeline` 已排除)。

## Acceptance Criteria

- [x] mock 单测:ensure_opencode_session 返回 flagged → recharge_account 返回
      outcome "flagged" 且 update_status(email,'flagged') 被调用。
- [x] 选取过滤:status='flagged' 的账号不进入每日充值账号集。
- [x] 语法/导入无回归。
