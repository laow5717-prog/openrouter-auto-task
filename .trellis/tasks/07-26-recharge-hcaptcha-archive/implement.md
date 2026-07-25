# 执行计划：充值 hCaptcha 自动解 + 余额≥$20 归档 + 修复 failed

批次顺序：先低风险数据修复(R3) → 再核心解题(R1) → 再归档(R2) → 透传收口 → 自测。
每批次后跑对应校验；R1/R2 涉及实钱路径，仅做静态/单测校验，真实跑批由用户手动触发。

## 批次 A — R3 修复误标 failed（低风险，先行）

- [ ] A1. 定位真实 DB：查 `config.yaml` 的 db 路径 + `data/` 下 `.db`（根 `openrouter_auto.db` 为 0 字节）。
- [ ] A2. `src/models/account.py` 增 `reset_failed_to_registered()`（UPDATE failed→registered，返回行数）。
- [ ] A3. 新增 `scripts/fix_failed_accounts_status.py`（对齐 `fix_valid_cards_status.py`：连库→统计→
      改→打印 before/after）。
- [ ] A4. 运行脚本一次，记录"修改 N 条"。
- 校验：`python3 scripts/fix_failed_accounts_status.py`；再查 `SELECT status,COUNT(*) FROM accounts GROUP BY status` 确认无 `failed`（或仅剩本次运行后新产生的）。
- 回滚点：仅数据变更，无代码耦合。

## 批次 B — R1 充值 hCaptcha 自动解

- [ ] B1. `opencode_billing.py`：加 `import captcha_solver`；新增 `read_current_balance(session, wid, monitor)`。
- [ ] B2. `opencode_billing.detect_payment_result`：在现有 hCaptcha 检测分支接入 `solve_hcaptcha`
      三分支逻辑（镜像 `detect_subscribe_result`）：
      - 新增局部 `captcha_tries = 0`。
      - captcha 分支加 `not saw_3ds` 守卫（3DS 优先，避免回头误解常驻 invisible hCaptcha）。
      - `is_available() and tries<3` → 解题；`tries>=3` 且无拒付 → 提前 `needs_captcha`；
        `not is_available()` → 保留旧「提示人工」（只提示一次）。
      - 保持 `_balance_grew` 每轮最先判（权威成功信号）不变。
- [ ] B3. `registration.recharge_account`：签名加 `captcha_api_key=None, captcha_server="api.multibot.cloud"`；
      import 换 `create_driver_vanilla`、`captcha_solver`；建 session 用 vanilla；
      建 session 后 `init_solver` + `install_hcaptcha_hook`。
- 校验：`python3 -c "import ast; ast.parse(open('src/browser/opencode_billing.py').read()); ast.parse(open('src/services/registration.py').read())"`；
      `python3 -c "import src.browser.opencode_billing, src.services.registration"`（导入无误）。
- 回滚点：B1-B3 三文件可整体 revert。

## 批次 C — R2 余额≥$20 归档

- [ ] C1. `registration.recharge_account`：加 `RECHARGE_SKIP_BALANCE`（env `OPENCODE_RECHARGE_SKIP_BALANCE` 默认 20）；
      `ensure_opencode_session` 拿 wid 后、试卡前调 `ob.read_current_balance`；≥阈值 → `update_status('archived')`
      + `update_balance` + `return (...,"archived")`。
- [ ] C2. `app._recharge_one_account`：加 `captcha_api_key/captcha_server` 参数并透传；返回三态
      `success|failed|archived`；outcome=`archived` 映射为 archived（不计 fail）。
- [ ] C3. `app.run_daily_pipeline`：加 `captcha_server` 参数；账号筛选排除 `archived`；
      透传 captcha 参数；`round_stats['archived']`；`progressed = paid>0 or after<remaining or archived>0`；收尾统计含 archived。
- 校验：`ast.parse` + import 三文件；跑 `tests/test_daily_pipeline.py`（新参数默认值向后兼容）。
- 回滚点：C1-C3 可整体 revert。

## 批次 D — 接口透传收口（routes）

- [ ] D1. daily 充值端点：加 `captcha_server`；启动门账号计数排除 `archived`；`args` 带 captcha_server。
- [ ] D2. `/api/accounts/recharge` 端点：读 captcha_api_key/captcha_server 透传 `_recharge_one_account`。
- 校验：`ast.parse` + `import src.api.routes`。

## 批次 E — 全量自测与审查

- [ ] E1. `.venv` 下跑：`python3 -m pytest tests/test_daily_pipeline.py tests/test_card_fault.py -q`
      （及其它 recharge/pipeline 相关用例）。
- [ ] E2. 静态检查：全部改动文件 `ast.parse` + 顶层 import；确认订阅文件（opencode_subscribe.py /
      app._subscribe_one_account）零改动（`git diff --stat` 核对）。
- [ ] E3. 跑 trellis-check（spec 合规/复用/一致性）。
- [ ] E4. 逐条对照 prd.md 的 AC1–AC9 自检并勾选。

## 验证命令速查

```bash
# 语法
python3 -c "import ast,sys; [ast.parse(open(f).read()) for f in sys.argv[1:]]" \
  src/browser/opencode_billing.py src/services/registration.py src/web/app.py \
  src/api/routes.py src/models/account.py scripts/fix_failed_accounts_status.py
# 导入
.venv/bin/python -c "import src.browser.opencode_billing, src.services.registration, src.web.app, src.api.routes"
# 单测
.venv/bin/python -m pytest tests/test_daily_pipeline.py -q
# 账号状态分布
.venv/bin/python -c "from src.models.database import ...; ..."   # A4 后核对无 failed
```

## 审查门 (review gates)

1. 批次 B 完成后：人读 `detect_payment_result` diff，确认 3DS 优先未被破坏、余额判定仍最先。
2. 批次 C 完成后：确认 `archived` 在 registration/app 两层都被显式分流，未落 else 当失败。
3. 批次 E：AC 全绿 + 订阅零改动 + 测试通过，方可提交（3.4 commit）。

## 不做 / 提醒

- 不自动发起真实充值跑批（用户手动触发）。
- 真实 hCaptcha 自动解的端到端验证需真实跑批时观察 `[multibot]` 日志，属用户触发后的验收，
  非本实现阶段自测范围。
