# Implement — 改造为 OpenRouter 项目框架

执行顺序遵循"先改代码并验证空库可起，再删不可逆数据"的原则（design D3/回滚）。

## 阶段 0：安全基线
- [ ] 0.1 建立回滚点：确认当前工作区改动（`git status`），提交或记录基线（不推送）。数据删除不可逆，需有对照。

## 阶段 1：命名 —— 数据路径与库名（R1/R2）
- [ ] 1.1 `src/config.py`：frozen 数据根 `~/.cloudflare-auto-task`→`~/.openrouter-auto-task`（:158）；`DatabaseConfig.path` 默认（:86）与 YAML 回退（:286）→ `data/openrouter_auto.db`。
- [ ] 1.2 `src/models/database.py:199` `_default_path()` → `openrouter_auto.db`。
- [ ] 1.3 `src/web/app.py:1073` 运行时 `db_path` → `openrouter_auto.db`。
- [ ] 1.4 `config.yaml:60` 与 `config.example.yaml`（如无 db 段则补/或保持靠默认）→ `openrouter_auto.db`。
- [ ] 1.5 `src/browser/driver.py:601` 临时 profile 前缀 `cf_chrome_`→`openrouter_chrome_`。
- 验证：`grep -rn "cloudflare_auto.db\|.cloudflare-auto-task\|cf_chrome_" src/ config*.yaml` 无残留。

## 阶段 2：命名 —— 字段 cf_password→login_password（R3）
- [ ] 2.1 `src/models/database.py:16` schema V1 建表列名 `cf_password`→`login_password`。
- [ ] 2.2 `src/` 内 49 处 `cf_password` 引用机械替换（models/account.py、services/registration.py、web/app.py、api/routes.py 等）。用 `grep -rn cf_password src/` 逐一核对。
- [ ] 2.3 grep 前端 `frontend/src/` 是否以 `cf_password` 为 API 键；若有同步改。
- 验证：`grep -rn "cf_password" src/ frontend/src/` 为空。

## 阶段 3：站点编排存根化（R5）
- [ ] 3.1 `src/services/registration.py`：替换为存根模块，保留 4 个公共函数签名，函数体 `raise NotImplementedError("OpenRouter 站点流程待接入：<name>")`；保留模块级 `print` 钩子兼容（app.py:1025）。备份原实现到任务目录 research/ 或 git 历史即可（不留在 src）。
- [ ] 3.2 `src/api/routes.py` 开浏览器登录流程（:430-440，用 login_cloudflare）→ 返回明确"未实现"响应，不调 CF 登录。
- [ ] 3.3 `src/browser/driver.py`：CF/Turnstile/Stripe 方法群顶部加 `# LEGACY Cloudflare-specific — pending OpenRouter rewrite` 注释；改品牌字符串。方法不删。
- [ ] 3.4 `src/services/email.py:182,286` 发件人过滤 `'cloudflare'` 参数化/改为 OpenRouter 占位（本轮可先改字符串并留 TODO）。
- [ ] 3.5 `src/utils.py:185-188` CF 邮件验证链接正则标 TODO/占位（后续按 OpenRouter 邮件格式重写）。
- 验证：`python -c "import src.web.app"` 不报 import 错误。

## 阶段 4：品牌与打包改名（R4）
- [ ] 4.1 前端：`frontend/index.html:6` title、`SidebarControls.vue:11`、`Workbench.vue:54,155`、`CardPool.vue:5` 文案 → OpenRouter。
- [ ] 4.2 入口/文案：`server.py:2`、`src/config.py:21,31` 注释、`build.py:147-179` 启动器文案。
- [ ] 4.3 打包：`CloudflareAutoTask.spec`→`OpenRouterAutoTask.spec`（含 name=）；`build.py:34,53,104-108`（spec 路径/`--name`/dist 目录）；`.github/workflows/build.yml:17,20` artifact 名；`pyproject.toml:2,4` name/description；`cloudflare-auto-task.iml`→`openrouter-auto-task.iml`；`release.sh:5` REPO。
- [ ] 4.4 前端若改文案需重新 `npm run build` 生成 static/assets（否则打包产物仍是旧文案）——评估本轮是否重构建，或标注 static/assets 待重构建。
- 验证：`grep -ril cloudflare`（排除 .trellis/、.git/、static/assets 旧产物）仅剩历史注释。

## 阶段 5：死代码删除（R6）
- [ ] 5.1 grep 确认无活动 import：`grep -rn "driver_selenium_backup\|backfill_\|merge_data\|merge_dili" src/ server.py`。
- [ ] 5.2 删除：`src/browser/driver_selenium_backup.py`、`backfill_bound_cards.py`、`backfill_recharge_card_display.py`、`merge_data.py`、`merge_dili.py`、`=3.5.5`、`底料/`。

## 阶段 6：删除旧真实数据（R1，不可逆，最后做）
- [ ] 6.1 先完成阶段 1-5 并通过阶段 7 的启动验证（确认新库能建），再执行删除。
- [ ] 6.2 删除 `data/cloudflare_auto.db*`（含 wal/shm/所有 .bak）、`data/profiles/`、`data/uploads/`、`data/bind_report_*.xlsx`、根目录 `registered_accounts.txt.migrated`。保留 `data/` 空目录（含 .gitkeep 如需要）。

## 阶段 7：验证（AC1-AC6）
- [ ] 7.1 `python server.py` 启动，确认无异常、生成全新空 `openrouter_auto.db`。
- [ ] 7.2 sqlite 校验：`PRAGMA user_version` 与 `.tables` 完整，`accounts` 表有 `login_password` 列、无 `cf_password`。
- [ ] 7.3 打开前端，标题显示 OpenRouter Auto Task。
- [ ] 7.4 调用注册/充值 API，确认返回"未实现"提示，不跑 CF 流程。
- [ ] 7.5 `pytest` 收集不崩；站点相关测试（test_bind_retry/test_turnstile_injection/test_card_fault）适配存根或 skip 并注明。
- [ ] 7.6 全仓 grep 终检（AC3）。

## 风险与回滚点
- 最高风险：阶段 6 数据删除不可逆 → 严格置于最后且在启动验证通过后。
- 中风险：`cf_password` 49 处替换遗漏 → 以 grep 归零为准。
- 中风险：driver.py 存根边界选择（保留 legacy 方法）若评审不接受，需回到设计调整为更激进删除。
- 前端 static/assets 旧产物含 cloudflare 文案：若本轮不重构建，AC3 需豁免 static/assets 或安排 npm build。
