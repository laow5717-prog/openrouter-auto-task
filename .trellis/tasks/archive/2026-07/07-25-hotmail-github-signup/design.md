# Design — 阶段二：hotmail 半自动注册 + 落库标记

## 目标与边界

改造让 `signup_one` 能用 xlsx 的真实 hotmail 邮箱 + ruoanzhu 收码跑半自动注册，
并新增批量编排入口，把账号数据导入 `accounts` 表、注册结果落状态。
**不改** mail.tm 老路径的行为（向后兼容）；**不解** Arkose（仍靠人工 `--semi-auto`）。

## 改造面

### 1. 收码解耦：`(token, since_ts)` → `fetch_code` 可调用

现状 `_collect_and_fill_code(session, token, since_ts, result)` 与
`_finish_semi_auto(session, token, since_ts, result)` 内部硬调
`wait_for_github_launch_code(token, since_ts)`（收两次：注册确认 + 新设备验证）。

改为接收一个**零参可调用** `fetch_code() -> str|None`：
- `_collect_and_fill_code(session, fetch_code, result)`
- `_finish_semi_auto(session, fetch_code, result)`

`signup_one` 按模式构造：
- mail.tm 模式：`fetch_code = lambda: wait_for_github_launch_code(token, since_ts)`
- hotmail 模式：`fetch_code = lambda: wait_for_github_launch_code_ruoanzhu(account.link)`

好处：两条收码路径都收敛到同一收尾逻辑，新增邮箱源只需换 `fetch_code`，收尾代码零改动。

### 2. `signup_one` 增参：`account`（可选 HotmailAccount）

```
signup_one(headless=False, semi_auto=False, keep_open=False, account=None)
```
- `account=None`（默认）：完全走原 mail.tm 流程，行为不变。
- `account` 提供时：
  - 跳过 `create_temp_email()`，用 `account.email` 作 GitHub 注册邮箱；
  - `result.email_password = account.password`（hotmail 密码，供落库）；
  - 浏览器用持久 profile：`create_driver(headless, profile_id=account.email)`——
    固定指纹环境，符合「真实账号+固定 profile 绕挂起」的既有经验；
  - `fetch_code` 走 ruoanzhu。
  - GitHub 用户名/密码仍随机生成（`login_password = github_pw`）。

返回结构不变，`email` 字段为 hotmail 地址。

### 3. 批量编排 + 落库：新脚本 `scripts/run_hotmail_github_signup.py`

DB 访问：`Database()` + `AccountModel(db)`（同 `scripts/test_opencode_recharge.py` 模式）。

子命令/开关：
- `--import`：只把 xlsx 各行 upsert 进 `accounts`（`email`, `email_password=hotmail 密码`,
  `status='imported'`），不起浏览器。对应用户「文件数据加入账号表」。
- `--index N` / `--email X` / `--all`：对指定/全部账号跑 `signup_one(semi_auto=True, headed, account=acc)`，
  逐个处理（半自动需人在场过码，`--all` 亦串行、每个都要人工过码）。
- 每次注册后按 outcome 落库（见状态映射）。

### 状态映射（outcome → accounts.status）

| signup_one.outcome | status | 落库动作 |
|---|---|---|
| signup_complete | `registered` | upsert(login_password=github_pw, email_password=hotmail_pw) + 成功标记 |
| account_suspended | `suspended` | upsert 同上（账号已建，被风控挂起） |
| rejected_by_github | `rejected` | update_status |
| captcha_timeout / no_verification_email / verification_failed / error | `failed` | update_status |
| reached_captcha / reached_verify_email（非半自动） | `pending` | update_status |

---

## 阶段四 — 充值按钮统一编排：注册或登录 → /go → Subscribe to Go 付款

### 用户决策
- Subscribe to Go 是**新增**付款路径，现有 billing 页 Add Balance/Enable Billing（充 credits）保留。
- **直接改 web 充值按钮**（`/api/accounts/recharge` → `_recharge_one_account` → `recharge_account`）。
- 付款卡**复用现有卡池分组**（payment_group），逐张尝试 + 逐卡记账（现有机制）。

### 实机探测结论（2026-07-25，carold030）
- `/go` 页付款入口按钮 = **`Subscribe to Go`**（另有 `Other payment methods`）。产品 = OpenCode Go 订阅，
  首月 $5（页面显示 CN¥35.22，50% off），之后 $10/月。
