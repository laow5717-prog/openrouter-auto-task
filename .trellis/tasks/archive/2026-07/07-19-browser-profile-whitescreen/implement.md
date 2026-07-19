# 执行计划

## 前置

方案关键点已在本机验证：

```bash
ps -Ao pid=,command= | grep "user-data-dir=.../data/profiles" | grep -v -- "--type="
# → 74195  （主进程可识别，路径未被截断）
```

## 检查清单

### 步骤 1：配置常量

- [ ] `src/config.py` 在 `TOPUP_AMOUNT` 之后新增 `PROFILE_CACHE_LIMIT_MB = 200`，
      带注释说明清理目标与白屏关联

验证：`.venv/bin/python3 -c "from src.config import PROFILE_CACHE_LIMIT_MB; print(PROFILE_CACHE_LIMIT_MB)"`

### 步骤 2：进程查询与回收

- [ ] `src/browser/driver.py` 新增 `_chrome_pids_for_profile(user_data_dir)`
      - `ps -Ao pid=,command=`，`timeout=5`
      - 匹配 `--user-data-dir=<dir>` 且需到达路径边界（后接空白或行尾），
        防止 `/a/b` 误匹配 `/a/bc`
      - 主进程（命令行不含 `--type=`）排在返回列表前面
      - 任何异常 → 返回 `[]`
- [ ] 新增 `_kill_chrome_for_profile(user_data_dir, reason)`
      - SIGTERM 主进程 → 轮询 5s（0.25s 间隔）→ 残留全部 SIGKILL
      - 吞 `ProcessLookupError` / `PermissionError`
      - 回收数 > 0 时打印 `⚠️ 回收 {reason} 的残留 Chrome 进程 N 个`

验证（不改代码的前提下先干跑查询函数）：

```bash
.venv/bin/python3 -c "
from src.browser.driver import _chrome_pids_for_profile
import glob
for d in glob.glob('data/profiles/*'):
    p = _chrome_pids_for_profile(__import__('os').path.abspath(d))
    if p: print(d, p)
"
```

预期：列出当前确实存在的孤儿 profile 及其 pid。

### 步骤 3：缓存清理

- [ ] 新增 `_prune_profile_cache(user_data_dir)`
      - 目标：`Cache` / `Code Cache` / `Service Worker` / `GPUCache` / `DawnCache` / `ShaderCache`（均在 `Default/` 下）
      - `os.walk` 求和（`follow_symlinks=False`），≤ 阈值静默返回
      - 超阈值 → `shutil.rmtree(ignore_errors=True)` + 打印清理日志
      - **绝不触碰** `Cookies` / `Login Data` / `Local Storage` / `Local State`

验证（只算不删，确认阈值判定符合预期）：

```bash
du -sm data/profiles/*/Default/{Cache,"Code Cache","Service Worker",GPUCache} 2>/dev/null | \
  awk '{s[$2]+=$1} END {for (k in s) print s[k], k}' | sort -rn | head
```

### 步骤 4：接入 `_clear_singleton_locks`

- [ ] 在 `driver.py:431` 的 `_clear_singleton_locks()` 内，按此顺序组织：
      1. `_kill_chrome_for_profile(...)` （新增，必须最先）
      2. 原有 Singleton* 删除（保留）
      3. 原有 Preferences >10MB 重置（保留）
      4. `_prune_profile_cache(...)` （新增，必须在 kill 之后）
- [ ] 仅 `is_persistent` 为真时执行新增的两步
- [ ] 更新函数注释：把「本应用通过 open_browsers 保证单实例，可安全清理」改成
      说明前提现由步骤 1 的孤儿回收主动保证

⚠️ 复核：`_write_profile_language(user_data_dir)` 仍必须在 `_clear_singleton_locks`
之后调用（`driver.py:455-456` 的既有约束）。

### 步骤 5：`quit()` 兜底

- [ ] `BrowserSession.__init__` 新增 `user_data_dir=None` 参数并存字段
- [ ] `create_driver` 构造 `BrowserSession` 时传入 `user_data_dir`
- [ ] `quit()`：`context.close()` 与 `playwright.stop()` 的 except 改为打印异常摘要
      （`str(e)[:120]`），不再静默
- [ ] `quit()` 末尾：持久化 profile（`self._temp_profile is None` 且有
      `user_data_dir`）时调 `_kill_chrome_for_profile(dir, '关闭后残留')`
- [ ] 保持 `quit()` 幂等（`_closed` 早返回逻辑不动）

### 步骤 6：验证

- [ ] 语法与导入：`.venv/bin/python3 -c "import src.browser.driver"`
- [ ] 清理现存现场：
      ```bash
      pkill -f "user-data-dir=.*cloudflare-auto-task"
      du -sh data/profiles
      ```
- [ ] 单账号实跑：应用内对一个大 profile 账号点「打开浏览器」
      - 日志出现缓存清理提示
      - 页面正常渲染（不白屏）
      - **仍处登录态**，无需重新输密码 ← 验收标准 2 的关键
- [ ] 孤儿场景：浏览器打开后 `kill -9` 主进程，再次点「打开浏览器」
      - 日志出现孤儿回收提示，正常启动
- [ ] 每日任务实跑一轮后核对：
      ```bash
      grep -c "浏览器初始化成功" server.log
      grep -c "正在关闭浏览器" server.log     # 两者应一致
      ps aux | grep -c "[G]oogle Chrome"      # 应回落
      ```

## 复核门

- 步骤 4 完成后暂停，人工确认 `_clear_singleton_locks` 内四步顺序无误再继续
- 步骤 5 完成后，全量跑一次 `/trellis:check` 再进实跑

## 回滚点

- 步骤 1-3 只新增代码，未接入调用链 → 此前任意时刻可安全中止
- 步骤 4/5 是唯二的行为变更点，出问题按 `design.md`「兼容性与回滚」小节降级：
  - 缓存清理失控 → `PROFILE_CACHE_LIMIT_MB` 调到 `10 ** 9`
  - 进程回收误杀 → `_chrome_pids_for_profile` 首行 `return []`
