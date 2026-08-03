"""AdsPower 环境池 + CDP 接管。

把「账号」翻译成「AdsPower 环境」，再用原生 Playwright 经 CDP 接管它，组装成与本地
栈完全同构的 BrowserSession —— 下游 50+ 个 driver 函数和 opencode_* 页面流程一行都不用改。

设计上必须知道的三件事（都是实测踩出来的，不是推断）：

1. **环境是稀缺资源，代理不是。** 本机实测环境上限 12，而账号成百上千；AdsPower 代理
   列表里有 100 个代理。所以瓶颈永远在环境配额，回收逻辑（reclaim）是主线而非兜底。

2. **Stripe 必须绕过代理直连。** i-proxy 这类住宅代理把 stripe.com 列入黑名单，
   CONNECT 直接被拒；不加绕过时 checkout.stripe.com 报 ERR_TUNNEL_CONNECTION_FAILED，
   整条付款链路断掉。本地栈靠 Playwright 的 proxy.bypass 解决，CDP 接管下没有那个参数，
   只能在启动环境时用 Chrome 原生的 --proxy-bypass-list（**分号**分隔，与 Playwright
   的逗号格式不同，两份常量不能互相复用）。

3. **接管后不能直接取 pages[0]。** AdsPower 启动时会开一个初始 tab 再替换掉它，
   加上一个 devtools:// 页；直接用 pages[0] 会拿到已关闭的目标，报
   "Target page, context or browser has been closed"。
"""

import threading
import time

from src.browser.driver import (
    BrowserSession, DEFAULT_TIMEOUT_MS, NAV_TIMEOUT_MS, BROWSER_ACCEPT_LANG_HEADER,
)
from src.services.adspower import (
    AdsPowerQuotaExceeded, AdsPowerProfileMissing, AdsPowerError,
)


# Stripe 付款域名绕过代理直连。**分号分隔**——这是 Chrome --proxy-bypass-list 的原生格式，
# 与 app.py 的 _PROXY_BYPASS（Playwright 的逗号格式）语义相同但写法不同，故各存一份。
# 改这里之前先读模块 docstring 第 2 条：不绕过 = 付款链路直接死。
ADSPOWER_PROXY_BYPASS = (
    "*.stripe.com;stripe.com;*.stripecdn.com;b.stripecdn.com;js.stripe.com;"
    "m.stripe.com;m.stripe.network;*.stripe.network"
)
ADSPOWER_LAUNCH_ARGS = [f"--proxy-bypass-list={ADSPOWER_PROXY_BYPASS}"]

# 接管后等 AdsPower 把初始标签页换完。不是玄学等待：AdsPower 启动时先开一个占位 tab，
# 随后关掉换成正式的，期间拿到的 Page 句柄会失效（见模块 docstring 第 3 条）。
_TAKEOVER_SETTLE_SEC = 2.0

# 单次回收删几个环境。删多了浪费登录态，删少了并发下不够分。
DEFAULT_RECLAIM_BATCH = 3

# 环境指纹允许的操作系统。**只能是 Windows 或 macOS**：本项目所有页面选择器都按桌面版
# 编写，随机到 Android/iOS 会拿到移动版 GitHub / Stripe 页面而整条流程失败。
# 取值取自 AdsPower 文档支持的系统版本；Windows 7/8 已停止支持、装机量极低，本身就是
# 一个显眼的指纹特征，故只用 10/11 与近几代 macOS。
ADSPOWER_UA_SYSTEMS = ["Windows 10", "Windows 11", "Mac OS X 12", "Mac OS X 13"]


def _profile_name(email):
    """环境名。带 email 是为了能在 AdsPower 客户端里人工核对到底是哪个账号。"""
    return f"auto-{email}"[:100]