- 点 Subscribe to Go → **整页跳转 `checkout.stripe.com/c/pay/cs_live_...`**（live 真实扣款；非 iframe，同首充 mode="first"）。
- Stripe 结账页结构与现有首充高度一致：币种默认 CN¥ 有 CNY/USD 切换；支付方式 Card/Alipay；邮箱预填；
  有「I am an AI agent acting on behalf of someone else」勾选；**提交按钮文案是 `Subscribe`（不是 `Pay`）**。

### 复用 vs 新增（对 opencode_billing 现有函数）
| 步骤 | 现有函数 | 订阅流程 |
|---|---|---|
| 入口 | `start_recharge`（billing 页 Enable/Add） | **新增** `start_subscribe_go`：进 /go 点 Subscribe to Go，等 checkout.stripe.com 整页 |
| 选币种 USD | `pick_currency_usd` | ✅ 复用 |
| 选 Card | `select_card_method` | ✅ 复用 |
| 填卡+地址 | `fill_card_and_address` | ✅ 复用 |
| 电话/取消保存/AI 声明 | `fill_phone_if_present`/`uncheck_save_info`/`check_ai_agent_consent` | ✅ 复用 |
| 提交 | `click_pay`（只认 "Pay"） | **适配**：新增 `click_subscribe`（认 "Subscribe"）或给 click_pay 加文案 |
| 成功判定 | `detect_payment_result`（余额增长） | **新增** `detect_subscribe_result`：订阅不加余额→改判 Stripe 成功跳转/回落 /go 显示已订阅；拒付/3DS/hCaptcha 文本判定复用 |

### 编排层
- 新增 `subscribe_go_account(email, login_password, account_model, payment_cards, ...)`，镜像 `recharge_account`
  的逐卡尝试 + 记账 + 卡状态机，但：
  1. **前置分支**：查 accounts.status —— 未 `registered` 则先 `signup_one(account=hotmail_acc, semi_auto, then_opencode)`
     （需 hotmail 数据 + 可能人工过码）；已注册则 `create_driver(profile_id=email)` + `login_and_open_own_go`。
  2. 付款走 `subscribe_via_stripe`（上表新增/复用组合）。
- Web 层：`_recharge_one_account` 增加订阅模式分支（不删原 Add Balance 路径，additive）。

### 风险 / 门控
- **真实扣款**：Subscribe 走 cs_live，点下 Subscribe 即真付 $5。实现先做到"填好卡、停在提交前"，
  真实点 Subscribe 的 e2e 必须用户显式确认后再跑。
- 订阅成功判定无余额信号，需实机点一次真订阅才能标定成功 DOM/URL——留待用户确认后的付费实测。
- 与在途任务 `07-23-daily-recharge-rework` 可能有交叠，接入 web 层时注意不破坏其改动。

导入阶段先 upsert 建行（status='imported'），注册阶段再 update；成功时补写 `login_password`。

## 兼容性 / 回滚

- `account` 为新可选参数，老调用方（`scripts/run_github_signup.py`、web 层）零感知。
- `_collect_and_fill_code` / `_finish_semi_auto` 是模块内私有函数，签名变更只影响本文件。
- 回滚：还原 `github_signup_service.py`、删新脚本即可；DB 只新增行/改 status，无 schema 变更。

## 不做

- 不自动过 Arkose；不做并发（半自动人工串行）；不改 accounts 表 schema。

---

## 阶段四·续 — Stripe hCaptcha token 注入攻克（2026-07-25 进展）

**已完成且验证**：hotmail 注册→落库(carold030 registered)；opencode OAuth 登录→自己 /go；
Subscribe to Go→整页 checkout.stripe.com→USD(button:has-text)→选 Card→填卡→点
`button.SubmitButton[type='submit']`（需等 enabled）→真实提交；付款判定(拒付/3DS/成功启发式)；
2captcha 接线(captcha.init_solver + detect_subscribe_result 自动 solve_hcaptcha)。

**真正硬卡点**：Stripe 结账页每次都弹 enterprise hCaptcha（账号级风控）。2captcha 服务恢复后
**能解出 token**（长度 ~3800-4900），但 **`_inject_hcaptcha_token` 注入失败**——它在顶层文档找
`h-captcha-response`/`window.hcaptcha`，而 Stripe 的 hCaptcha 在跨域 iframe（js.stripe.com/v3/
hcaptcha-inner-*.html）里，顶层没有这些元素。sitekey 有两个：`ec637546…`(无 rqdata) 与
`c7faac4c…`(+rqdata 264，enterprise 真身)。

