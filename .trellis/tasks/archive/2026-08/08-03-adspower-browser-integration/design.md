# 技术设计：AdsPower 指纹浏览器接入

## 1. 边界与总体形状

改动集中在「浏览器怎么起来 / 怎么关掉」这一层，页面流程零改动。

```
run_daily_pipeline (app.py)
        │  领账号 + 领代理(旧) ──────────────► 改为：只领账号
        ▼
_recharge_one_account / _subscribe_one_account / _register_one_account
        │  proxy=dict 透传
        ▼
registration.recharge_account
        │  create_driver_vanilla(profile_id=email, proxy=proxy)
        ▼                                    ┌──────────────────────────┐
   BrowserSession  ◄──────────────────────── │ 新：open_browser(email)  │
   （对外契约不变）                            │  AdsPower 或 本地 Chrome │
                                             └──────────────────────────┘
```

新增两个模块，其余文件只做接线：

| 文件 | 职责 |
| --- | --- |
| `src/services/adspower.py`（新） | AdsPower HTTP 客户端：限流、鉴权、创建/启动/停止/删除环境、代理列表查询。纯 API 层，不认识"账号"。 |
| `src/browser/adspower_driver.py`（新） | 环境池编排 + CDP 接管：账号↔环境映射、配额回收、`connect_over_cdp` 组装 `BrowserSession`。 |
| `src/models/adspower_profile.py`（新） | `adspower_profiles` 表：email ↔ profile_id 映射与元信息。 |
| `src/browser/driver.py` | 新增 `create_driver_adspower()`；`BrowserSession` 增加 `remote` 模式分支（关闭时不杀本地进程、不删目录）。 |
| `src/web/app.py` | 代理领取逻辑改为「AdsPower 模式下不领本地代理」；收尾时停掉本次启动的环境。 |
| `src/config.py` / `config.yaml` | 新增 `adspower` 配置段。 |

## 2. 数据契约

### 2.1 新表 `adspower_profiles`

```sql
CREATE TABLE IF NOT EXISTS adspower_profiles (
    email        TEXT PRIMARY KEY,
    profile_id   TEXT NOT NULL UNIQUE,   -- AdsPower 环境 ID，如 k1fbc2m0
    profile_no   TEXT,                   -- 环境编号，人工核对用
    proxy_id     TEXT,                   -- 绑定的 AdsPower 代理 ID
    created_at   TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    last_used_at TIMESTAMP
);
```

email 作主键 = R1「一账号一环境」的结构性保证，而不是靠调用方自觉。
`profile_id` 加 UNIQUE，防止两条映射指向同一环境（那会绕过 `AccountRegistry` 的单实例约束）。

**本地映射可能与 AdsPower 实际状态不一致**（用户在客户端手工删了环境）。启动前若 `start` 返回
"环境不存在"，删本地映射并重新走创建流程，而不是报错退出。

### 2.2 AdsPower API 契约（实测确认）

```
POST /api/v2/browser-profile/create
  body: {name, group_id, proxyid, fingerprint_config, remark}
  → {code:0, data:{profile_id, profile_no}}
  配额满: {code:-1, msg:"If the number of imported accounts exceeds the limit of 12, ..."}

POST /api/v2/browser-profile/start
  body: {profile_id, headless:"0", cdp_mask:"1", proxy_detection:"0", launch_args:[...]}
  → {code:0, data:{ws:{puppeteer,selenium}, debug_port, webdriver}}

POST /api/v2/browser-profile/stop    body: {profile_id} → {code:0}
POST /api/v2/browser-profile/delete  body: {profile_id:[...]} → {code:0}   单次上限 100
GET  /api/v2/browser-profile/active?profile_id=xxx → {data:{status:"Active"|"Inactive", ws, debug_port}}
POST /api/v2/proxy-list/list  body:{page,limit} → {data:{list:[{proxy_id,host,port,profile_count,related_profile_no}], total}}
```

鉴权统一 `Authorization: Bearer <key>`。返回 `code != 0` 一律视为失败并把 `msg` 原样带进异常。

## 3. 关键设计决策

### D1 代理来源：AdsPower 代理列表，服务端占用为准

用 `proxyid` 在**建环境时**绑定，一次绑定长期有效；不再每次运行领代理。
挑选规则：`profile_count == 0` 优先（完全未占用），全被占用时取 `profile_count` 最小者并记警告日志。

