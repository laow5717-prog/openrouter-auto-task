# 执行计划 — 账单支付选卡规则

> 前置：改动前备份 `data/cloudflare_auto.db`。落笔改每处前真实读取该行确认（防注入/漂移）。

## 前置校验

- [ ] P1：真实读取确认锚点：`registration._get_card`/`_on_invoice_paid`/`_on_invoice_failed`；`driver.handle_unpaid_invoices` 中 `on_failed` 调用点与 `_fill_stripe_payment_and_submit` 的 3DS 返回（driver.py:2139-2143）；`valid_card.record`/`get_all`；`recharge_log` 字段；`database.py` 建表处。
- [ ] P2：确认 `_on_invoice_paid` 写入 recharge_logs 的 `card_display` 是完整卡号（R2 统计依赖）。
- [ ] P3：确认前端 valid_cards 的展示位置（`CardPool.vue` 内嵌 or 需新页）。

## 实现步骤（按依赖顺序）

- [ ] S1 — `database.py`：建表 `card_payment_state`（见 design DDL），随启动幂等创建。
- [ ] S2 — 新增 `src/models/card_payment_state.py` `CardPaymentStateModel`：`set_tds(card_number, until_dt_str, reason)`、`get_tds_until(card_number)`、`in_tds_cooldown(card_number)`（now<tds_until）、`get_map()`（批量取，供选卡/前端）。注册进 `create_app` 的 models（键 `card_state`）。
- [ ] S3 — `recharge_log.py`：新增 `success_count_since(card_number, hours=24)`（`created_at >= datetime('now','localtime','-{h} hours')`）、`last_success_at(card_number)`。
- [ ] S4 — `valid_card.py`：新增 `get_bound_email(card_number)`（`source_type='payment'` 的 source_email，无则 ''）；`get_all` 结果或新增查询用于导出。
- [ ] S5 — `registration.recharge_account`：构建 `_eligible(num)`（R1+R2+R3，用 card_binding_model 之外的 `valid_card_model`/`recharge_log_model`/`card_state_model`）；在选卡前过滤 `payment_cards`，被排除的打印原因（去重）。给 `recharge_account` 传入 `card_state_model`（新参数，app 层注入 `models['card_state']`）。
- [ ] S6 — `_on_invoice_failed` 增 `tds` 语义：3DS 且该卡曾成功 → `card_state.set_tds(num, now+24h, reason)` 且**不** `mark_invalid`；否则维持现状。
- [ ] S7 — `driver.py`：`_fill_stripe_payment_and_submit` 3DS 返回值加 `tds=True`；`handle_unpaid_invoices` 把 `tds` 透传到 `on_failed(...)` 回调签名。
- [ ] S8 — `app._recharge_one_account` / `run_daily_pipeline`：给 `registration.recharge_account` 传 `card_state_model=models['card_state']`（与 S5 对齐）。
- [ ] S9 — `routes.py`：`/api/valid-cards` 响应补 `bound_email`/`tds`/`rate_cooldown` 状态；新增 `GET /api/valid-cards/export`（openpyxl 生成 xlsx，列=模板13列+`bound_email`+`status`，`send_file` 下载）。
- [ ] S10 — 前端：valid_cards 展示处加"导出"按钮（下载 export 接口）+ 状态列（绑定账号/冷却/3DS）。`cd frontend && npm run build`。

## 验证 / 门槛

- [ ] V1 — 导入/语法：`.venv/bin/python3 -c "import src.web.app, src.services.registration, src.browser.driver, src.models.card_payment_state"`。
- [ ] V2 — 单元/构造数据静态验证（不跑浏览器）：
  - 造 recharge_logs：卡 X 在账号 A 有 1 条 success → `_eligible(X)` 对 B=False（R1/AC1,AC2）、对 A=True。
  - 卡 Y 在 A 24h 内 2 条 success → `_eligible(Y)` 对 A=False（R2/AC3）；把时间改到 24h 前 → True。
  - `card_state.set_tds(Z, now+24h)` → `_eligible(Z)`=False；设为过去 → True（R3/AC4）。
- [ ] V3 — 导出接口：`curl /api/valid-cards/export -o out.xlsx` 能下载且 openpyxl 可读、列正确（AC6）。
- [ ] V4 — 前端构建通过；页面出现导出按钮与状态列。
- [ ] V5 — 实跑（真实环境，浏览器）：一轮账单支付，观察：跨账号已绑卡被跳过（AC1/AC2）、达次数上限跳过（AC3）、3DS 卡标临时未永久作废（AC4）、真正可用卡正常支付（AC7）。

## 审查门 / 回滚点

- 审查门：S1-S9 完成、V5 前，回读全 diff，确认仅触及 registration/driver/routes/models/database/前端，无越界、无可疑外部调用。
- 回滚点：新表可留（无害）；选卡闸门、3DS 临时、导出各自独立可回退。DB 靠备份回滚。

## 注意事项

- 前端改动需 `npm run build`（memory 约定）。
- 时区：24h 窗口查询统一 `datetime('now','localtime',...)`。
- 不改 Top-up 选卡、不改从未成功卡的首次 3DS 处理（Out of Scope）。