**攻克方向（用户已确认投入）**：token 注入要打进 hcaptcha iframe 帧 + 触发 Stripe postMessage 回调。
先诊断 frame 树定位 textarea/callback 位置，再改注入。真实付款始终未成功、未扣 $5。

**关键运行方式**：`TWOCAPTCHA_API_KEY=<key> python3 scripts/run_subscribe_once.py --email <e> --group 1 --max N`；
2captcha key 余额 ok（$9.96）；profile=email 复用登录态；每次跑前 pkill -f "<email>" 清残留 chrome。

### hCaptcha 注入攻克·第一轮结论（2026-07-25）
- frame 诊断定位：`h-captcha-response`/`g-recaptcha-response` textarea 在 frame
  `b.stripecdn.com/stripethirdparty-srv/assets/vXX/HCaptchaInvisible.html`（Stripe invisible
  enterprise hCaptcha 包装帧）；真正挑战在 newassets.hcaptcha.com 帧；顶层 checkout 帧无这些元素。
- 已把 captcha._inject_hcaptcha_token 改为**逐帧注入**：现能命中该帧，textareas 被成功 set（4 个）。
- **但仍 needs_captcha**：setResponse=0（该帧无 window.hcaptcha）、callbacks=0。证实 **Stripe invisible
  hCaptcha 不读 textarea**，只认 hcaptcha.execute() 的 JS 回调 → HCaptchaInvisible.html postMessage 给结账页。
  「塞 textarea」的标准 2captcha 投递方式对 Stripe invisible hCaptcha 无效。
- 唯一可行但复杂的路子：Playwright `add_init_script` 在 hcaptcha 帧加载前 hook `hcaptcha.render/execute`，
  捕获其 callback，等 2captcha 出 token 后主动调 callback(token) 交付。复杂、脆、不保证成功。
- 旁证：hCaptcha 每次必弹 = 账号/IP 级风控；即便过码也未必成功，carold030 可能已被重度标记。
- 真实付款始终未成功、未扣 $5。

### hCaptcha 注入攻克·第二轮改动（2026-07-25，captcha.py hook 补强）
针对第一轮 hook「实现了但仍 needs_captcha」的 4 个缺陷改造 `_HCAPTCHA_HOOK_JS` + 注入 JS：
1. **execute 全拦**：原只拦 `opts.async` 分支，非 async 调用落回原始 execute 跑真实挑战。现无论
   async 与否都拦成可控 Promise，不调用原始 execute（免弹真实挑战）。
2. **补 getResponse/getRespKey override**：callback 之后集成常调 `getResponse()` 复核 token，
   现返回注入的 `H.token`；`getRespKey` 返回记到的 widget id。
3. **修 resolve 形状**：enterprise `execute({async:true})` resolve `{response, key}` 中 `key` 应是
   widget/sitekey 而非 token（原塞成 token，疑被 Stripe 判伪）。现回填捕获到的 widgetId。
4. **晚到 execute 兜底**：注入设 `H.token` 后，此后才被调的 execute 立即用 H.token 自兑现。
5. **统一交付口 `H.deliverToken(token)`** + 富诊断 `hookDiag{sf,rc,ec,gr,cbs,rs,wids,keys}`。

**下轮验证要看的诊断**（跑 run_subscribe_once 时 `hCaptcha token 注入 frame ...` 那行的 hookDiag）：
- `ec>0`（Stripe 走了 execute）且 `rs>0`（resolver 被建）→ 我们的可控 Promise 生效路径。
- `rc>0` + `cbs>0` → 走 render callback 路径。
- 两者全 0 → Stripe 未经 JS API 交付，需另找 postMessage 直发路子。
- 若 hookDiag 有值但仍 needs_captcha → 大概率账号/IP 风控（token 有效但被判高风险），换新账号/换 IP 再试。
未实跑验证（真实扣款 $5，待用户确认后跑）。

### hCaptcha 注入攻克·第三轮：三次实跑 + 两个零成本探测的决定性结论（2026-07-25）
用 carold030 实跑 3 次（均 needs_captcha，**未扣款**，仅耗几次 2captcha 解题费）+ 两个诊断探测，钉死根因：

