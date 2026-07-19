# 技术设计

## 改动边界

全部改动集中在 `src/browser/driver.py` 与 `src/config.py`。
**不改** `registration.py`、`worker.py`、`app.py`、`routes.py` —— 它们的 driver
生命周期调用点（`create_driver` / `close_driver`）语义不变，修复对调用方透明。

| 文件 | 改动 |
|------|------|
| `src/config.py` | 新增缓存清理阈值常量 |
| `src/browser/driver.py` | 新增 3 个模块级私有函数；改 `_clear_singleton_locks` 与 `BrowserSession.quit` |

## 新增基础设施

### `_chrome_pids_for_profile(user_data_dir) -> list[int]`

用标准库查出占用指定 user-data-dir 的 Chrome 进程 pid。

```
subprocess.run(['ps', '-Ao', 'pid=,command='], capture_output=True, text=True, timeout=5)
```

逐行匹配 `--user-data-dir=<user_data_dir>`（精确前缀匹配到路径边界，避免
`/a/b` 误匹配 `/a/bc`）。返回值按「主进程优先」排序：命令行**不含** `--type=`
的是浏览器主进程，含 `--type=renderer` / `--type=gpu-process` 等的是 helper。

平台降级：非 `darwin` / `linux` 或 `ps` 调用失败 → 返回 `[]`，
调用方退化成当前行为（直接删锁），不阻断启动。

**为什么不用 psutil**：项目未安装该依赖，PRD 约束不新增依赖。`ps -Ao pid=,command=`
在 macOS 与 Linux 上输出格式一致，足够本用途。

### `_kill_chrome_for_profile(user_data_dir, reason) -> int`

终止占用该 profile 的所有 Chrome 进程，返回终止数量。

1. 拿到 pid 列表，为空则直接返回 0
2. 对主进程发 `SIGTERM`（`os.kill`），给 Chrome 正常落盘 Cookies 的机会
3. 轮询最多 5 秒（0.25s 间隔）等待列表清空
4. 超时仍存活 → 对剩余全部 pid 发 `SIGKILL`
5. 打印 `⚠️ 回收 {reason} 的残留 Chrome 进程 N 个`

`ProcessLookupError` / `PermissionError` 静默跳过（进程已退出或非本用户）。

**并发安全性论证**：`AccountRegistry.claim()` / `try_open_manual()`
（`src/web/worker.py:38-110`）已保证同一 email profile 在任一时刻只被一个 worker 或
一个手动会话持有。因此 `create_driver` 执行到此处时，占用该 profile 的任何存活进程
**必然是孤儿**，kill 不会误伤正在工作的会话。

这一步严格优于现状：现状是无条件删锁后让第二个实例与孤儿并存（正是白屏主因 B），
新逻辑是先消灭孤儿再删锁。

### `_prune_profile_cache(user_data_dir) -> None`

```
CACHE_DIRS = ['Cache', 'Code Cache', 'Service Worker', 'GPUCache', 'DawnCache', 'ShaderCache']
```

1. 累加 `Default/<each>` 的磁盘占用（`os.walk` 求和，符号链接跳过）
2. 合计 ≤ `PROFILE_CACHE_LIMIT_MB` → 直接返回，不打日志（避免刷屏）
3. 超阈值 → 逐个 `shutil.rmtree(..., ignore_errors=True)`
4. 打印 `🧹 profile 缓存 {n}MB 超过 {limit}MB，已清理（登录态保留）`

**登录态安全性**：Chrome 的登录态存放在 `Default/Cookies`（SQLite）、
`Default/Login Data`、`Default/Local Storage`、以及 profile 根的 `Local State`
（加密密钥）。上述 6 个目录均不含其中任何一个，删除后 Chrome 自动重建空缓存。
`Service Worker` 目录被删会强制 `dash.cloudflare.com` 重新注册 SW —— 这正是修复
主因 A 的手段。

**为什么用总量阈值而非逐目录阈值**：三个大目录是联动增长的，单看任一个都可能不触发，
而白屏由整体缓存腐坏引起，总量更贴合实际信号。

