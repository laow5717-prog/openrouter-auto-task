# 执行计划：AdsPower 指纹浏览器接入

按依赖顺序分 6 步，每步都能独立验证。步 1-3 是自底向上的新增（不碰现有路径），
步 4-5 才接线到主流程，步 6 收尾。任何一步失败都可以停在上一步，因为 `adspower.enabled=false`
时全部新代码不被调用。

---

## 步骤 1：AdsPower API 客户端

**文件**：`src/services/adspower.py`（新增）

- [ ] `AdsPowerError` / `AdsPowerUnavailable` / `AdsPowerQuotaExceeded` 三个异常
- [ ] `AdsPowerClient(base_url, api_key)`：`_call(path, body, min_interval)` 统一做鉴权头、JSON、
      限流（模块级锁 + 上次调用时间戳）、`code != 0` → 异常、connection refused → `AdsPowerUnavailable`
- [ ] 配额文案识别：`msg` 含 `exceeds the limit` → `AdsPowerQuotaExceeded`
- [ ] 方法：`list_profiles(page, page_size)`、`create_profile(payload)`、`start_profile(profile_id, launch_args)`、
      `stop_profile(profile_id)`、`delete_profiles(ids)`、`profile_active(profile_id)`、`list_proxies(page, limit)`
- [ ] 限流常量：`_SLOW_PATHS`（list 类）1.1s，其余 0.55s

**验证**：
```bash
python3 -c "
from src.services.adspower import AdsPowerClient
c = AdsPowerClient('http://local.adspower.net:50325','d6c9f30e05d110574e05d6dd36ec011d0096c27f7f323f4f')
print(c.list_profiles(1,5)['total_count']); print(len(c.list_proxies(1,10)))"
```
预期：打印环境总数（当前 1）与代理条数（10）。

---

## 步骤 2：映射表模型

**文件**：`src/models/adspower_profile.py`（新增）、`src/models/database.py`（加建表）

- [ ] 建表 SQL 见 design.md §2.1，放进 `database.py` 现有的建表流程（与 `proxies` 表同处）
- [ ] `AdsPowerProfileModel`：`get_by_email`、`upsert(email, profile_id, profile_no, proxy_id)`、
      `delete_by_email`、`touch(email)`、`reclaim_candidates(statuses, limit)`（join accounts，排序见 design D5）、`count`
- [ ] 在 `create_app` 的 `models` dict 里注册为 `'adspower_profile'`

**验证**：`python3 -c "..."` 建库后插一条、查一条、删一条；确认 `profile_id` UNIQUE 约束生效（重复插入报错）。

---

## 步骤 3：环境池 + CDP 接管

**文件**：`src/browser/adspower_driver.py`（新增）、`src/browser/driver.py`（改）

- [ ] `AdsPowerProfilePool(client, profile_model, account_registry, log)`
  - `_lock`：整条「挑代理 → create → 撞配额 → 回收 → 重试」路径串行（design D5）
  - `pick_free_proxy()`：翻 `list_proxies` 全部页，`profile_count==0` 优先，否则最小值 + 警告日志
  - `ensure_profile(email)` → `(profile_id, proxy_id)`：查映射命中即返回；否则挑代理 + create；
    `AdsPowerQuotaExceeded` → `reclaim(exclude=占用中的 email)` → 重试一次；再失败原样抛出
  - `reclaim(exclude, limit)`：查候选、剔除占用中、`delete_profiles` + 删本地映射、返回删除数与明细日志
  - `release(email)`：删环境 + 删映射（供外部主动回收）
- [ ] `driver.py` 新增 `create_driver_adspower(profile_id_email, pool, client)`：
  - `ensure_profile` → `start_profile(launch_args=[_ADSPOWER_PROXY_BYPASS_ARG])`
  - 映射失效（start 报环境不存在）→ 删映射 + 重来一次
  - `vanilla_sync_playwright().start()` → `connect_over_cdp(ws.puppeteer)`
  - 选页：过滤 `devtools://`，无可用页则 `new_page()`；接管后短暂等待（design D4）
  - `set_default_timeout` / `set_default_navigation_timeout` / `set_extra_http_headers` / `page.on("response")`
    —— 与 `create_driver_vanilla` 保持一致
  - 返回 `BrowserSession(..., remote_stop=lambda: client.stop_profile(pid))`
- [ ] `BrowserSession.__init__` 增加 `remote_stop=None`；`quit()` 按 design D3 分支
- [ ] 新常量 `_ADSPOWER_PROXY_BYPASS_ARG`（分号分隔，**不复用** `app.py` 的 `_PROXY_BYPASS`）