1. **add_init_script 在 Patchright 下完全失效**（probe_hcaptcha_frames）：hCaptcha 弹出时 dump 全 19 帧，
   **连顶层 checkout.stripe.com 帧都 hookInstalled=false**。Patchright 为反检测静默禁用了
   `addScriptToEvaluateOnNewDocument`（自动化检测重点特征）。→ 之前所有 add_init_script hook 是死的。
2. **改逐帧 `frame.evaluate` 注入后 hook 能装上**（19 帧），但拦截计数全 0：
   `hookDiag{sf:0,rc:0,ec:0,gr:0}`——Stripe 调 execute 时**没走我们包装的 window.hcaptcha**。
3. **零成本可变性探测（probe_hcaptcha_obj，未点 Subscribe，不扣款）给出终局判据**：
   **点 Subscribe 之前，没有任何帧存在 window.hcaptcha**。→ 承载 hcaptcha 的帧
   （b.stripecdn/HCaptchaInvisible.html?id=...，每次 id 不同）是**点 Subscribe 瞬间才创建**，
   Stripe 随即 load api.js → 定义 window.hcaptcha → 立刻 execute()，全在我们的点击后注入能跑之前。

**终局结论**：任何 post-hoc（evaluate/CDP 事后）hook 都拦不到——hcaptcha 帧在提交瞬间诞生并即刻使用。
唯一技术出路是**前置注入到 OOPIF 子目标创建之前**：CDP `Target.setAutoAttach(flatten,waitForDebuggerOnStart)`
→ 每个 attachedToTarget 里 `Page.addScriptToEvaluateOnNewDocument(hook)` + `Runtime.runIfWaitingForDebugger`。
风险：① 复杂事件驱动 CDP（sync API + 线程红线）；② 重新引入 Patchright 特意抹掉的可检测特征，
hCaptcha 可能硬拦或判 token 无效；③ 即便交付成功仍可能撞账号/IP 风控。高投入、不保证成功。
备选：hCaptcha 出现时**半自动人工点一下**（可靠、低投入，但每次付款需人在场），或放弃对此 Stripe
enterprise hCaptcha 的自动绕过。诊断脚本：probe_hcaptcha_frames.py（帧 hook 覆盖）、probe_hcaptcha_obj.py（对象可变性）。

### hCaptcha 注入攻克·第四轮：CDP 前置注入全部撞墙（2026-07-25，硬啃 CDP 结论）
按用户选择硬啃 CDP 前置注入，做了 4 个零成本探测，逐一排除，确认**注入这条路整体不可行**：
1. **add_init_script 本构建彻底失效**：纯 example.com 上 `context.add_init_script("window.__x=1")` 后
   `window.__x` 仍 MISSING（bypass_csp 也无效）。Patchright 用「路由把 init script 内联进 document 响应」
   实现，依赖它 patch 的 Node driver，此版本没生效 → 从一开始所有 add_init_script hook 都是死的。
2. **raw CDP addScriptToEvaluateOnNewDocument 经 Playwright CDPSession 被 Patchright 阉割**：
   `session._cdp().send(...)` 返回 identifier 假成功，导航后脚本不执行。
3. **独立 websocket 直连 Chrome 的 addScriptToEvaluateOnNewDocument 也不执行**：绕过 Playwright/Patchright、
   连浏览器级端点、保持连接、Page.enable 齐全，injected 到 23 个目标，脚本仍全不运行（连 example.com 主帧都 MISSING）。
   —— Chrome 对「Playwright 已作主调试器占用的目标」，二级连接的前置脚本注册不被兑现。
4. **浏览器级 setAutoAttach(waitForDebuggerOnStart) 抢不到暂停窗口**：新目标 attachedToTarget 全部
   `waiting=false`——Playwright 作主连接已瞬间 resume，二级连接永远慢一步。
5. **关站点隔离(--disable-site-isolation-trials) 也没让 hcaptcha 帧脱离 OOPIF**（帧数反增），主目标 addScript 覆盖不到。

**最终裁决**：本 Patchright + Playwright 主连接 + Stripe enterprise invisible hCaptcha（OOPIF 提交瞬间诞生并即用）
三者叠加，导致「在 hcaptcha 脚本前注入 hook 交付 2captcha token」在现有浏览器栈下不可实现。2captcha 能出 token，
但无处交付。可行路径只剩：(a) **半自动**：hCaptcha 弹出时人工点一下再自动续付款（可靠）；(b) 换浏览器栈——
付款步骤改用**原生 Playwright**（非 Patchright，add_init_script 正常，作主连接能前置注入 OOPIF），但更易被检测、
大改、不保证过风控。实验脚本：probe_cdp_inject*.py、probe_no_isolation.py、probe_bypass_csp.py；
失败的基建（cdp_inject.py、driver 的 bypass_csp/disable_site_isolation 参数）默认关闭、未接入生产。

