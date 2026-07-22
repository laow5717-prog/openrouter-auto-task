# PRD — 改造为 OpenRouter 项目框架

## Goal
把现有 Cloudflare 自动化项目（Python + Flask + Vue + SQLite + Patchright 浏览器自动化）在**当前目录 `openrouter-auto-task` 就地改造**为面向 OpenRouter 的全新项目。本轮范围限定为**框架搭建先跑通**：完成数据隔离、全局改名去 Cloudflare、把 Cloudflare 专属站点流程存根化，使 Web 服务能以 OpenRouter 专属的数据路径干净启动。OpenRouter 站点的实际注册/绑卡/充值流程本轮不实现，留作后续接入。

## Background
本目录是从 cloudflare-auto-task 复制而来（git remote 已改为 openrouter-auto-task），代码与 `data/` 内数据全是 Cloudflare 的。原 Cloudflare 项目在别的目录、独立运行、不受本次改造影响。通用基础设施（临时邮箱、验证码框架、SQLite DAO、并发调度 WorkerPool、Web 管理界面、卡池/Excel）站点无关，予以保留复用；Cloudflare 专属的注册/绑卡/充值编排与 Turnstile/Stripe DOM 操作予以存根化，作为 OpenRouter 后续重写的接入面。

## Confirmed Facts（勘查锚点）

### 数据落盘面（隔离目标）
- Dev 数据根 `get_base_dir()/data`（`src/config.py:160`）；Frozen 数据根 `~/.cloudflare-auto-task`（`src/config.py:158`）。
- SQLite 库名 `cloudflare_auto.db`：`src/config.py:86,286`、`src/models/database.py:199`、运行时实际路径 `src/web/app.py:1073`、`config.yaml:60`。
- profile 根 `data/profiles`（`src/browser/driver.py:593`）；临时 profile 前缀 `cf_chrome_`（`driver.py:601`）；下载目录 `driver.py:605`。
- uploads：`src/api/routes.py:791`（`pool_upload_{group_id}.xlsx` @793）。
- 报表：`bind_report_{ts}.xlsx`（`src/services/card.py:132`）、`card_history_export.xlsx`(routes:268)、`accounts_export.xlsx`(routes:623)、`有效卡_{date}.xlsx`(routes:927)、模板 `credit_cards_template.xlsx`(card.py:37)。
- 文本存储 `registered_accounts.txt`→`.migrated`：`src/config.py:91,292`、`src/models/database.py:213,223,245`。
- `server.log` 由外部重定向产生（应用日志是内存 ring buffer `src/web/app.py:118`）。
- 现存真实数据：`data/cloudflare_auto.db`(+wal/shm/多个 .bak)、`data/profiles/`、`data/uploads/`、200+ `data/bind_report_*.xlsx`、根目录 `registered_accounts.txt.migrated`。

### 命名面
- 品牌/文案：`frontend/index.html:6`、`SidebarControls.vue:11`、`Workbench.vue:54,155`、`CardPool.vue:5`、`server.py:2`、`build.py:147-179`、大量后端注释/日志。
- DB 字段 `accounts.cf_password`（`src/models/database.py:16`；`src/` 内共 49 处引用）。
- 打包命名：`CloudflareAutoTask.spec:35,55`、`build.py:34,53,104-108`、`.github/workflows/build.yml:17,20`、`pyproject.toml:2,4`、`cloudflare-auto-task.iml`、`release.sh:5`。
- 站点 URL/域名：`dash.cloudflare.com`、`challenges.cloudflare.com`（registration.py / driver.py / captcha.py 多处）；邮件正则 `src/utils.py:185-188`；发件人过滤 `src/services/email.py:182,286`。

### 站点耦合面
- Cloudflare 专属编排入口 `src/services/registration.py`（1185 行）暴露 4 个函数：`register_one_account`、`register_and_bind_cards`、`bind_cards_to_existing_account`、`recharge_account`；被 `src/web/app.py:26` 与 `src/api/routes.py:13` import；调用点 app.py:229/316/440/711、routes.py 内 `login_cloudflare` 开浏览器流程（routes:430-440）。
- `src/browser/driver.py`（5967 行）含 CF/Turnstile/Stripe 方法群（login_cloudflare、check_and_handle_cf_challenge、turnstile 系列、stripe 系列、navigate_to_ai_credits 等）与站点无关的浏览器基建（create_driver、profile 卫生、_safe_goto/click/fill）。`login_cloudflare` 在 src/tests 共 8 处引用。
- Schema 表名全部站点无关；唯一显式 CF 命名字段 `accounts.cf_password`。`credits_balance`/`recharge_logs`/`invoice_payment_state`/`INVOICE_DAILY_CAP`/`TOPUP_AMOUNT` 为 CF AI Credits 语义耦合（结构保留，语义后续重定义）。测试跑临时库不碰生产库（`tests/conftest.py`）。