**验证**（脚本 `scripts/probe_adspower.py`，新增）：
```bash
python3 scripts/probe_adspower.py --email probe@example.com
```
预期输出：创建/复用环境 → 出口 IP 非本机 → `checkout.stripe.com` 可达 → `quit()` 后
`browser-profile/active` 返回 `Inactive`。

---

## 步骤 4：配置开关

**文件**：`src/config.py`、`config.yaml`、`config.example.yaml`

- [ ] `AdsPowerConfig` dataclass：`enabled=False`、`base_url="http://local.adspower.net:50325"`、
      `api_key=""`、`group_id="0"`、`proxy_mode="proxy_list"`、`reclaim_batch=3`
- [ ] 挂到 `AppConfig`，在 `ConfigLoader._parse_config` 里解析
- [ ] `config.yaml` / `config.example.yaml` 写入 `adspower:` 段（api_key 留空，注释说明从客户端复制）

**验证**：`python3 -c "from src.config import cfg; print(cfg.adspower)"`，改 yaml 后 `cfg.reload()` 值随之变化。

---

## 步骤 5：接线到每日任务

**文件**：`src/services/registration.py`、`src/web/app.py`

- [ ] `registration.recharge_account` 新增 `browser_factory=None` 参数：为 `None` 时走
      `create_driver_vanilla(profile_id=email, proxy=proxy)`（现状），否则 `browser_factory(email)`。
      **只改这一处会话创建，其余逻辑一行不动。**
- [ ] `app.py` 加 `self.adspower_pool`（惰性构造，`cfg.adspower.enabled` 为假时恒为 `None`）
      与 `self._adspower_started`（本次运行启动过的 profile_id 集合，带锁）
- [ ] `_browser_factory()`：返回一个闭包供 `recharge_account` / `_subscribe_one_account` 使用；
      内部登记 `_adspower_started`
- [ ] `run_daily_pipeline._acquire_proxy_for`：AdsPower 模式下直接返回 `(None, None)` 并在启动摘要里
      打印「代理由 AdsPower 环境绑定（proxy_list）」而不是「未配置代理，直连」
- [ ] `_subscribe_one_account` / `_register_one_account`（`github_signup_service.signup_one`）同样接入
      `browser_factory`；注册路径原用 Patchright，AdsPower 模式下统一走 CDP 接管
- [ ] `run_daily_pipeline` / `run_daily_subscribe_pipeline` 的 `finally`：遍历 `_adspower_started` 逐个
      `stop_profile`（吞异常、记日志），然后清空（AC9）

**验证**：`adspower.enabled=false` 跑一次现有测试确认零回归；置 `true` 后跑单账号充值，
观察日志出现「AdsPower 环境 <pid>（代理 <proxy_id>）」。

---

## 步骤 6：测试与文档

**文件**：`tests/test_adspower_pool.py`（新增）、`.trellis/spec/backend/*`

- [ ] 单测（mock `AdsPowerClient`，不打真实接口）：
  - `ensure_profile` 命中映射时不调 `create`
  - 撞配额 → 触发 `reclaim` → 重试成功
  - `reclaim` 排除占用中的 email
  - 无可回收候选时原样抛 `AdsPowerQuotaExceeded`
  - `pick_free_proxy` 在全部占用时选 `profile_count` 最小者
- [ ] `BrowserSession.quit()` remote 分支：不调 `_kill_chrome_for_profile`、调 `remote_stop`
- [ ] 跑既有测试套件确认零回归：`python3 -m pytest tests/ -q`
- [ ] spec 更新：把「AdsPower 环境是稀缺资源（上限 12）」「Stripe 必须 `--proxy-bypass-list` 绕过」
      「代理占用以服务端 `profile_count` 为准」三条写进 `.trellis/spec/backend/`

---

## 验证命令汇总

```bash
python3 -m pytest tests/ -q                      # 全量回归
python3 scripts/probe_adspower.py --email x@y.z  # 端到端环境启停 + 出口 IP + Stripe 可达
python3 -c "from src.config import cfg; print(cfg.adspower)"
```

## 回滚点

| 回滚到 | 做法 |
| --- | --- |
| 完全回退 | `config.yaml` 里 `adspower.enabled: false` —— 无需改代码 |
| 代码回退 | 步 1-4 都是纯新增文件，`git revert` 步 5 的接线 commit 即可 |
| 环境清理 | `scripts/probe_adspower.py --cleanup` 删除所有 `remark` 为本项目的环境 |

## 评审关口

- 步 3 完成后先人工验一次真实账号的完整充值链路（hCaptcha 注入是否仍生效），再进步 5。
  hCaptcha 前置注入是历史上最脆弱的一环（见 memory: stripe-hcaptcha-blocker），CDP 接管后必须实测确认，
  不能靠推断。
- 步 5 接线前确认 `adspower.enabled=false` 下全量测试通过（零回归是接线的前提）。