**为什么不用本地 `ProxyRegistry`**：环境与代理的绑定关系存活在 AdsPower 服务端且跨进程持久，
用内存注册表判定"空闲"会在重启后立刻失真。`profile_count` 是唯一权威。

**为什么不用 `adspowerauto`**：实测无出网能力（见 prd 表格）。配置里保留 `proxy_mode: proxy_list | adspower_auto | none`，
默认 `proxy_list`，但代码不为 `adspower_auto` 的不可用性做兼容兜底。

### D2 Stripe 绕过：靠 `launch_args`，而不是 Playwright proxy 参数

AdsPower 模式下 Playwright 是 CDP 客户端，`launch_persistent_context(proxy=...)` 那条路不存在，
`_PROXY_BYPASS` 无处可传。改为在 `start` 时传：

```
--proxy-bypass-list=*.stripe.com;stripe.com;*.stripecdn.com;*.stripe.network;*.stripecdn.com
```

注意分隔符是**分号**（Chrome 原生格式），不是现行 `_PROXY_BYPASS` 用的逗号（Playwright 格式）。
两份常量必须分开定义，不能直接复用字符串——实测不加绕过即 `ERR_TUNNEL_CONNECTION_FAILED`，
这条是付款链路的生死线。

### D3 `BrowserSession` 增加 remote 模式，而不是新造一个会话类

新增构造参数 `remote_stop=<callable|None>`。为 `None` 时行为与现在逐字一致；非 `None` 时 `quit()`：

- 不执行 `_kill_chrome_for_profile`（AdsPower 的 Chrome 不归我们管，按 profile 目录杀进程会误伤）
- 不删 profile 目录（`temp_profile=None`）
- `context.close()` 换成 `browser.close()`（断开 CDP 连接，不关浏览器）
- 最后调 `remote_stop()` 让 AdsPower 关环境

看门狗保留但降级：CDP 断连不会像本地 Chrome 那样把线程钉死，超时后直接调 `remote_stop()` 即可。

**为什么复用而不是新建类**：下游 50+ 个 driver 函数、`opencode_billing`、`worker.make_monitor`
全部按 `BrowserSession` 的字段写死（`.page` / `.net_responses` / `_last_png`），新建类等于复制这些字段，
两份实现迟早漂移。

### D4 CDP 接管后的页面选取

实测 `connect_over_cdp` 后 `contexts[0].pages` 里混着 `about:blank` 和一个 `devtools://` 页面，
直接取 `pages[0]` 会拿到已被关闭的初始页（实测报 `Target page, context or browser has been closed`）。

规则：过滤掉 `devtools://` 开头的页；有可用页取第一个，否则 `context.new_page()`。
接管后 `sleep(2)` 等 AdsPower 的初始化标签页稳定——这不是玄学等待，是 AdsPower 启动时会替换初始 tab。

### D5 配额回收：懒回收 + 单锁串行

不做后台清理线程。只在 `create` 返回配额错误时触发一次回收，理由：环境删了就丢登录态，
主动清理等于主动作废账号；配额是硬约束，只在真撞上时付这个代价最省。

回收候选 SQL（在 `adspower_profiles` 与 `accounts` 上 join）：

```sql
SELECT p.email, p.profile_id FROM adspower_profiles p
JOIN accounts a ON a.email = p.email
WHERE a.status IN ('recharged','archived','subscribed','flagged','banned','suspended')
ORDER BY CASE a.status WHEN 'recharged' THEN 0 WHEN 'archived' THEN 1
                       WHEN 'subscribed' THEN 2 ELSE 3 END,
         p.last_used_at ASC
```

再由调用方剔除 `AccountRegistry` 当前占用中的 email。一次回收删够 `reclaim_batch`（默认 3）个，
删完重试创建一次；仍失败则该账号本轮以 `failed` 结束（AC5）。

**整个「创建/回收」路径由一把 `threading.Lock` 串行化**：并发 worker 同时撞配额上限时，
若各自回收各自重试，会出现 A 删的环境刚好被 B 用掉、A 再次失败的活锁。串行化后一次只有一个 worker 在争配额。

### D6 限流：客户端侧最小间隔

