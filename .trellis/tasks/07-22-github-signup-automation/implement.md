# 执行计划 —— GitHub 自动注册（前半段）

## 前置

- 目标站点：`https://github.com/signup?source=form-home-signup&user_email=`
- 复用：`src/services/email.py`（mail.tm）、`src/browser/driver.py` 通用 helper。
- 新增：`src/browser/github_signup.py`、`src/services/github_signup_service.py`、独立运行入口。

## 有序清单

### 步骤 0 —— 侦察 GitHub signup DOM（阻塞后续，禁止跳过）
- [ ] 写一个临时侦察脚本：`create_driver(headless=False)` → `_safe_goto` 打开 signup 页。
- [ ] 观察并记录：email/password/username input 的定位属性、Continue/Create-account 按钮、
      邮箱校验错误提示节点、Arkose FunCaptcha 容器/iframe 标识、字段是否逐步揭示。
- [ ] 将 DOM 结论写入 `research/github-signup-dom.md`。
- 验证：research 文档能明确给出每个字段的可用选择器。

### 步骤 1 —— 页面操作层 `github_signup.py`
- [ ] 用步骤 0 的选择器实现 `open_signup` / `fill_signup_form` / `detect_terminal_state`。
- [ ] 逐字段揭示处理：填一个字段→等下一个字段出现→继续；email 填完后先查拒绝提示。
- [ ] 仅 import driver.py 的通用 helper，无 Cloudflare/opencode 依赖。
- 验证：`python -c` 单步调用 open_signup 能打开页面并定位到首字段。

### 步骤 2 —— 编排层 `github_signup_service.py`
- [ ] 实现 `signup_one(headless=False)`：建邮箱→起浏览器→填表→判终态→截图→关浏览器（finally）。
- [ ] 用户名/密码生成 + 本地规则校验（GitHub 用户名规则、密码强度）。
- [ ] 返回结构化结果（ok / outcome 三态 / email / 截图路径 / final_url）。
- 验证：函数返回契约与 design 一致。

### 步骤 3 —— 运行入口
- [ ] 新增 `scripts/run_github_signup.py`（或 `python -m` 入口）调用 `signup_one` 并打印结果。
- 验证：命令行单次运行能走完到验证码/拒绝并输出清晰结果。

### 步骤 4 —— 端到端实跑验证
- [ ] 实跑一次：确认能填入 mail.tm 邮箱并推进到 Arkose 验证码出现（或如实报告邮箱被拒）。
- [ ] 截图落 `data/screenshots/`，日志清晰。
- 验证：满足 prd.md 全部验收标准。

## 验证命令

```bash
# 侦察（步骤0）
python3 scripts/probe_github_signup.py        # 临时侦察脚本，产出 DOM 文档后可删

# 端到端
python3 scripts/run_github_signup.py
```

## Review Gate

- 步骤 0 完成后：确认选择器真实可用再进入步骤 1（避免基于臆造选择器返工）。
- 步骤 4 实跑前：确认 headless=False 有头运行，人工可旁观是否卡在预期终态。

## 回滚点

- 任一步骤失败：删除对应新增文件即可完全回滚（零改动既有编排，无数据结构变更）。
