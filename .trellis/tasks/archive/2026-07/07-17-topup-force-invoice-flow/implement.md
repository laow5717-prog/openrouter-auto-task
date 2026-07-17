# 执行计划 — topup 提交后强制进入账单支付流程

## 变更清单（仅 1 个文件）

- [ ] Step 1 — 全量模式改早返回
  文件 [src/services/registration.py](../../../src/services/registration.py) 约 897-904 行
  把 `if not pay_success: return ... "failed"` 改为：`responses = responses or []` + 不返回，仅打日志，落到既有 `if not skip_invoice:` 账单处理分支。（见 design.md「全量模式」代码块）

- [ ] Step 2 — 单步模式改早返回
  文件同上，约 768-772 行
  删除 `if not pay_success: return (... "failed" ...)` 早返回，改为 `responses = responses or []` + 打日志，落到既有 `if not skip_invoice:` 分支，最终正常返回 6 元组 `"stepped"`。（见 design.md「单步模式」代码块）

## 验证命令 / 门槛

- [ ] 语法/导入自检：
  ```bash
  .venv/bin/python3 -c "import ast,sys; ast.parse(open('src/services/registration.py').read()); print('ast ok')"
  .venv/bin/python3 -c "import src.services.registration as r; print('import ok')"
  ```
- [ ] 静态确认两处早返回已消除：
  ```bash
  grep -n "填写金额或确认支付失败" src/services/registration.py   # 期望：0 处命中（两处均已删）
  grep -n "if not pay_success" src/services/registration.py        # 期望：无残留
  ```
- [ ] Review gate：人工核对控制流——`pay_success=False` 时能落入 `if not skip_invoice:` 且不会二次返回；`skip_invoice=True` 分支仍走 `_classify_topup` 后按 failed 收尾。

## 运行验证（真实流程，由用户实跑）

自动化涉及真实 CF 登录与扣款，无法在此环境端到端跑。交付后请用户实跑确认：
- [ ] 构造/遇到一次提交异常（或临时模拟 `fill_topup_and_confirm` 返回 False），确认日志出现"仍按要求继续账单支付流程"且浏览器跳到账单页执行 `handle_unpaid_invoices`。
- [ ] 一次被拒卡（decline）充值：确认仍进账单页（回归）。
- [ ] `skip_invoice`（未选支付卡分组）提交异常：确认按 failed 收尾、不进账单页。

## 回滚点

- 单文件、两处小改，`git diff src/services/registration.py` 可完整回看；异常时 `git checkout -- src/services/registration.py` 即回滚。

## 提交

- 前端无改动，无需 `npm run build`。
- 直接在 main 提交（见 git-workflow 偏好）。
