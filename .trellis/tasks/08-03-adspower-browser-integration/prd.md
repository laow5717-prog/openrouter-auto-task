# 接入 AdsPower 指纹浏览器执行每日任务

## Goal

把每日任务（充值 / 订阅 / 注册）的浏览器启动方式，从「本地 Chrome 持久 profile + Playwright 自带 proxy 参数」
改为「AdsPower 指纹浏览器环境 + CDP 接管」。每个账号绑定一个独立的 AdsPower 环境，环境启动时绑定一个
独立代理出口 IP；AdsPower 环境数达到上限且无可用环境时，回收（删除）已充值成功账号对应的环境以腾出配额。

## 背景与实测结论

接口地址 `http://local.adspower.net:50325`，鉴权 `Authorization: Bearer <API_KEY>`（v1 的 query api_key 无效）。
以下均为本机实测结果（2026-08-03），不是文档推断：

| 事实 | 证据 |
| --- | --- |
| v2 接口可用，v1 亦需同一 Bearer 头 | `/api/v2/browser-profile/list` 返回 `code:0` |
| **环境数上限 12** | 连续创建到第 12 个报 `If the number of imported accounts exceeds the limit of 12, please delete some accounts and try again.` |
| AdsPower 代理管理里已存 100 个代理 | `/api/v2/proxy-list/list` 返回 `gateway.i-proxy.com:10000-10099`，`proxy_id` 1-100 |
| 代理占用可由服务端查询 | 每条代理带 `profile_count` 与 `related_profile_no` |
| `proxyid` 可把环境绑到指定代理 | 建环境传 `proxyid:"100"` 后出口 IP = `98.238.105.219`（非本机 IP） |
| 内置动态代理 `adspowerauto` 不通 | 建环境后所有导航 `ERR_TIMED_OUT` / `chrome-error://`，无出网能力 |
| Stripe 域名必须绕过代理 | 不加绕过：`checkout.stripe.com` → `ERR_TUNNEL_CONNECTION_FAILED`；加 `--proxy-bypass-list` 后正常 |
| Playwright 可经 CDP 接管 | `connect_over_cdp(ws.puppeteer)` 成功，`contexts[0]` 可用 |
| 请求频率受限 | 0-200 环境时 2 次/秒；`browser-profile/list`、`proxy-list` 等固定 1 秒/次 |

> 用户原先选择「AdsPower 内置动态代理」，实测该通道无出网能力；而用户在 App 内测通的"内置代理"实为
> AdsPower **代理管理列表**里保存的那 100 个 i-proxy 代理。故本任务的代理来源确定为 AdsPower 代理列表
> （经 `proxyid` 绑定），**不读本地 DB 的 `proxies` 表**。

## Requirements

### R1 环境即账号（一账号一环境）

- 每个账号（email）唯一对应一个 AdsPower 环境，映射关系持久化到本地 DB，进程重启后仍可复用同一环境（保住登录态）。
- 环境命名与备注需可溯源到账号，便于在 AdsPower 客户端里人工核对。
- 同一账号并发排他仍由现有 `AccountRegistry` 保证；不得出现两个 worker 同时启动同一环境。

### R2 环境绑定独立代理

- 创建环境时从 AdsPower 代理列表挑一个**未被任何环境占用**的代理（`profile_count == 0`），用 `proxyid` 绑定。
- 代理占用以 AdsPower 服务端 `profile_count` / `related_profile_no` 为准，不再用本地 `ProxyRegistry` 内存态判定代理是否空闲。
- 代理列表为空或全部被占用时，任务需给出明确日志，而不是静默退化为直连。

### R3 配额耗尽时回收环境

- 环境创建失败且原因是配额上限时，按「已充值成功的账号」优先删除其环境释放配额，然后重试创建。
- 回收候选顺序：`recharged` → `archived` → `subscribed` → `flagged`/`banned`/`suspended`（终态账号），
  且必须排除当前正在被任何 worker 占用的账号。
- 删除环境同时删除本地映射记录，代理占用由 AdsPower 自动释放。
- 一轮回收后仍无法创建，任务需以明确错误结束该账号，不得无限重试。

### R4 Stripe 可达

- 环境启动时通过 `launch_args` 传 `--proxy-bypass-list`，让 Stripe 域名直连，保持现有付款链路可用。
- 绕过域名集合与现行 `_PROXY_BYPASS` 语义一致。

### R5 保住既有能力

- hCaptcha token 前置注入（`add_init_script`）仍须生效——付款链路依赖它。
- 现有 `BrowserSession` 对外契约（`get` / `get_screenshot_as_png` / `title` / `quit` / `.page` / `.context`）不变，
  下游 50+ 个 driver 函数与 `opencode_billing` / `opencode_login` / `opencode_subscribe` 无需改动。
- worker 的实时截图流、停止响应、日志分栏行为不变。

### R6 可回退

- 通过配置开关在「AdsPower 模式」与「本地 Chrome 模式」之间切换，开关关闭时行为与现状逐字一致。

## Constraints

- AdsPower 环境上限 12，而账号数远超 12 —— 环境是**稀缺资源**，必须池化 + 回收，不能一账号一直占着。
- 接口有频率限制（部分 1 秒/次），并发 worker 调用需串行化 + 限流，否则会收到 `Too many request per second`。
- AdsPower 客户端必须在本机运行且已登录，否则接口不可用；需有可读的失败提示。
- 删除环境会丢失该账号在 AdsPower 里的登录态 —— 只对已完成（充值成功等终态）的账号执行。
- 代理只有 100 个而环境最多 12 个，代理不是瓶颈；瓶颈是环境配额。

## Acceptance Criteria

- [ ] AC1 开启 AdsPower 模式后，每日充值任务能为账号创建/复用 AdsPower 环境并完成一次完整的「登录 → 读余额 → Stripe 付款」流程。
- [ ] AC2 同一账号第二次执行时复用同一 `profile_id`（不新建环境），日志中可见「复用环境」。
- [ ] AC3 两个并发 worker 取到的是两个不同的环境与两个不同的代理出口 IP，`proxy-list` 中对应代理 `profile_count` 各为 1。
- [ ] AC4 环境配额打满（12 个）时，任务自动删除已 `recharged` 账号的环境并成功创建新环境继续跑，日志中可见回收明细。
- [ ] AC5 无任何可回收环境时，账号以明确错误（非异常堆栈）结束，任务继续处理其他账号，不卡死不无限重试。
- [ ] AC6 AdsPower 环境内 `checkout.stripe.com` 可打开（不受代理封锁影响），`api.ipify.org` 返回的是代理出口 IP 而非本机 IP。
- [ ] AC7 关闭 AdsPower 模式后，任务走原 `create_driver_vanilla` 路径，行为与改造前一致。
- [ ] AC8 AdsPower 客户端未启动 / API Key 错误时，任务给出可读中文错误并安全结束，不产生孤儿环境。
- [ ] AC9 任务被用户停止或异常退出后，本次运行启动过的环境都会被 stop（不残留后台 Chrome）。

## Notes

- 本任务只改「浏览器怎么起来」这一层，不改任何 opencode/Stripe 页面流程。
- `data/profiles/<email>` 本地 profile 目录在 AdsPower 模式下不再使用，但保留不删（供回退）。
