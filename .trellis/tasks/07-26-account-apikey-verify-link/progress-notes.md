# 实现进度 notes（防压缩丢失）

任务已 in_progress。备份库:`data/openrouter_auto.db.bak-20260726`。

## 已完成(需在压缩后重新核实真实性,因部分在输出截断期执行)
- **Step1** database.py:加了 `_SCHEMA_V10`(apikey/apikey_updated_at/email_verify_link)+ `_MIGRATIONS[10]`。迁移已跑,`PRAGMA user_version=10`,三列存在。✓ 已确认
- **Step2** account.py:`upsert` 加 `email_verify_link=None` 参数;新增 `update_apikey(email,apikey)`、`backfill_email_verify_link(email,link)`。**⚠️ 需重新核实语法与结构**(grep 行号异常疑似显示错乱)。
- **Step3** app.py `_subscribe_one_account` 两处 upsert 传 `email_verify_link=hacc.link`(suspended + registered 分支)。
- **Step4** scripts/backfill_verify_links.py 已创建(2141 bytes)。回填已跑,10 个账号 email_verify_link 落库。**⚠️ 核实计数=10**。

## 待做
- **Step5** scripts/fetch_apikeys.py **不存在,需重建**(源码见下)。建后:先 `--dry`,再 `--email carold030@hotmail.com` 单测(有头浏览器),OK 后全量跑。
- **Step6** src/api/routes.py:①列表接口约 :303 `data.append` 加 `apikey`/`apikey_updated_at`/`email_verify_link`;②导出 `headers`(约 :569,现20列)末尾加 `"API Key"`、`"邮箱认证链接"`;`write_acc_cols`(约 :583)加 `ws.cell(row=r,column=21,value=acc.get('apikey') or '')`、`column=22,value=acc.get('email_verify_link') or ''`。
- **Step7** frontend/src/views/Accounts.vue:thead(:41)Credits余额后加2个th(API Key/邮箱认证链接);tbody(:59)加2个td(apikey monospace明文;链接`<a :href target=_blank>`);loading/empty colspan 9→11(:54,:57)。然后 `cd frontend && npm run build`(vite outDir ../static)。

## 关键事实
- apikey 抓取已探索确认:`/auth`→302到`/workspace/<wid>`;`/workspace/<wid>/keys` 页 outerHTML 含完整 `sk-` 明文(显示打码但DOM有全量);正则 `/sk-[A-Za-z0-9_\-]{20,}/` 抓。每账号 profile=email 复用登录态。
- create_driver(headless=False, profile_id=email) / close_driver / session.get / session.page.evaluate / session.current_url。均 from src.browser.driver。
- 有余额账号7个(credits_balance>0)。
- 展示=明文(用户定)。触发=脚本(用户定)。
- 验收:AC1迁移✓ AC2回填✓ AC3抓key AC4接口+前端 AC5注册搭车(代码审查)。

## scripts/fetch_apikeys.py 完整源码(重建用)
见 design.md「5. apikey 抓取」。核心:
- argparse: --email / --all / --dry / --db
- 默认 targets = `SELECT email FROM accounts WHERE credits_balance>0 ORDER BY id`
- WID_RE=`wrk_[A-Za-z0-9]+`;KEY_JS 抓 outerHTML.match(/sk-[A-Za-z0-9_\-]{20,}/)
- fetch_one(email): create_driver(headless=False,profile_id=email) → get /auth → wait_workspace(轮询current_url出wrk_,~25s超时)→ get /workspace/<wid>/keys → sleep3 → evaluate KEY_JS → 有key返回(True,detail,key)否则False;finally close_driver
- main: 遍历 targets 串行,ok则 account.update_apikey(email,key),打码打印;末尾汇总成功/失败清单
- mask(key)=key[:8]+...+key[-4:]
