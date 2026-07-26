# 账号 apikey 与邮箱认证链接落库

## Goal

给账号管理增加两块能力,让「有余额账号的 opencode API key」和「账号的邮箱认证链接」都进入 accounts 表并在账号列表明文可见:便于后续直接取 key 调用 API,以及排障时直接拿到该账号的收信链接。

## Background(confirmed facts)

- accounts 表(`src/models/database.py`)现有列:id / email / login_password / email_password / status / created_at / updated_at / credits_balance / balance_updated_at / bound_card_count / cards_checked_at。**无 apikey 列,无邮箱认证链接列**。
- schema 迁移机制:`PRAGMA user_version` + `_MIGRATIONS` 字典(`src/models/database.py:160`),当前最高 V9;新增列走 `_SCHEMA_V10`(`ALTER TABLE accounts ADD COLUMN ...`),`_migrate()` 幂等按版本号推进。
- 「邮箱认证链接」= `hotmail.xlsx` 每行第三段的 ruoanzhu 收信链接(形如 `https://www.ruoanzhu.com/s?e=..&p=..&h=1&r=1&i=2`)。解析:`src/services/hotmail_inbox.py::read_hotmail_accounts`(按 `----` 拆行 → `HotmailAccount(email,password,link,raw)`)。运行时按 email 惰性缓存在 `src/web/app.py::_hotmail_by_email`。当前**只用于注册收码,从未落库**。当前 xlsx 有 10 行(9~10 个可匹配邮箱)。
- 账号列表接口 `src/api/routes.py`(约 :295)返回 email/password/status/email_password/card_count/bound_card_count/credits_balance/balance_updated_at;xlsx 导出在约 :590(逐列 `ws.cell`)。前端 `frontend/src/views/Accounts.vue`,构建产物 `static/assets/Accounts-*.js`。
- 账号 upsert:`src/models/account.py::upsert`(email/login_password/email_password/status)。注册成功落库点:`src/web/app.py::_subscribe_one_account`(约 :713,`models['account'].upsert(...)`),此处正好持有 `hacc.link`,可顺带写认证链接。
- 有余额账号:`credits_balance>0` 共 7 个(均 $20,status=archived)。总账号 10。每账号有独立 profile `data/profiles/<email>/`,`create_driver(headless=False, profile_id=<email>)` 复用其登录态(safe_name = 正则替换非 `\w@.-` 为 `_`)。
- 项目当前**无任何获取 opencode apikey 的代码**(全仓 grep `api[_-]?key` 零命中)。

## apikey 探索结论(已确认 · 2026-07-26 用 manual profile 实测)

- 登录后是 **workspace 模型**:`/auth` 会自动跳到 `/workspace/<wid>`;导航栏含 Zen / Go / Usage / **API Keys** / Members / Billing / Settings。
- **API Keys 页 = `/workspace/<wid>/keys`**。每个 zen 账号开通时**自动生成一个名为 "Default" 的 key**。页面有 "Create API Key" 按钮、每行 "Delete"。
- **完整 key 明文直接在 DOM 里**:页面**显示**打码的 `sk-TJZ0...vRPu`,但 `document.documentElement.outerHTML` 含完整 `sk-...`(实测正则 `/sk-[A-Za-z0-9_\-]{6,}/` 一击命中全量 64+ 字符 key)。→ **无需点 Copy Key、无需读剪贴板**,直接 `page.evaluate` 抓 DOM。
- **获取流程(每账号)**:`create_driver(headless=False, profile_id=<email>)` → `get("https://opencode.ai/auth")`(跳转后从 URL 提取 `wid`)→ `get("/workspace/<wid>/keys")` → `evaluate` 正则抓 `sk-` → 落库。登录态失效则停在 `/auth`(无 `wid`/无 `sk-`)→ 判失败,不阻塞其他账号。

## 已定决策

- **触发方式**:apikey 获取用**一次性批处理脚本** `scripts/fetch_apikeys.py`(手动执行),契合现有 `scripts/`(fix_*/run_*)运维脚本风格;前端只负责展示已落库结果。
- **展示形态**:apikey 与邮箱认证链接在账号列表**明文直接显示**。
- **回填范围**:邮箱认证链接对**全部**账号回填(凡 `hotmail.xlsx` 能匹配 email)。
- **key 获取语义**:直接读账号自带的 Default key 明文,不创建、不重置。

## Requirements

### R1 邮箱认证链接落库
- **R1.1** accounts 新增列 `email_verify_link TEXT`(V10 迁移)。
- **R1.2** 从 `hotmail.xlsx` 全量回填已有账号:email 匹配则写入其 ruoanzhu link(覆盖空值)。提供可重复执行的入口。
- **R1.3** 后续注册/导入新账号时一并写入:`account.upsert` 支持 `email_verify_link` 可选参数(向后兼容);`app.py::_subscribe_one_account` 注册成功 upsert 时传 `hacc.link`。
- **R1.4** 账号列表接口 `/api/accounts` 返回 `email_verify_link`;前端 `Accounts.vue` 明文展示;xlsx 导出增列。

### R2 有余额账号 apikey 获取落库
- **R2.1** accounts 新增列 `apikey TEXT`、`apikey_updated_at TEXT`(V10 迁移)。
- **R2.2** `scripts/fetch_apikeys.py`:默认遍历 `credits_balance>0` 账号(支持 `--email` / `--all` 覆写),逐账号复用 profile 登录态,导航 `/auth`→`/workspace/<wid>/keys`,DOM 正则抓 `sk-`,调模型落库;登录态失效/抓不到 → 记失败并继续下一个;串行执行(有头浏览器、同 profile 不可并发)。
- **R2.3** `account.py` 新增 `update_apikey(email, apikey)`(写 apikey + apikey_updated_at）。
- **R2.4** 账号列表接口返回 `apikey` / `apikey_updated_at`;前端明文展示;xlsx 导出增列。

## Acceptance Criteria

- [ ] **AC1**(R2.1/R1.1)迁移幂等:重启 app 不报错,`PRAGMA user_version=10`,accounts 新增 `apikey`/`apikey_updated_at`/`email_verify_link` 三列。
- [ ] **AC2**(R1.2)回填后,`hotmail.xlsx` 中能匹配到的账号,其 `email_verify_link` 非空且等于 xlsx 原链接。
- [ ] **AC3**(R2.2/R2.3)`fetch_apikeys.py` 对 7 个有余额账号跑完:成功账号 `apikey` 形如 `sk-...` 且 `apikey_updated_at` 非空;失败账号有清晰日志(区分登录态失效 vs 抓取失败),不中断其余账号。
- [ ] **AC4**(R1.4/R2.4)`/api/accounts` 返回三新字段;前端账号表可见两列明文;导出含两列。
- [ ] **AC5**(R1.3)注册新账号后(或用同链路模拟),该账号 `email_verify_link` 落库。

## Out of Scope

- opencode API key 的创建/删除/多 key 管理(仅读取 Default key)。
- 用 apikey 实际发起 API 调用/计费验证。
- 前端触发按钮 / worker 异步任务(本期用批处理脚本,后续可增强)。
