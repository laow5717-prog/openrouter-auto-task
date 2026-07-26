# Design — 账号 apikey 与邮箱认证链接落库

## 架构总览

两块 **additive** 能力,不改现有充值/订阅/注册主流程,只:①在 accounts 表加 3 列;②在注册落库点搭车写认证链接;③新增两个运维脚本(回填链接、抓 apikey);④展示层(接口/导出/前端)加列。

数据流:
```
hotmail.xlsx --read_hotmail_accounts--> backfill_verify_links.py --> accounts.email_verify_link
                                        app.py 注册成功 upsert(hacc.link) --^
opencode /workspace/<wid>/keys DOM --fetch_apikeys.py--> accounts.apikey / apikey_updated_at
accounts --routes /api/accounts, /api/accounts/export--> Accounts.vue 明文展示
```

## 1. 数据模型 · `src/models/database.py`

新增 `_SCHEMA_V10` 并注册 `_MIGRATIONS[10]`:
```sql
ALTER TABLE accounts ADD COLUMN apikey TEXT;
ALTER TABLE accounts ADD COLUMN apikey_updated_at TEXT;
ALTER TABLE accounts ADD COLUMN email_verify_link TEXT;
```
- 幂等:`_migrate()` 按 `user_version` 从 9→10 只执行一次;`max(_MIGRATIONS)` 自动变 10。
- additive:现有 `SELECT *`(get_paginated/get_all)自动带出新列,读取侧无需改。

## 2. 账号模型 · `src/models/account.py`

- **`upsert(...)` 增加 `email_verify_link=None` 可选参数**(向后兼容,现有调用不传):
  - 已存在账号:仿 `login_password` 的 `COALESCE` 写法——传入非空则更新,否则保留原值(`final_link = email_verify_link if email_verify_link else existing['email_verify_link']`);需在 SELECT 里补出 `email_verify_link`。
  - 新账号:INSERT 带上 `email_verify_link`。
- **新增 `update_apikey(email, apikey)`**:仿 `update_balance`——`UPDATE accounts SET apikey=?, apikey_updated_at=datetime('now','localtime'), updated_at=datetime('now','localtime') WHERE email=?`;`apikey` 空则直接 return(不写脏值)。
- **新增 `backfill_email_verify_link(email, link)`**:`UPDATE accounts SET email_verify_link=?, updated_at=... WHERE email=? AND (email_verify_link IS NULL OR email_verify_link='')`;返回 `cursor.rowcount`(便于统计)。只写空值,可重复执行不覆盖已有。

## 3. 注册/导入时写入认证链接(R1.3)· `src/web/app.py::_subscribe_one_account`

两处 `models['account'].upsert(...)` 传 `email_verify_link=hacc.link`:
- 约 :707 suspended 分支;约 :713 注册成功分支。
- `hacc`(HotmailAccount)已在作用域内(`_hotmail_by_email(email)`),`hacc.link` 即 ruoanzhu 链接。

## 4. 一次性回填 · `scripts/backfill_verify_links.py`(R1.2)

```
read_hotmail_accounts(<base>/hotmail.xlsx) -> [HotmailAccount]
for acc in accounts: n += models.account.backfill_email_verify_link(acc.email, acc.link)
打印「回填 N 个账号认证链接（xlsx M 行,accounts 命中 K）」
```
- 复用 `Database()` + `AccountModel`(与其他 scripts 同构:`sys.path` 注入 + `from src.models...`)。
- 只写 accounts 里已存在且当前为空的账号;xlsx 有但 accounts 无的邮箱跳过(不新建账号)。
- 幂等、可重复执行。

## 5. apikey 抓取 · `scripts/fetch_apikeys.py`(R2.2)

选账号:默认 `credits_balance>0`(`SELECT email FROM accounts WHERE credits_balance>0`);`--email <addr>` 单个;`--all` 全部。

**串行**(有头浏览器 + 同 profile 不可并发;7 个账号量级足够):
```
for email in targets:
    session = create_driver(headless=False, profile_id=email)
    try:
        session.get("https://opencode.ai/auth")
        # 轮询等跳转:current_url 出现 wrk_ 或超时(~20s)
        wid = re.search(r'wrk_[A-Za-z0-9]+', session.current_url)
        if not wid: 记 FAIL「登录态失效/未跳转 workspace」; continue
        session.get(f"https://opencode.ai/workspace/{wid.group(0)}/keys"); sleep(3)
        key = session.page.evaluate(KEY_JS)   # (outerHTML.match(/sk-[A-Za-z0-9_-]{20,}/)||[null])[0]
        if key: models.account.update_apikey(email, key); 记 OK(打码打印 key)
        else:   记 FAIL「/keys 未抓到 sk-」
    except Exception as e: 记 FAIL(异常)
    finally: close_driver(session)
汇总打印 成功/失败清单
```
- key 正则 `{20,}`:真实 key 64 字符,避免误命中页面上的短 `sk-` 文案。
- 只在拿到合法 `sk-` 才落库,失败不写脏数据、不阻塞后续账号。
- 打印时打码(`sk-XXXX...XXXX`),完整值只入库。

## 6. API 接口 · `src/api/routes.py`

- **列表接口**(约 :303 `data.append`)增 3 字段:`apikey` / `apikey_updated_at` / `email_verify_link`(`acc.get(...)`)。
- **导出**(约 :569 `headers` + :583 `write_acc_cols`):`headers` 末尾加 `"API Key"`、`"邮箱认证链接"`;`write_acc_cols` 加 `ws.cell(row=r, column=21, value=acc.get('apikey') or '')`、`column=22, value=acc.get('email_verify_link') or ''`。

## 7. 前端 · `frontend/src/views/Accounts.vue`

- `<thead>`(:41)在「Credits 余额」后加两 `<th>`:`API Key`、`邮箱认证链接`。
- `<tbody>` v-for(:59)对应加两 `<td>`:`<td style="font-family:monospace">{{ acc.apikey || '-' }}</td>`(明文);`<td><a v-if="acc.email_verify_link" :href="acc.email_verify_link" target="_blank">{{ acc.email_verify_link }}</a><span v-else>-</span></td>`。
- loading/empty 行 `colspan="9"` → `colspan="11"`(:54、:57)。
- **构建**:`cd frontend && npm run build`(vite `outDir:'../static'`)→ 覆盖 `static/`;重启后端加载新产物。

## 兼容性 / 回滚

- V10 additive:老库升级即加列;SQLite 不易 DROP COLUMN,但新列可空、不影响旧逻辑;无需回滚脚本。
- `upsert` 新参数默认 None,现有 3 处调用不受影响。
- 后端全 additive;前端 `static/` 改动 git 可回滚(`git checkout -- static frontend`)。
- 抓取脚本只在拿到 `sk-` 才 UPDATE,失败无副作用。

## 安全 / 风险

- apikey 是真实凭证:落库明文 + 前端明文(用户明确要求)。脚本日志打码。
- **须确认 `data/openrouter_auto.db` 在 `.gitignore`**(避免 key 随库进 git)——implement 首步检查。
- 抓取脚本跑有头浏览器,需本机 GUI;登录态失效的账号会被跳过并列出,可后续手动补登。
