# AI Credits 自动充值功能

## Goal

为已绑定信用卡的 Cloudflare 账号添加 AI Gateway Credits 自动充值功能。通过浏览器模拟操作（与现有注册+绑卡模式一致），登录账号后跳转到 AI 充值页面完成 $10 充值。

## Requirements

### 核心流程
1. 使用已有账号的 email + cf_password 登录 Cloudflare（浏览器模拟）
2. 登录后从 URL 提取 account_id
3. 跳转到 `https://dash.cloudflare.com/{account_id}/ai/ai-gateway/credits`
4. 点击充值按钮，固定充值 $10
5. 完成支付确认

### 前端
- Accounts.vue 操作列：为已绑卡状态（`bound*`）的账号添加"充值"按钮
- 点击后发起后端请求，按钮显示 loading 防止重复点击
- 充值结果反馈给用户

### 后端
- 新增 `/api/accounts/recharge` POST 接口（接收 email）
- 新增 `login_cloudflare(driver, email, password)` — 独立封装，可复用
- 新增 `recharge_ai_credits(driver, amount=10)` — 充值自动化
- 编排函数：创建浏览器 -> 登录 -> 导航充值页 -> 执行充值 -> 关闭浏览器

### 封装复用
- `login_cloudflare` 独立封装，未来其他需登录已有账号的功能可复用

## Constraints
- 充值金额固定 $10
- 仅对已绑卡账号（status 包含 `bound`）可用
- 充值页面 DOM 结构需实际调试确认，初始实现基于合理推测

## Acceptance Criteria
- [ ] 操作列出现"充值"按钮（仅已绑卡账号可见）
- [ ] 点击充值后浏览器自动登录、跳转充值页、完成 $10 充值
- [ ] 充值成功/失败有明确反馈
- [ ] login_cloudflare 函数独立可复用
