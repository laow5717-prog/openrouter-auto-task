# 技术设计 —— GitHub 自动注册（前半段）

## 1. 边界与模块划分

新增两个隔离模块，不碰 `registration.py`（opencode 存根）与 driver.py 的 LEGACY Cloudflare 方法：

- `src/browser/github_signup.py` —— **GitHub signup 页面操作层**。承载所有 GitHub 专属选择器与
  分步填表逻辑。仅从 `driver.py` import 通用 helper（`create_driver`、`_safe_goto`、`_safe_fill`、
  `_safe_click`、`_wait_visible`、`_wait_gone`、`close_driver`）。**不**引入任何 Cloudflare/opencode 语义。
- `src/services/github_signup_service.py` —— **编排层**。串起「建 mail.tm 邮箱 → 起浏览器 →
  调用页面层填表提交 → 判定终态」，返回结构化结果。

> 选择新建模块而非扩展 driver.py：driver.py 已 260KB 且大量方法标注 `LEGACY Cloudflare-specific`，
> GitHub 与该站点无关，混入会加重后续 opencode 改造的耦合面。

## 2. 关键契约

### 页面层 `github_signup.py`

```
GITHUB_SIGNUP_URL = "https://github.com/signup?source=form-home-signup&user_email="

def open_signup(session) -> None
    # _safe_goto 到 signup 页，等待表单首字段可见

def fill_signup_form(session, email, password, username) -> dict
    # 按 GitHub 逐字段揭示式表单顺序填写：email → password → username →
    #   （若出现）产品更新订阅选项 → 逐步 Continue，直到提交按钮/验证码出现。
    # 返回 {"stage": <到达的阶段>, "rejected": bool, "reject_reason": str|None, "captcha": bool}

def detect_terminal_state(session) -> dict
    # 判定当前页面终态：captcha_present / field_rejected / unknown
```

> 具体选择器（input 的 id/name、错误提示节点、Continue 按钮、Arkose iframe 标识）**留空占位**，
> 在实现阶段第 0 步「侦察」实跑页面后填入，禁止臆造。

### 编排层 `github_signup_service.py`

```
def signup_one(headless=False) -> dict
    """
    返回契约:
    {
      "ok": bool,                 # 是否成功推进到验证码（= 流程按预期跑通）
      "email": str|None,
      "email_password": str|None, # mail.tm 登录密码，便于后续收验证邮件
      "github_password": str|None,
      "username": str|None,
      "outcome": "reached_captcha" | "rejected_by_github" | "error",
      "reason": str,              # 人类可读说明
      "screenshot": str|None,     # 终态截图路径
      "final_url": str|None,
    }
    """
```

`outcome` 三态可区分「跑通到验证码（成功）/ 被 GitHub 拒邮箱（外部限制）/ 脚本异常」，对应验收标准。

## 3. 数据流

```
signup_one()
  ├─ email.create_temp_email()            → (address, mail_pw, mail_token)
  ├─ driver.create_driver(headless)       → BrowserSession
  ├─ github_signup.open_signup(session)
  ├─ github_signup.fill_signup_form(...)  → 逐字段填 + Continue
  ├─ github_signup.detect_terminal_state()→ captcha / rejected / unknown
  ├─ session.get_screenshot_as_png()      → 落盘 data/screenshots/
  └─ driver.close_driver(session)（finally 保证关闭）
```

用户名生成：复用项目已有 `faker`（pyproject 已依赖）或 `src/utils.py` 若有随机名工具；GitHub 用户名
需符合规则（字母数字与连字符、不以连字符开头/结尾、≤39 字符），生成后本地校验一次。

GitHub 密码：随机强密码（≥15 字符含大小写数字，满足 GitHub 强度要求）。

## 4. 侦察先行（实现第 0 步，强约束）

GitHub signup 是 React 逐字段揭示表单且可能 A/B，选择器不可假设。实现第一步必须：
1. 实跑 `create_driver(headless=False)` + `_safe_goto` 打开 signup 页；
2. dump 首屏与逐步揭示后的关键 DOM（input 属性、按钮、错误节点、Arkose iframe），
   落到 `research/github-signup-dom.md`；
3. 据此填 `github_signup.py` 的选择器常量。

## 5. 失败与终态处理

- **邮箱被拒**：email input 失焦后 GitHub 会异步校验并渲染错误提示。填 email 后须等待并检查错误节点，
  命中即 `outcome=rejected_by_github`，不再继续填后续字段。
- **验证码出现**：检测 Arkose FunCaptcha 容器/iframe 出现即视为到达终态 `reached_captcha`，
  截图后停止（本轮不解）。
- **超时/DOM 不符**：`outcome=error`，落截图与当前 URL 便于排查，不静默吞。
- **浏览器清理**：`close_driver` 放 finally，异常路径也不泄漏 Chrome 进程（沿用 driver.py 既有看门狗）。

## 6. 兼容性 / 回滚

- 纯新增文件，零改动既有编排（app.py / routes.py / registration.py 不动），对现有功能零风险。
- 回滚 = 删除两个新模块与侦察产物，无迁移、无数据结构变更。
- 暂不接入 WorkerManager / routes；本轮以独立脚本入口（如 `scripts/run_github_signup.py` 或
  `python -m` 入口）驱动，验证流程后再决定是否纳入批量调度（后续任务）。