### hCaptcha 注入攻克·第五轮：换全新账号验证——排除账号级风控（2026-07-25）
用户选「先换干净账号测一次」。用 hotmail 池里 leilao40@hotmail.com 跑 GitHub 半自动注册：
- **注册意外顺利、未弹 Arkose**：直接进邮箱验证页，ruoanzhu 自动收码(47167527)建号，登进 GitHub dashboard。
- opencode 首次 OAuth 登录**瞬态失败**（全新账号 workspace 未即时 provision，wid=None），**重试即成功**
  （wid=wrk_01KYBRYHJNRF2J1Y1CTZHJSFJ2，到 /go 见 Subscribe to Go）。→ login_and_open_own_go 对新号
  可加重试/加长 timeout（当前 90s 偏紧）。非 onboarding 障碍。
- **关键结论**：leilao40 全新账号点 Subscribe **一样弹 hCaptcha**（同 b.stripecdn/HCaptchaInvisible 帧 + widgetIds）。
  → hCaptcha **不是账号级风控**（carold030 被标记之说被推翻），是**环境级**触发：同出口 IP / 同一批支付卡 /
  Stripe 对「AI-agent 国际卡订阅」的默认风控。换账号不消除。

**综合裁决（第一~五轮）**：2captcha 能出 token 但注入交付被浏览器栈架构性挡死；换全新账号 hCaptcha 照弹。
仍未验证的唯一环境变量是**出口 IP（住宅代理）**与**换卡 BIN**——若要继续追全自动，这是下一个该试的方向；
否则落**半自动**（人工过 hCaptcha 再自动续付款）最务实可靠。诊断脚本另加 probe_opencode_onboarding.py。

### hCaptcha 注入攻克·第六轮：原生 Playwright 前置注入——**攻克成功**（2026-07-25）
用户选「续追全自动」。改用**原生 Playwright**（非 Patchright，pip 装 playwright==1.61.0，channel=chrome）：
- **probe_vanilla_inject.py（不扣款）**：context.add_init_script 把 hook **前置注入进所有帧含 OOPIF**，
  hcaptcha 帧 [10] b.stripecdn/HCaptchaInvisible `installed=true, hasHcaptcha=true, diag{sf:1,rc:2,ec:2,rs:2,wids:4}`
  ——execute 被拦成 2 个可控 Promise。**证实原生栈作主调试器能暂停 OOPIF 前置注入，Patchright 阉割的正是这个。**
- **run_subscribe_once.py 默认改走 create_driver_vanilla（原生栈）真实付款**：2captcha 出 token →
  `_inject_hcaptcha_token` 逐帧 `deliverToken` → hcaptcha 帧 `hookCbs=2`（resolver 兑现）→
  **Stripe 接受 token 放行到扣款**。outcome 从 needs_captcha 变为 **failed=「unable to authenticate your
  payment method」（卡拒付，非 hCaptcha）**——**hCaptcha 已被彻底绕过**。

**生产接线**：`driver.create_driver_vanilla(profile_id)`（原生栈，仅付款用；注册/登录仍 Patchright 主栈）；
`run_subscribe_once.py` 默认原生栈（`--patchright` 切回）；hook 靠 `install_hcaptcha_hook`→context.add_init_script
（原生栈下真正生效）+ captcha 的 execute/render/getResponse 拦截 + deliverToken。
**剩余（已定论）**：卡质量——leilao40 逐卡试 8 张（group 1）：**7/7 到达扣款的卡全部同一错
「unable to authenticate your payment method」**（第 8 张一次 2captcha 超时 needs_captcha）。
号码 1017/1015/3000/1005/1009/2009/3006/1001 像测试/顺序号 → **group 1 整批过不了 Stripe 认证**，
系统性卡源问题、非单卡质量、非代码问题。需换真实可过 Stripe 的卡。hCaptcha 每张稳过（hookCbs=2）。
Web 层 `_recharge_one_account` 订阅分支接入时，付款 driver 也要切原生栈。cdp_inject.py / driver 的
bypass_csp/disable_site_isolation 是第四轮失败基建，可删。
