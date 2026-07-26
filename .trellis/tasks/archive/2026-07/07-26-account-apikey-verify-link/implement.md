# Implement — 账号 apikey 与邮箱认证链接落库

执行顺序按依赖排列;每步后跑对应验证再进入下一步。所有改动 additive,可分步提交。

## 前置检查
- [ ] `data/openrouter_auto.db` 在 `.gitignore`(避免 apikey 随库进 git);若否先补。
- [ ] `frontend/node_modules` 存在、`npm` 可用(Step 7 需 build);缺则 `cd frontend && npm install`。
- [ ] 改 `database.py` 前备份:`cp data/openrouter_auto.db data/openrouter_auto.db.bak`。

## Step 1 · DB schema V10 · `src/models/database.py`
- 加 `_SCHEMA_V10`(3 个 `ALTER TABLE accounts ADD COLUMN`:apikey / apikey_updated_at / email_verify_link),注册 `_MIGRATIONS[10]`。
- 验证:`.venv/bin/python -c "from src.models.database import Database; Database()"` → 无错;`sqlite3 data/openrouter_auto.db "PRAGMA user_version;"` = 10;`.schema accounts` 含 3 新列。**(AC1)**

## Step 2 · 账号模型 · `src/models/account.py`
- `upsert` 加 `email_verify_link=None`:SELECT 补 `email_verify_link`;UPDATE 用 COALESCE 写法(传入优先,否则保留);INSERT 带上。
- 新增 `update_apikey(email, apikey)`(空则 return)。
- 新增 `backfill_email_verify_link(email, link)`(只写空值,返回 rowcount)。
- 验证:`.venv/bin/python -c "import ast; ast.parse(open('src/models/account.py').read())"` 语法通过;逻辑在 Step4/5 端到端验证。

## Step 3 · 注册落库搭车 · `src/web/app.py::_subscribe_one_account`
- 约 :707(suspended)、:713(注册成功)两处 `upsert(...)` 传 `email_verify_link=hacc.link`。
- 验证:`grep -n "email_verify_link=hacc.link" src/web/app.py` 命中 2 处;`ast.parse` 语法通过。**(AC5 — 代码审查;真注册链路不在本期实跑)**

## Step 4 · 回填脚本 · `scripts/backfill_verify_links.py`(新建)
- `read_hotmail_accounts(<base>/hotmail.xlsx)` → 逐个 `account.backfill_email_verify_link`;打印命中/回填数。参考 `scripts/fix_failed_accounts_status.py` 的 Database/AccountModel 初始化。
- 运行:`.venv/bin/python scripts/backfill_verify_links.py`
- 验证:`sqlite3 -header data/openrouter_auto.db "SELECT email, email_verify_link FROM accounts WHERE email_verify_link IS NOT NULL;"` → 链接等于 xlsx 原值。**(AC2)**

## Step 5 · apikey 抓取脚本 · `scripts/fetch_apikeys.py`(新建)
- 选账号(默认 credits_balance>0 / `--email` / `--all`)、串行、`/auth`→提 wid→`/keys`→DOM 正则抓 `sk-`→`update_apikey`;失败跳过并汇总。日志打码。
- 先单测:`PYTHONUNBUFFERED=1 .venv/bin/python scripts/fetch_apikeys.py --email <一个有余额账号>`(有头浏览器)。
- 确认 OK 后全量:`... scripts/fetch_apikeys.py`(7 个)。
- 验证:`sqlite3 data/openrouter_auto.db "SELECT email, substr(apikey,1,8), apikey_updated_at FROM accounts WHERE apikey IS NOT NULL;"`。**(AC3)**

## Step 6 · API 接口 + 导出 · `src/api/routes.py`
- 列表 `data.append`(约 :303)加 `apikey`/`apikey_updated_at`/`email_verify_link`。
- 导出 `headers`(约 :569)加 2 表头;`write_acc_cols`(约 :583)加 `column=21`(apikey)、`column=22`(email_verify_link)。
- 验证:启动 `server.py`,`curl -s localhost:<port>/api/accounts | jq '.data[0] | {apikey, email_verify_link}'`;导出 xlsx 打开含 2 列。

## Step 7 · 前端 · `frontend/src/views/Accounts.vue` + 构建
- `<thead>` 加 2 `<th>`(API Key / 邮箱认证链接,放 Credits 余额后);`<tbody>` 加 2 `<td>`(apikey monospace 明文;链接 `<a :href target=_blank>`);loading/empty `colspan` 9→11。
- `cd frontend && npm run build`(输出 `../static`)。
- 验证:重启后端,浏览器打开账号页 → 两列明文可见、链接可点。**(AC4)**

## 验证命令汇总
```bash
# 迁移
.venv/bin/python -c "from src.models.database import Database; Database()"
sqlite3 data/openrouter_auto.db "PRAGMA user_version;"
# 回填
.venv/bin/python scripts/backfill_verify_links.py
# 抓 key(单账号先测)
PYTHONUNBUFFERED=1 .venv/bin/python scripts/fetch_apikeys.py --email carold030@hotmail.com
# 前端
cd frontend && npm run build
```

## 风险 / 回滚点
- **`src/models/database.py`**（启动路径）:迁移写错影响启动 → 已备份 `.bak`,回滚 `cp` 还原。
- **`src/web/app.py`**（注册主流程）:仅加可选参数,不改控制流。
- **前端**:build 改坏 → `git checkout -- static frontend/src/views/Accounts.vue`。
- **fetch_apikeys**:登录态失效账号被跳过并列出(非致命),可后续手动补登再跑。

## 完成判据(→ AC 映射)
AC1→Step1;AC2→Step4;AC3→Step5;AC4→Step6+7;AC5→Step3(代码审查)。