## Decisions（已确认）
1. 工作方式：Trellis 任务 + 先规划（复杂任务，需 prd/design/implement）。
2. 隔离方式：清理当前目录复用；原 Cloudflare 项目不受影响。
3. 业务范围：本轮仅框架跑通，站点流程后续再接。
4. **旧数据处置：直接删除** `data/` 下全部真实数据及根目录 `registered_accounts.txt.migrated`（原项目为独立目录，此处仅为副本）。
5. **命名方案：openrouter 专属名** —— db=`openrouter_auto.db`；frozen 目录=`~/.openrouter-auto-task`；pyproject 包=`openrouter-auto-task`；打包产物=`OpenRouterAutoTask`；前端标题=`OpenRouter Auto Task`；DB 字段 `cf_password`→`login_password`（因重建库、无需迁移）。
6. **剥离力度：存根化保留占位** —— CF 专属编排（registration 4 函数、routes 开浏览器登录流程）替换为抛 `NotImplementedError`（OpenRouter TODO）的存根，保持函数签名以免破坏 import；driver.py 的 CF/Turnstile/Stripe 方法保留在库中但标注为 legacy/待 OpenRouter 重写（仅被存根编排引用，行为上失活），品牌字符串与 `cf_chrome_` 前缀改名。
7. **死代码清理：全部删除** —— `src/browser/driver_selenium_backup.py`、`backfill_bound_cards.py`、`backfill_recharge_card_display.py`、`merge_data.py`、`merge_dili.py`、`底料/` 目录、异常文件 `=3.5.5`。

## Requirements
- R1 数据隔离：所有数据落盘路径改用 OpenRouter 专属命名；`data/` 下 Cloudflare 真实数据与 `registered_accounts.txt.migrated` 删除；启动后自动生成全新空的 `openrouter_auto.db`。
- R2 库名与配置：db 默认值/运行时路径/`config.yaml`/`config.example.yaml` 全部指向 `openrouter_auto.db`；frozen 数据根改 `~/.openrouter-auto-task`。
- R3 字段改名：`accounts.cf_password`→`login_password`，`src/` 内 49 处引用同步更新，schema V1 直接以新列名建表。
- R4 品牌改名：前端标题/UI 文案、后端日志/注释、入口与打包（spec/build.py/build.yml/pyproject/.iml/release.sh）中的 Cloudflare 品牌字样改为 OpenRouter；`cf_chrome_` 临时 profile 前缀改中性名。
- R5 站点逻辑存根化：registration 4 个公共函数与 routes 开浏览器登录流程替换为清晰 TODO 存根（抛 NotImplementedError 或返回明确"未实现"结果），app/routes import 与 Web 服务启动不受影响；driver.py CF 方法标注 legacy。
- R6 死代码删除：Decision 7 所列文件/目录删除；相关 import 若有残留一并清理。
- R7 测试可跑：涉及 `login_cloudflare`/`cf_password`/Turnstile 的测试相应更新为存根/新命名，或明确标注为待站点接入时重写，`pytest` 不因命名改动而 import 崩溃。

## Acceptance Criteria
- [ ] AC1 `data/` 内不再有 `cloudflare_auto.db*`、`profiles/`、`uploads/`、`bind_report_*.xlsx` 等旧真实数据；根目录无 `registered_accounts.txt.migrated`。
- [ ] AC2 `python server.py` 能启动 Web 服务，首次启动在 OpenRouter 数据路径下生成全新空 `openrouter_auto.db`（含完整 schema），前端页面标题显示 OpenRouter Auto Task 且可正常打开。
- [ ] AC3 全仓（排除 .trellis/、打包产物 static/assets、.git）`grep -ri cloudflare` 仅可能剩不影响运行的历史注释；`cloudflare_auto.db`、`~/.cloudflare-auto-task`、`cf_password`、`cf_chrome_`、`CloudflareAutoTask` 命名不再出现在活动代码/配置/打包脚本中。
- [ ] AC4 触发注册/绑卡/充值类站点动作时，返回清晰的"OpenRouter 流程待接入"提示（NotImplementedError/结构化未实现结果），不执行任何 Cloudflare 流程、不报 import 错误。
- [ ] AC5 Decision 7 所列死代码/一次性脚本/垃圾文件已从仓库删除。
- [ ] AC6 `pytest` 收集不因命名/存根改动而 import 崩溃；站点相关测试要么适配新存根，要么明确 skip 并注明待站点接入重写。

## Out of Scope
- OpenRouter 站点实际的注册/绑卡/充值 DOM 流程实现。
- 适配 OpenRouter 实际验证码/风控机制的解题逻辑。
- 重定义 credits/invoice 等业务语义（结构保留，后续接入时再定）。