## 修改点

### `_clear_singleton_locks()`（`driver.py:431-441`）

调整为「先回收孤儿，再删锁，再剪缓存」的三步：

```
_kill_chrome_for_profile(user_data_dir, safe_name)   # 新增：前提验证 + 回收
<原有 SingletonLock/Cookie/Socket 删除>               # 保留：此时前提已成立
<原有 Preferences >10MB 重置>                         # 保留
_prune_profile_cache(user_data_dir)                  # 新增
```

顺序不可调换：
- kill 必须在删锁前 —— 否则孤儿仍在时删锁就是制造双实例
- `_prune_profile_cache` 必须在 kill 之后 —— 删活着的 Chrome 的 Cache 目录会让它崩溃
- 现有「`_write_profile_language` 必须在 `_clear_singleton_locks` 之后」的约束
  （`driver.py:455-456`）不变，因为后者可能重置 Preferences

仅对持久化 profile 生效（`is_persistent` 为真）。临时 profile 每次全新创建，
既无孤儿也无缓存积累，跳过以省开销。

### `BrowserSession.quit()`（`driver.py:207-225`）

```
if self._closed: return
self._closed = True

try:
    self.context.close()
except Exception as e:
    print(f"  ⚠️ 关闭浏览器 context 失败: {str(e)[:120]}")   # 改：不再静默

try:
    self.playwright.stop()
except Exception as e:
    print(f"  ⚠️ 停止 playwright 失败: {str(e)[:120]}")       # 改：不再静默

# 新增：确认 Chrome 确已退出，未退出则回收
if self._user_data_dir and not self._temp_profile:
    _kill_chrome_for_profile(self._user_data_dir, '关闭后残留')

<原有临时 profile rmtree>                                    # 保留
```

`BrowserSession.__init__` 新增 `user_data_dir` 参数（默认 `None`），
`create_driver` 构造时传入。`_temp_profile` 字段保留不动 —— 它承担的是
「是否删目录」的语义，与新字段职责不同。

打印走 `print`，由 `AppState._patch_prints` 劫持进 Web 日志，与现有日志一致。

### `src/config.py`

在「充值/账单相关常量」区块后新增：

```python
# === 浏览器 profile 相关常量 ===
# 单个持久化 profile 的缓存类目录（Cache/Code Cache/Service Worker/GPUCache 等）
# 合计上限（MB）。超限则在浏览器启动前整体清理——这些目录不含登录态。
# 缓存腐坏（尤其 Service Worker）会让 dash.cloudflare.com 返回空响应导致白屏。
PROFILE_CACHE_LIMIT_MB = 200
```

沿用 `INVOICE_DAILY_CAP` / `TOPUP_AMOUNT` 的模块级常量风格，不进 dataclass —— 这是
运维阈值而非用户配置，不需要出现在 `config.yaml`。

## 兼容性与回滚

- 对调用方零接口变更，`create_driver` / `close_driver` 签名不变
- 非 macOS/Linux 平台自动降级为当前行为
- 回滚粒度：三个新函数彼此独立，可单独摘除
  - 只想关掉缓存清理 → `PROFILE_CACHE_LIMIT_MB` 调到极大值
  - 只想关掉进程回收 → `_chrome_pids_for_profile` 直接 `return []`

## 风险

| 风险 | 缓解 |
|------|------|
| `ps` 输出被截断导致漏匹配长路径 | `-Ao command=` 不截断（区别于 `ps aux`）；已在本机验证路径完整 |
| kill 误伤用户日常 Chrome | 匹配条件含完整 `data/profiles/<email>` 路径，日常 Chrome 用默认 profile 不会命中 |
| 首次清理后账号需重新登录 | 设计上不碰 Cookies/Login Data；验收标准 2 专门验证这点 |
| 清理耗时拖慢启动 | `os.walk` 求和在 700M 目录上约百毫秒级；仅持久化 profile 且超阈值才 rmtree |
