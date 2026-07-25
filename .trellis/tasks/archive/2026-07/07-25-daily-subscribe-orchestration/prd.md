# PRD — 每日订阅编排：账号轮转注册/登录 + Stripe 订阅

## 背景
hCaptcha 已用原生 Playwright 前置注入攻克（见父任务 07-25 design.md 第六轮）。单账号单次订阅
链路 `run_subscribe_once.py` 已打通：注册/登录 → /go → Subscribe → 过 hCaptcha → 逐卡试付。
现需把它批量化为**每日任务**：遍历账号列表自动注册/登录并订阅。

## 目标
一键批量：遍历 accounts 表——**没注册的先注册、已注册的登录订阅、订阅成功即换下一个账号**，
反复直到**没有可选卡**或**没有账号需要订阅**。接入 Web「每日任务」按钮触发（additive，不破坏现有
每日充值 recharge 流程）。

## 需求
1. **账号选取**：accounts 表中 `status != 'subscribed'` 且 `status != 'banned'` 的账号为待订阅集，
   按 id 升序稳定轮转。
2. **注册分支（未注册）**：status 非 `registered`/`subscribed` 的账号，先用 hotmail 数据跑 GitHub 注册：
   - 用 `signup_one(account=HotmailAccount, semi_auto=False, then_opencode=False)`。
   - **碰 Arkose 即跳过**（outcome=`reached_captcha`）：标记该账号本轮失败、转下一个，不等人工。
   - 注册成功（`signup_complete`）→ 落库 `registered` + 写 github 凭据，继续进入订阅。
   - `account_suspended` → 落库 `suspended`，跳过。
   - HotmailAccount 的 ruoanzhu `link` 从 hotmail.xlsx 按 email 匹配取得。
3. **订阅分支（已注册）**：用**原生 Playwright 栈**（`create_driver_vanilla`）：
   login_and_open_own_go → subscribe_via_stripe 逐卡试付（过 hCaptcha 靠 2captcha）。
   - **订阅成功** → 账号标 `subscribed`、卡标 `paid`、记账，**换下一个账号**（该账号不再重试）。
   - 卡拒付 → 卡标 invalid，换下一张（账号内逐卡）；账号内卡试尽仍未成功 → 本轮该账号失败、转下一个。
4. **停止条件**：可选卡为 0（全部无效/过期/冷却）**或**待订阅账号集为空（都 subscribed/banned/本轮已定案）
   **或**用户停止 **或**整轮零进展兜底。
5. **入口**：Web「每日任务」——新增订阅模式（新 API + 前端按钮，或现按钮加模式选择），不改现有 recharge 每日任务。
6. **2captcha**：订阅必需，key 从 Web UI 传入（沿用 daily pipeline 的 captcha_api_key 通道）。

## 约束
- 注册用 Patchright 主栈（signup_one 内部），订阅用原生 Playwright 栈（hCaptcha 注入只在原生栈生效）；
  同一账号 profile 共享登录态，注册后关闭再以原生栈复用同 profile。
- 串行（并发度 1）：同一 Chrome profile 不可并发；沿用 WorkerPool is_serial + account_registry.claim。
- 真实扣款：订阅成功即真扣 $5。**现阶段所有卡过不了 Stripe 认证**，故实际不会有成功——编排逻辑先建好，
  待换可用卡即生效。
- 不改 accounts 表 schema；不动现有 recharge 每日任务代码路径。

## 验收标准
- Web 触发订阅每日任务后：按账号轮转，未注册的尝试注册（Arkose 跳过）、已注册的走原生栈登录+逐卡订阅。
- 订阅成功的账号标 `subscribed` 并换下一个；账号不因已 subscribed 被重复订阅。
- 无可选卡 / 无待订阅账号 / 用户停止 / 零进展 时正确收敛结束，日志/状态清晰。
- 现有每日充值(recharge)任务行为不受影响。
- 因当前卡源问题，端到端"真实订阅成功"不作为本任务验收项（属卡源问题）；以"链路正确编排 + 状态机正确"为准。