def _fingerprint_config(ua_systems=None):
    """新建环境的指纹配置。

    automatic_timezone：时区跟随代理出口 IP。时区与 IP 打架是最廉价的风控信号之一。
    webrtc=disabled：防 WebRTC 把真实 IP 泄漏出去——那会让整个代理隔离白做。
    random_ua.ua_system_version：**必须显式给**。文档写明不填即「在所有系统中随机」，
        而那个集合含 Android / iOS / Linux——移动端 UA 会让 GitHub 与 Stripe 返回移动版
        页面，本项目所有选择器都是按桌面版写的，一旦随机到就整条流程失败。
        实测抽样 10 个未设置的环境恰好都落在 Windows/macOS，正因为是随机，这种
        「碰巧对」不能当契约用。
    """
    systems = list(ua_systems) if ua_systems else list(ADSPOWER_UA_SYSTEMS)
    return {
        "automatic_timezone": "1",
        "webrtc": "disabled",
        "browser_kernel_config": {"version": "latest", "type": "chrome"},
        "random_ua": {"ua_browser": ["chrome"], "ua_system_version": systems},
    }


class AdsPowerProfilePool:
    """账号 ↔ AdsPower 环境的编排：查映射、挑代理、建环境、撞配额时回收。

    并发红线：整条「挑代理 → 建环境 → 撞配额 → 回收 → 重试」路径由 _lock 串行化。
    若让并发 worker 各自回收各自重试，会出现 A 刚删出来的配额被 B 抢走、A 再次失败
    的活锁——两个 worker 都在回收却谁都建不成。串行后一次只有一个 worker 在争配额，
    代价是建环境阶段无并发（几秒级），换来的是不会活锁。
    """

    def __init__(self, client, profile_model, group_id="0",
                 reclaim_batch=DEFAULT_RECLAIM_BATCH, log=None, is_busy=None,
                 ua_systems=None):
        self.client = client
        self.profiles = profile_model
        self.group_id = str(group_id or "0")
        self.reclaim_batch = max(1, int(reclaim_batch or DEFAULT_RECLAIM_BATCH))
        self.ua_systems = list(ua_systems) if ua_systems else list(ADSPOWER_UA_SYSTEMS)
        self._log = log or (lambda msg: print(msg))
        # is_busy(email) -> bool：该账号此刻是否正被某个 worker 占用。由调用方注入
        # （通常是 AccountRegistry.is_claimed），回收时据此跳过，避免删掉正在用的环境。
        self._is_busy = is_busy or (lambda email: False)
        self._lock = threading.RLock()

    # ---------- 代理 ----------

    def pick_free_proxy(self):
        """挑一个未被任何环境绑定的代理，返回 proxy_id（字符串）。

        占用判据用 AdsPower 服务端的 profile_count，而不是本地记录：环境↔代理的绑定
        存活在 AdsPower 且跨进程持久，本地内存态一重启就失真。

        代理列表为空时抛错而不是退化为直连——静默直连意味着所有账号共用本机 IP，
        反关联彻底失效，而这种失效在日志里是看不出来的。
        """
        proxies = self.client.list_all_proxies()
        if not proxies:
            raise AdsPowerError(
                "AdsPower 代理列表为空，无法为环境绑定出口 IP；"
                "请先在 AdsPower 客户端「代理管理」中导入代理")

        def _count(p):
            try:
                return int(p.get("profile_count") or 0)
            except (TypeError, ValueError):
                return 0

        free = [p for p in proxies if _count(p) == 0]
        if free:
            return str(free[0]["proxy_id"])

        least = min(proxies, key=_count)
        self._log(f"[AdsPower] 警告：{len(proxies)} 个代理全部已被环境占用，"
                  f"复用占用最少的 proxy_id={least['proxy_id']}"
                  f"（已绑 {_count(least)} 个环境，存在同 IP 关联风险）")
        return str(least["proxy_id"])

    # ---------- 环境 ----------

    def ensure_profile(self, email):
        """取得该账号的环境，返回 (profile_id, proxy_id, created)。

        命中本地映射即复用（保住登录态）；否则挑代理建新环境，撞配额则回收后重试一次。
        """
        with self._lock:
            row = self.profiles.get_by_email(email)
            if row and row.get("profile_id"):
                self.profiles.touch(email)
                return row["profile_id"], row.get("proxy_id") or "", False
            return self._create_profile(email)

    def _create_profile(self, email):
        """建环境。调用方须持有 _lock。"""
        proxy_id = self.pick_free_proxy()
        payload = {
            "name": _profile_name(email),
            "remark": f"openrouter-auto-task 自动创建 / {email}",
            "group_id": self.group_id,
            "proxyid": proxy_id,
            "fingerprint_config": _fingerprint_config(self.ua_systems),
        }
        try:
            profile_id, profile_no = self.client.create_profile(payload)
        except AdsPowerQuotaExceeded as e:
            self._log(f"[AdsPower] 环境配额已满（{str(e)[:120]}），开始回收已完成账号的环境")
            freed = self.reclaim(exclude={email})
            if not freed:
                raise AdsPowerQuotaExceeded(
                    "AdsPower 环境配额已满，且没有可回收的环境"
                    "（所有环境都属于尚未完成或正在运行的账号）") from e
            profile_id, profile_no = self.client.create_profile(payload)

        self.profiles.upsert(email, profile_id, profile_no, proxy_id)
        self._log(f"[AdsPower] 为 {email} 新建环境 {profile_id}（编号 {profile_no}，"
                  f"代理 proxy_id={proxy_id}）")
        return profile_id, proxy_id, True

    def reclaim(self, exclude=None, limit=None):
        """删掉已完成账号的环境以腾出配额，返回被回收的 [(email, profile_id)]。

        候选由 DB 按账号状态排序给出（recharged 优先），这里再剔除两类：
        调用方点名排除的，以及此刻正被 worker 占用的（is_busy）。后者是硬约束——
        删掉正在用的环境会让那个 worker 的浏览器凭空消失。
        """
        exclude = set(exclude or ())
        limit = limit or self.reclaim_batch
        with self._lock:
            candidates = self.profiles.reclaim_candidates(limit=max(limit * 5, 20))
            picked = []
            for row in candidates:
                if len(picked) >= limit:
                    break
                email = row["email"]
                if email in exclude:
                    continue
                if self._is_busy(email):
                    continue
                picked.append(row)

            if not picked:
                self._log("[AdsPower] 无可回收环境（候选均正在运行或被排除）")
                return []

            ids = [r["profile_id"] for r in picked]
            emails = [r["email"] for r in picked]
            self._stop_all(ids)
            try:
                self.client.delete_profiles(ids)
            except AdsPowerError as e:
                self._log(f"[AdsPower] 删除环境失败: {str(e)[:150]}")
                return []
            # 本地映射必须在删除成功后才清：先清本地再删远端，一旦删除失败就会留下
            # 一批查不到映射、也没人再删的孤儿环境，配额被永久吃掉。
            self.profiles.delete_by_emails(emails)
            detail = "、".join(f"{r['email']}({r['status']})" for r in picked)
            self._log(f"[AdsPower] 已回收 {len(picked)} 个环境释放配额: {detail}")
            return [(r["email"], r["profile_id"]) for r in picked]

    def _stop_all(self, profile_ids):
        """删除前先关掉这些环境。

        AdsPower 拒绝删除正在运行的环境（"is being used by other users and cannot be
        deleted"）。回收候选理论上都是已跑完的账号，但浏览器可能因为上一轮崩溃/强杀
        而残留在 Active——那种残留恰恰最该被回收，不能因为它开着就删不掉。
        stop 是异步的（状态约 1 秒后才翻），故 stop 完留一点时间再删。
        """
        stopped = False
        for pid in profile_ids:
            try:
                self.client.stop_profile(pid)
                stopped = True
            except AdsPowerError:
                pass   # 本来就没开着，或已不存在——都不影响接下来的删除
        if stopped:
            time.sleep(1.5)

    def drop_mapping(self, email):
        """删本地映射（用于「远端环境已不存在」的自愈），不碰远端。"""
        with self._lock:
            self.profiles.delete_by_email(email)

    def release(self, email):
        """主动删除某账号的环境（远端 + 本地映射）。"""
        with self._lock:
            row = self.profiles.get_by_email(email)
            if not row:
                return False
            self._stop_all([row["profile_id"]])
            try:
                self.client.delete_profiles([row["profile_id"]])
            except AdsPowerError as e:
                self._log(f"[AdsPower] 删除 {email} 的环境失败: {str(e)[:150]}")
                return False
            self.profiles.delete_by_email(email)
            return True


