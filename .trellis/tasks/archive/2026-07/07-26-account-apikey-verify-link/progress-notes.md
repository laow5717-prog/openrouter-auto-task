# 实现进度 notes(防压缩丢失)

任务 in_progress。备份库:`data/openrouter_auto.db.bak-20260726`。

## 已完成(2026-07-26 第二会话逐项实测核实)

- **Step1** database.py:`_SCHEMA_V10`(apikey/apikey_updated_at/email_verify_link)+`_MIGRATIONS[10]`。`PRAGMA user_version=10`,三列存在。✓
- **Step2** account.py:`upsert(email_verify_link=None)` COALESCE 语义、`update_apikey`、`backfill_email_verify_link` 均已核实存在且正确(上一会话的"语法存疑"警告解除)。✓
- **Step3** app.py `_subscribe_one_account` 两处 upsert 传 `email_verify_link=hacc.link`。✓
- **Step4** scripts/backfill_verify_links.py **重建并实跑**:10/10 账号 `email_verify_link` 落库(sqlite 实查确认)。✓
  - ⚠️ 上一会话记录称"已建已跑 10 落库"不属实——当时文件不存在、库里 0 行;本会话重做。
- **Step5(部分)** scripts/fetch_apikeys.py 已存在(4176B);单账号 carold030 已有 key(13:41)。全量跑见下。
- **Step6** routes.py:列表接口 data.append 加 `apikey`/`apikey_updated_at`/`email_verify_link`;导出 headers 加 "API Key"/"邮箱认证链接"(col 21/22),`write_acc_cols` 对应写入。✓
- **Step7** Accounts.vue:thead 加 2 th(Credits 余额后);tbody 加 2 td(apikey monospace 明文+title 显抓取时间;链接 `<a target=_blank>`);colspan 9→11。`npm run build` 成功(Accounts-CiEOIi3X.js,产物 grep 到新列)。✓
- `.gitignore` 已确认覆盖 `data/openrouter_auto.db`(key 不进 git)。✓

## 已完成(续)

- **Step5 全量** ✓:`python3 scripts/fetch_apikeys.py` 跑完 7 个有余额账号,**成功 7/7、失败 0**;每个 apikey 形如 `sk-...`、apikey_updated_at 落库(16:37~16:38)。sqlite 实查:apikey 非空 7、link 非空 10。

## 验收对照(全部达成)

- AC1 迁移 ✓ / AC2 回填 ✓(10/10) / AC3 抓 key ✓(7/7,失败 0) / AC4 接口+导出+前端 ✓(代码+构建完成) / AC5 注册搭车 ✓(代码审查:app.py 已传 hacc.link)。
- 未 commit,等用户确认。

## 关键事实

- apikey 抓取:`/auth`→302 `/workspace/<wid>`;`/workspace/<wid>/keys` outerHTML 含完整 `sk-` 明文;正则 `sk-[A-Za-z0-9_\-]{20,