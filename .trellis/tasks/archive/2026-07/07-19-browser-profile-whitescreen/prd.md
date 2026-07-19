# 修复浏览器 profile 泄漏导致的白屏

## 背景

用户反馈：执行每日任务期间，自动打开的浏览器窗口访问页面总是白屏——地址栏 URL 正常，
但页面内容不渲染。

现场取证（2026-07-19，用户机器）：

| 证据 | 数值 | 含义 |
|------|------|------|
| Chrome 进程数 | 41 | 远超实际窗口数 |
| `server.log` 浏览器初始化成功 | 55 次 | — |
| `server.log` 正在关闭浏览器 | 48 次 | 至少 7 个 driver 泄漏未关 |
| `data/profiles` 总量 | 2.7G | — |
| 单 profile 最大 | 695M | `fl58bpop4a@web-library.net` |
| 该 profile 内 Cache / Code Cache / Service Worker | 248M / 188M / 133M | 缓存腐坏高发区 |

## 成因分析

### 主因 A：Service Worker 缓存腐坏（对应「URL 正常但不渲染」）

`dash.cloudflare.com` 是注册了 Service Worker 的 SPA。SW 拦截导航请求走缓存，
一旦缓存条目损坏（Chrome 被强杀时高发），SW 返回空响应——地址栏 URL 正常、页面全白。
Chrome 不会自愈。133M 的 Service Worker 目录说明缓存从未被清理过。

现有代码只对 `Default/Preferences` 做 >10MB 检测（`src/browser/driver.py:443-450`），
完全不管 Cache / Code Cache / Service Worker / GPUCache。

### 主因 B：孤儿 Chrome 进程与新实例抢占同一 profile

`src/browser/driver.py:431-441` 的 `_clear_singleton_locks()` **无条件**删除
`SingletonLock` / `SingletonCookie` / `SingletonSocket`，注释里写的前提是：

> 本应用通过 open_browsers 保证同一 profile 单实例，可安全清理

该前提在存在孤儿进程时不成立。泄漏的 Chrome 仍占着 user-data-dir，新实例强删锁后照样
启动，两个实例争抢同一份 leveldb（Cookies / Local Storage），渲染进程起不来 → 白屏。

### 泄漏来源：`quit()` 静默吞异常

`src/browser/driver.py:207-225` 中 `context.close()` 的异常被 `except Exception: pass`
全吞。close 失败时 Chrome 进程不会退出，而调用方（`registration.py` 各 finally 的
`close_driver`）拿不到任何信号，日志里只留下「初始化 55 / 关闭 48」这种事后才看得出的差值。

`playwright.stop()` 紧随其后拆掉 driver 传输通道，此后再没有任何途径能关掉那个 Chrome。

### 非成因（已排除，不要改）

- **不是清 cookie / 删 profile**：全仓 grep 无 `clear_cookies` / `about:blank` /
  `storage_state` / `context.clear_*`；`shutil.rmtree` 只作用于临时 profile 分支
  （`_temp_profile is not None`），持久化 profile 不会被删。
- **不是 `worker.clear_active_driver()` 不 quit**：driver 生命周期归 `registration.py`
  各 finally 管，worker 只持引用用于截图，丢引用是正确设计。
- **`_safe_goto` 用 `domcontentloaded`** 会放大「看起来慢」的观感，但 URL 正常且
  内容永久不渲染不是它造成的，本任务不动导航等待策略。

## 需求

### R1 profile 缓存自动清理

浏览器启动前，若该 profile 的缓存类目录合计超过阈值，整体删除后再启动。

- 清理目标：`Default/Cache`、`Default/Code Cache`、`Default/Service Worker`、`Default/GPUCache`
- 登录态必须完好：`Cookies`、`Login Data`、`Local Storage`、`Local State` 一律不碰
- 阈值可配置，默认 200MB

### R2 孤儿进程检测与回收

`_clear_singleton_locks()` 删锁前先确认无进程占用该 user-data-dir；发现孤儿则先终止
并等待其退出，再删锁。删锁的安全前提由「已验证无占用」保证，而非注释里的假设。

### R3 关闭失败可见且有兜底

- `quit()` 中 `context.close()` 失败必须打印可见日志（经 `_hooked_print` 进 Web 日志）
- close 后确认 Chrome 进程确已退出；未退出则终止进程，避免泄漏

## 验收标准

1. 清掉现存缓存后，每日任务运行中自动打开的窗口不再白屏（用户实跑确认）
2. 对一个 >200MB 的 profile 调 `create_driver`，启动前日志出现缓存清理提示，
   且启动后该账号仍处于登录态（无需重新输密码）
3. 手动 `kill -9` 掉一个 Chrome 制造孤儿后再 `create_driver` 同 profile，
   日志出现孤儿回收提示，浏览器正常启动且页面正常渲染
4. 跑一轮每日任务后，`server.log` 中「浏览器初始化成功」与「正在关闭浏览器」次数一致；
   `ps aux | grep -c "[G]oogle Chrome"` 回落到正常水平
5. 现有注册 / 绑卡 / 充值流程行为不变（无回归）

## 约束

- **不引入新依赖**：psutil 未安装且不安装，进程查询走标准库 `subprocess` + `ps`
- 目标平台 macOS（`darwin`），实现需在 Linux 上也不报错（降级为不做进程检测）
- 不改动 `_safe_goto` 的 `wait_until` 策略
- 不改动 `AccountRegistry` 的排他语义（并发三处排他见项目记忆 parallel-execution.md）