def _pick_page(context):
    """从接管到的 context 里选一个可用页面。

    过滤 devtools:// —— 那是 AdsPower 自己开的调试页，操作它没有意义。
    一个可用页都没有时新开一个（而不是报错）：AdsPower 有时会把初始 tab 关干净。
    """
    for pg in context.pages:
        try:
            url = pg.url or ""
        except Exception:
            continue
        if url.startswith("devtools://"):
            continue
        if pg.is_closed():
            continue
        return pg
    return context.new_page()


def create_driver_adspower(email, pool, client, launch_args=None, headless=False):
    """启动/接管该账号的 AdsPower 环境，返回与本地栈同构的 BrowserSession。

    用**原生 Playwright**（非 Patchright）：反检测由 AdsPower 的指纹内核负责，
    我们这一侧只需要 add_init_script 能正常前置注入——那是 Stripe enterprise hCaptcha
    的 token 交付前提，而 Patchright 恰好阉割了它（见 driver.create_driver_vanilla 注释）。

    映射失效自愈：本地记着 profile_id 但 AdsPower 侧已被手工删掉时，start 会报
    "环境不存在"。此时删本地映射重建一次，而不是把它当致命错误——用户在客户端里
    整理环境是完全正常的操作。
    """
    from playwright.sync_api import sync_playwright as _vanilla_sync_playwright

    args = list(launch_args) if launch_args is not None else list(ADSPOWER_LAUNCH_ARGS)

    profile_id, proxy_id, created = pool.ensure_profile(email)
    try:
        ws, debug_port = client.start_profile(profile_id, launch_args=args, headless=headless)
    except AdsPowerProfileMissing:
        pool.drop_mapping(email)
        pool._log(f"[AdsPower] 环境 {profile_id} 在 AdsPower 侧已不存在，重新创建")
        profile_id, proxy_id, created = pool.ensure_profile(email)
        ws, debug_port = client.start_profile(profile_id, launch_args=args, headless=headless)

    print(f"🌐 接管 AdsPower 环境 {profile_id}（{'新建' if created else '复用'}，"
          f"代理 proxy_id={proxy_id or '-'}，debug_port={debug_port}）")

    playwright = _vanilla_sync_playwright().start()
    browser = None
    try:
        browser = playwright.chromium.connect_over_cdp(ws)
        if not browser.contexts:
            raise AdsPowerError(f"接管 AdsPower 环境 {profile_id} 后没有可用的浏览器上下文")
        context = browser.contexts[0]
        time.sleep(_TAKEOVER_SETTLE_SEC)
        page = _pick_page(context)

        context.set_default_timeout(DEFAULT_TIMEOUT_MS)
        context.set_default_navigation_timeout(NAV_TIMEOUT_MS)
        # 强制英文页面：所有页面选择器都按英文文案编写。走 CDP Network 域设请求头，
        # 不触发 Emulation.setLocaleOverride（那是受控浏览器的检测信号）。
        try:
            context.set_extra_http_headers({"Accept-Language": BROWSER_ACCEPT_LANG_HEADER})
        except Exception as e:
            print(f"  ⚠️ 设置 Accept-Language 头失败(忽略): {str(e)[:80]}")

        session = BrowserSession(
            playwright, context, page,
            temp_profile=None, download_dir=None, user_data_dir=None,
            remote_browser=browser,
            remote_stop=lambda: client.stop_profile(profile_id),
        )
        page.on("response", session._on_response)
        session.adspower_profile_id = profile_id
        session.adspower_proxy_id = proxy_id
        print("✅ AdsPower 环境接管成功")
        return session
    except Exception:
        print("  ❌ AdsPower 环境接管失败，正在清理...")
        if browser is not None:
            try:
                browser.close()
            except Exception:
                pass
        try:
            playwright.stop()
        except Exception:
            pass
        try:
            client.stop_profile(profile_id)
        except Exception:
            pass
        raise
