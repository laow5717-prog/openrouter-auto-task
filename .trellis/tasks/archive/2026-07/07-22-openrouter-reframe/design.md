# Design — 改造为 OpenRouter 项目框架

## 架构边界
本轮不改变系统分层，只做三类横切改动：**数据路径隔离**、**品牌/命名替换**、**站点编排存根化**。分层保持：

```
server.py (入口)
  └─ src/web/app.py (Flask + AppState 任务编排)
       ├─ src/api/routes.py (REST)
       ├─ src/web/worker.py (WorkerPool 并发调度)  ── 站点无关，保留
       ├─ src/services/registration.py             ── 站点专属编排 → 存根化
       ├─ src/services/email.py (mail.tm 临时邮箱) ── 保留，发件人过滤参数化
       ├─ src/services/captcha.py                   ── 保留 hCaptcha 通用，Turnstile 部分随存根失活
       ├─ src/services/card.py (卡池/Excel)         ── 保留，仅路径改名
       ├─ src/browser/driver.py                     ── 保留浏览器基建，CF 方法标 legacy，品牌改名
       └─ src/models/* (SQLite DAO)                 ── 保留，字段 cf_password→login_password
```

## 关键设计决策

### D1 数据路径集中隔离
- 单一事实源优先：`src/config.py` 的 `get_data_dir()`（frozen 分支 `~/.cloudflare-auto-task`→`~/.openrouter-auto-task`）、`DatabaseConfig.path` 默认、`src/models/database.py:199` `_default_path()`、`src/web/app.py:1073` 运行时拼接，四处统一改为 `openrouter_auto.db`。
- 临时 profile 前缀 `cf_chrome_`→`openrouter_chrome_`（`driver.py:601`，备份文件将被删除故只改 driver.py）。
- 其余落盘（uploads/reports/profiles 目录名）本身站点无关，保持相对结构，仅随 `get_data_dir()` 改名自然隔离；bind_report 文件名保留（属通用报表，非品牌）。

### D2 字段改名 cf_password→login_password
- 因决策为"删除旧库、重建空库"，无需写迁移脚本：直接在 schema V1 建表语句改列名，`user_version` 迁移链不受影响（V1 是初始建表）。
- `src/` 内 49 处引用（models/database.py、models/account.py、services/registration.py、web/app.py、api/routes.py 等）机械替换 `cf_password`→`login_password`。
- 风险：前端/JSON API 若以 `cf_password` 作为键需同步；勘查未见前端直接用该键（前端用"统一密码"文案），实施时 grep 确认。

### D3 站点编排存根化（核心权衡）
- **存根边界设在编排层，不设在 driver 底层**。理由（first-principles）：本轮"跑通"的根本诉求 = Web 服务能起、UI 能开、空库能建、站点动作不跑 CF 流程且不 import 崩溃。达成此诉求的最小机制是中和**编排入口**，而非逐行删改 driver.py 的 5967 行 CF 方法。
- `src/services/registration.py`：替换为精简存根模块，保留 4 个公共函数签名（`register_one_account`/`register_and_bind_cards`/`bind_cards_to_existing_account`/`recharge_account`），函数体 `raise NotImplementedError("OpenRouter 站点流程待接入：<函数名>")`。保留必要的 import 与模块级 `print` 钩子兼容点（app.py:1025 `registration.print = hooked`）。
- `src/api/routes.py`：开浏览器登录流程（用 `login_cloudflare`，routes:430-440）改为返回明确"未实现"响应，不调用 driver 的 CF 登录。
- `src/browser/driver.py`：保留全文件（worker/存根仍需 create_driver 等基建以保证 import 与浏览器生命周期可用），CF/Turnstile/Stripe 方法群顶部加统一注释块标注 `# LEGACY Cloudflare-specific — pending OpenRouter rewrite`，只改品牌字符串与 `cf_chrome_` 前缀，不删方法。→ 这是本轮唯一"保留但失活"的大块 CF 代码，已在 PRD Decision 6 说明，评审时可否决改为更激进删除。

### D4 品牌替换范围
- 用户可见：前端 `<title>`、Vue 文案、`server.py` 头、`build.py` 启动器文案。
- 打包链：`CloudflareAutoTask.spec`→`OpenRouterAutoTask.spec`（含 `name=`）、`build.py`（`--name`/dist 路径）、`.github/workflows/build.yml`（artifact 名）、`pyproject.toml`（name/description）、`cloudflare-auto-task.iml`→`openrouter-auto-task.iml`、`release.sh`（REPO 已在别处改 remote，这里改脚本内 REPO 变量）。
- 后端注释/日志中的 "Cloudflare" 文案改 "OpenRouter"（不影响运行，但属去品牌一部分，随手改）。

### D5 死代码删除
- 删除文件：`src/browser/driver_selenium_backup.py`、`backfill_bound_cards.py`、`backfill_recharge_card_display.py`、`merge_data.py`、`merge_dili.py`、`=3.5.5`。
- 删除目录：`底料/`（含 merged_credit_cards.xlsx）。
- 删除前 grep 确认无活动代码 import 这些脚本（勘查显示均为根目录一次性脚本，driver.py 主逻辑用 patchright 版而非 selenium 备份）。

## 兼容性 / 回滚
- 全部改动在 git 版本控制内，未推送。回滚 = `git checkout .` + 恢复被删数据（数据删除不可逆，故先做 git 提交点或确认后再删）。
- 数据删除是唯一不可逆操作：实施时**先完成代码改名并本地验证服务能起新库，再删旧数据**，避免误删后无法对照。
- 无外部消费者依赖旧 db 名/字段名（原 Cloudflare 项目独立目录、独立库）。

## 验证策略
- 静态：全仓 grep 校验 Cloudflare 命名残留（AC3）。
- 动态：`python server.py` 启动 → 确认生成空 `openrouter_auto.db` 且 schema 完整（sqlite `.tables` / `PRAGMA user_version`）→ 打开前端确认标题。
- 站点动作：调用注册/充值 API，确认返回"未实现"而非执行 CF 流程。
- 回归：`pytest` 收集不崩；站点相关测试适配或 skip。