AdsPower 对 0-200 环境限 2 次/秒，部分接口固定 1 秒/次。客户端内置一把锁 + 上次调用时间戳，
对 `browser-profile/list` / `proxy-list/*` 用 1.1s 间隔，其余用 0.55s 间隔。
实测并发裸调会返回 `{"code":-1,"msg":"Too many request per second, please check"}`。

限流放在 `adspower.py` 客户端内部而非调用方，是因为调用点分散在多个 worker 线程，
调用方自觉排队守不住。

### D7 指纹与环境参数

创建环境固定参数：

```python
{
  "name": f"auto-{email}",
  "remark": "openrouter-auto-task 自动创建",
  "group_id": <配置，默认 "0">,
  "proxyid": <挑选出的 proxy_id>,
  "fingerprint_config": {
      "automatic_timezone": "1",          # 时区跟随代理 IP，避免时区/IP 打架
      "webrtc": "disabled",               # 防 WebRTC 泄漏真实 IP
      "browser_kernel_config": {"version": "latest", "type": "chrome"},
      "random_ua": {"ua_system_version": ["Windows 10", "Windows 11"]},
  },
}
```

`language` 不在此处强制——AdsPower 会按 IP 推断；页面英文由现有的
`set_extra_http_headers({"Accept-Language": ...})` 保证（CDP 接管后照样能设）。

## 4. 数据流：一次充值的完整链路

```
_produce()            领账号（AdsPower 模式下不再领本地代理）
   └─► _do(worker, item)
        └─► _recharge_one_account(email, ...)
             └─► registration.recharge_account(email, ..., use_adspower=True)
                  └─► driver.create_driver_adspower(email)
                       ├─ pool.ensure_profile(email)          # 查映射 → 无则挑代理 → create（撞配额则回收重试）
                       ├─ client.start(profile_id, launch_args=[bypass])
                       ├─ playwright(vanilla).connect_over_cdp(ws)
                       ├─ 选页 / set_extra_http_headers / page.on("response")
                       └─ BrowserSession(remote_stop=lambda: client.stop(profile_id))
                  ── 页面流程完全不变 ──
                  └─► close_driver(session) → quit() → CDP 断开 + client.stop()
```

## 5. 兼容与回退

- `config.yaml` 新增 `adspower.enabled`（默认 `false`）。为 `false` 时 `recharge_account` 等仍调
  `create_driver_vanilla`，代码路径与现状完全一致 —— 这是 R6/AC7 的保证。
- 旧的 `proxies` 表、`ProxyModel`、`ProxyRegistry` 全部保留不动，仅在 AdsPower 模式下不参与调度。
  不删除是因为回退路径还依赖它们。
- 打包（`build.py` / `.spec`）无需改动：新增模块无二进制依赖，只用 `urllib`。

## 6. 失败模式与处理

| 失败 | 现象 | 处理 |
| --- | --- | --- |
| AdsPower 客户端未启动 | `urlopen` connection refused | 抛 `AdsPowerUnavailable`，日志「AdsPower 客户端未运行或接口未开启（http://local.adspower.net:50325）」，账号记 failed |
| API Key 错误 | `{"code":-1,"msg":"Require api-key"}` | 同上，提示检查配置 |
| 配额满且无可回收 | create 返回 limit 文案 | 账号记 failed 并给出「环境配额已满且无可回收环境」日志，任务继续下一个账号 |
| 代理列表为空 | `proxy-list` total=0 | 拒绝创建（不静默直连），日志提示先在 AdsPower 代理管理里导入代理 |
| 本地映射指向已删环境 | start 返回环境不存在 | 删本地映射，重新创建 |
| 环境已被手工打开 | start 返回已运行的 ws | 直接接管（AdsPower 对已运行环境返回同一 ws），不报错 |
| 任务中断 | 用户点停止 | `finally` 里遍历本次运行 started 集合逐个 `stop`（AC9） |

## 7. 明确不做

- 不做环境的预热/预创建池（配额只有 12，预创建会挤掉正在用的）。
- 不做跨机器（cloud-active）环境调度。
- 不迁移 `data/profiles/<email>` 里已有的 Cookie 到 AdsPower —— 已登录账号在 AdsPower 环境里需重新走一次 OAuth，
  这是可接受的一次性成本（GitHub 密码在 DB 里，登录流程本就是自动的）。
- 不改任何 opencode / Stripe 页面选择器。
