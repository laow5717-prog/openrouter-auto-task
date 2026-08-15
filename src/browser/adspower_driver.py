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
from src.models.adspower_profile import RECHARGED_RANK
from src.services.adspower import (
    AdsPowerQuotaExceeded, AdsPowerProfileMissing, AdsPowerError,
    AdsPowerDailyLimitExceeded, AdsPowerProfileLocked,
    DEFAULT_DAILY_LIMIT_RECOVERY_SEC,
)


# Stripe 付款域名绕过代理直连。**分号分隔**——这是 Chrome --proxy-bypass-list 的原生格式，
# 与 app.py 的 _PROXY_BYPASS（Playwright 的逗号格式）语义相同但写法不同，故各存一份。
# 改这里之前先读模块 docstring 第 2 条：不绕过 = 付款链路直接死。
ADSPOWER_PROXY_BYPASS = (
    "*.stripe.com;stripe.com;*.stripecdn.com;b.stripecdn.com;js.stripe.com;"
    "m.stripe.com;m.stripe.network;*.stripe.network"
)
# 接管 AdsPower 环境后强制的窗口尺寸。
#
# AdsPower 自己给的默认窗口偏小，Stripe 结账页会挤成窄单栏——币种切换、支付方式
# accordion 这类控件被折叠或挤出可视区，点击容易落空，人工看截图排查也费劲。
# 选 1600x1000：常见笔电分辨率，两栏结账页能完整展开，又不会超出 1920x1080 云主机
# 的屏幕（超出会被窗口管理器裁掉，等于白设）。屏幕更大想占更满就调这里。
ADSPOWER_WINDOW_SIZE = (1600, 1000)

ADSPOWER_LAUNCH_ARGS = [
    f"--proxy-bypass-list={ADSPOWER_PROXY_BYPASS}",
    f"--window-size={ADSPOWER_WINDOW_SIZE[0]},{ADSPOWER_WINDOW_SIZE[1]}",
]

# 接管后等 AdsPower 把初始标签页换完。不是玄学等待：AdsPower 启动时先开一个占位 tab，
# 随后关掉换成正式的，期间拿到的 Page 句柄会失效（见模块 docstring 第 3 条）。
_TAKEOVER_SETTLE_SEC = 2.0

# 单次回收删几个环境。删多了浪费登录态，删少了并发下不够分。
DEFAULT_RECLAIM_BATCH = 3

# 撞上占用锁（AdsPowerProfileLocked）后等多久重试。锁有两种成因：
#   a) 刚关掉的浏览器，AdsPower 云端状态还没翻转 —— 等几秒就好；
#   b) 云端残留的占用记录（进程被强杀、或同账号在别处开着）—— 等多久都没用。
# 取 5 秒是给 (a) 留够时间：_stop_all 里那 1.5 秒是「本机 stop 后状态翻转」的量级，
# 而云端同步明显更慢——2026-08-16 现场有一批 delete 就是卡在 1.5 秒不够上。
_LOCK_RETRY_SEC = 5.0

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

    # ---- 「今日创建次数用尽」的冻结闸 ----
    #
    # 撞到 AdsPowerDailyLimitExceeded 后，在恢复时间之前**本地直接短路**，不再发出
    # 任何 create 请求。少了这道闸，每个还没有环境映射的账号都会各自去撞一次，而且
    # 失败后进的是 failed_this_round（本轮跳过、下轮重试）——2026-08-11 就是这样用
    # 7 个账号打出了 233 次必败请求，把限额越撞越深。
    #
    # **类级**而非实例级：日限额是 AdsPower 账户级的资源，而 pool 会在 UI 改配置时被
    # 整个重建（app._ensure_adspower 里 creds 变更那条分支），实例级状态一重建就丢，
    # 冻结完立刻又开始撞。代价是换 API Key 到另一个 AdsPower 账户时冻结不会跟着清——
    # 相比反复撞限额，这个误等是更便宜的一侧。
    _daily_limit_until = 0.0
    _daily_limit_lock = threading.Lock()

    @classmethod
    def creation_frozen_seconds(cls):
        """距离可以再次创建环境还剩几秒。0 表示没冻结。"""
        with cls._daily_limit_lock:
            return max(0.0, cls._daily_limit_until - time.time())

    @classmethod
    def _freeze_creation(cls, seconds):
        """记下冻结截止时刻。返回 (是否是本次新设的, 剩余秒数)。

        取 max 而不是无条件覆盖：并发下几个 worker 可能几乎同时撞到同一堵墙，
        后到的那个若用自己的（更短的）恢复时长覆盖，会把冻结期悄悄缩短。
        """
        seconds = float(seconds or DEFAULT_DAILY_LIMIT_RECOVERY_SEC)
        until = time.time() + seconds
        with cls._daily_limit_lock:
            fresh = until > cls._daily_limit_until
            if fresh:
                cls._daily_limit_until = until
            return fresh, max(0.0, cls._daily_limit_until - time.time())

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
        # 池锁把整条「挑代理→建环境→撞配额→回收→重试」串行化，每一步都是带限流的
        # AdsPower 请求，并发下后来者可能要排队十几秒。先探一次锁，等得上再说一声——
        # 否则这段等待完全静默，调用方的日志停在「打开注册页」之后再无下文。
        if not self._lock.acquire(blocking=False):
            self._log(f"[AdsPower] {email} 等待环境池（另一个账号正在建环境或回收）…")
            self._lock.acquire()
        try:
            row = self.profiles.get_by_email(email)
            if row and row.get("profile_id"):
                self.profiles.touch(email)
                return row["profile_id"], row.get("proxy_id") or "", False
            return self._create_profile(email)
        finally:
            self._lock.release()

    def _create_profile(self, email):
        """建环境。调用方须持有 _lock。

        撞到「今日创建次数用尽」时会冻结创建（见类属性 _daily_limit_until），冻结期内
        后续调用**不再发请求**，直接抛同一个异常。已有环境映射的账号不受影响——那条
        路径在 ensure_profile 里就返回了，压根不会走到这。
        """
        frozen = self.creation_frozen_seconds()
        if frozen > 0:
            raise AdsPowerDailyLimitExceeded(
                f"AdsPower 今日创建环境次数已用尽，还需等待约 {frozen / 60:.0f} 分钟"
                f"（本地已冻结创建，未再发请求）", recovery_seconds=frozen)

        proxy_id = self.pick_free_proxy()
        payload = {
            "name": _profile_name(email),
            "remark": f"openrouter-auto-task 自动创建 / {email}",
            "group_id": self.group_id,
            "proxyid": proxy_id,
            "fingerprint_config": _fingerprint_config(self.ua_systems),
        }
        try:
            profile_id, profile_no = self._create_or_freeze(payload)
        except AdsPowerQuotaExceeded as e:
            self._log(f"[AdsPower] 环境配额已满（{str(e)[:120]}），开始回收已完成账号的环境")
            freed = self.reclaim(exclude={email})
            if not freed:
                raise AdsPowerQuotaExceeded(
                    "AdsPower 环境配额已满，且没有可回收的环境"
                    "（所有环境都属于尚未完成或正在运行的账号）") from e
            # 重试这次同样可能撞日限额（配额与次数是两个独立的闸），所以走同一个包装。
            profile_id, profile_no = self._create_or_freeze(payload)

        self.profiles.upsert(email, profile_id, profile_no, proxy_id)
        self._log(f"[AdsPower] 为 {email} 新建环境 {profile_id}（编号 {profile_no}，"
                  f"代理 proxy_id={proxy_id}）")
        return profile_id, proxy_id, True

    def _create_or_freeze(self, payload):
        """create_profile 的薄包装：撞到日限额就落下冻结闸再把异常抛出去。

        日志只在**首次**落闸时打——冻结期内每个账号都会经过这里，逐次打印会把两小时
        的日志刷成同一句话，反而盖掉真正的问题。
        """
        try:
            return self.client.create_profile(payload)
        except AdsPowerDailyLimitExceeded as e:
            fresh, remain = self._freeze_creation(e.recovery_seconds)
            if fresh:
                self._log(
                    f"[AdsPower] 今日创建环境次数已用尽（{str(e)[:120]}）。"
                    f"已冻结新建环境约 {remain / 60:.0f} 分钟——这不是环境配额满，"
                    f"回收环境没有用，只能等恢复；期间已有环境的账号照常运行")
            raise

    def reclaim(self, exclude=None, limit=None):
        """删掉已完成账号的环境以腾出配额，返回被回收的 [(email, profile_id)]。

        候选由 DB 按 rank 排序给出（孤儿 → 身份已死 → 真终态 → recharged），这里再剔除
        两类：调用方点名排除的，以及此刻正被 worker 占用的（is_busy）。后者是硬约束——
        删掉正在用的环境会让那个 worker 的浏览器凭空消失。

        **第 3 档（RECHARGED_RANK）是最后手段。** 那些账号余额还没到 balance_cap，
        下一轮还会被领走继续充；删它的代价是一次 GitHub 完整重登 + 一次新设备邮箱验证
        （数分钟 + 一封验证码）。所以只在前面几档一个都挑不出来时才动它，而且**一次
        只牺牲一个**——批量删三个等于一次赔三份，而配额只要腾出一格就能继续跑。
        """
        exclude = set(exclude or ())
        limit = limit or self.reclaim_batch
        with self._lock:
            candidates = self.profiles.reclaim_candidates(limit=max(limit * 5, 20))
            picked = []
            sacrificed = False
            for row in candidates:
                if len(picked) >= limit:
                    break
                email = row["email"]
                if email in exclude:
                    continue
                if self._is_busy(email):
                    continue
                if row.get("rank") == RECHARGED_RANK:
                    # 候选已按 rank 排序，走到这里说明前面几档要么没有、要么全被
                    # exclude/is_busy 挡掉了。已经挑到更值得删的就不必牺牲活账号；
                    # 真要牺牲也只牺牲这一个，后面全是同档或更低优先级，不必再看。
                    if picked:
                        break
                    sacrificed = True
                    picked.append(row)
                    break
                picked.append(row)

            if not picked:
                self._log("[AdsPower] 无可回收环境（候选均正在运行或被排除）")
                return []

            ids = [r["profile_id"] for r in picked]
            emails = [r["email"] for r in picked]
            self._stop_all(ids)
            try:
                self._delete_with_retry(ids)
            except AdsPowerError as e:
                self._log(f"[AdsPower] 删除环境失败: {str(e)[:150]}")
                return []
            # 本地映射必须在删除成功后才清：先清本地再删远端，一旦删除失败就会留下
            # 一批查不到映射、也没人再删的孤儿环境，配额被永久吃掉。
            self.profiles.delete_by_emails(emails)
            if sacrificed:
                # 单独一条：删的是活账号的登录态，不是垃圾。这条日志的出现频率就是
                # 「配额压力有多大」的直接指标——高频出现说明该调 balance_cap 或轮转账号数。
                row = picked[0]
                bal = row.get("bal")
                bal_txt = f"余额 ${bal:.0f}" if bal is not None else "余额未知"
                self._log(f"[AdsPower] 无真终态环境可回收，牺牲 1 个活账号环境腾配额: "
                          f"{row['email']}(recharged, {bal_txt})")
            else:
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

    def _delete_with_retry(self, profile_ids):
        """删除这些环境；撞占用锁时等一会儿再 stop + delete 一次。

        为什么必须重试：_stop_all 停完只等 1.5 秒，那是「本机 stop 后状态翻转」的
        量级，云端的占用记录同步更慢。2026-08-16 现场，「注册失败当场释放环境」这条
        新路径紧接在浏览器关闭之后，一批 delete 就撞上
        `[] is being used by other users and cannot be deleted` —— 环境明明已经没人用了，
        只是云端还没反应过来，而删除失败意味着那一格配额要等下一次 reclaim 才回得来。

        重试仍失败就抛出去，由调用方决定怎么记账。**绝不在删除失败时清本地映射**
        （那条红线在三个调用点都写着）：先清本地再删远端，一旦失败就留下查不到映射、
        也没人再删的孤儿环境，那一格配额被永久吃掉。
        """
        try:
            self.client.delete_profiles(profile_ids)
        except AdsPowerProfileLocked:
            self._log(f"[AdsPower] 环境仍被占用，{_LOCK_RETRY_SEC:.0f} 秒后重试删除…")
            time.sleep(_LOCK_RETRY_SEC)
            self._stop_all(profile_ids)
            self.client.delete_profiles(profile_ids)

    def drop_mapping(self, email):
        """删本地映射（用于「远端环境已不存在」「占用锁解不开」的自愈），不碰远端。"""
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
                self._delete_with_retry([row["profile_id"]])
            except AdsPowerError as e:
                self._log(f"[AdsPower] 删除 {email} 的环境失败: {str(e)[:150]}")
                return False
            self.profiles.delete_by_email(email)
            return True

    def release_many(self, emails):
        """批量删除这些账号的环境，返回 {released, skipped_busy, failed, no_profile}。

        不是 `for e in emails: self.release(e)`：release 每次都走一遍 _stop_all，
        而它在停过环境后要 sleep(1.5) 等 AdsPower 的异步状态翻转。删 20 个账号就是
        30 秒的纯等待，删除接口会直接卡到前端超时。整批一次 stop、一次 delete，
        那 1.5 秒只付一次。

        正被 worker 占用的账号（_is_busy）跳过远端删除并保留映射——删掉正在用的环境
        会让那个 worker 的浏览器凭空消失。调用方通常紧接着就把账号行删了，于是映射
        变成孤儿，由 reclaim_candidates 的第 0 档在跑完后回收，配额不会永久丢。

        删除失败时**不清本地映射**（与 reclaim 同一条红线）：先清本地再删远端，一旦
        失败就留下查不到映射、也没人再删的孤儿环境，那一格配额被永久吃掉。
        """
        out = {"released": [], "skipped_busy": [], "failed": [], "no_profile": []}
        if not emails:
            return out
        with self._lock:
            targets = []      # [(email, profile_id)]
            for email in emails:
                row = self.profiles.get_by_email(email)
                if not row:
                    out["no_profile"].append(email)
                    continue
                if self._is_busy(email):
                    out["skipped_busy"].append(email)
                    continue
                targets.append((email, row["profile_id"]))

            if out["skipped_busy"]:
                self._log("[AdsPower] 以下账号正在运行，其环境未删除（待跑完后回收）: "
                          + "、".join(out["skipped_busy"]))
            if not targets:
                return out

            self._stop_all([pid for _, pid in targets])
            try:
                self._delete_with_retry([pid for _, pid in targets])
            except AdsPowerError as e:
                self._log(f"[AdsPower] 批量删除环境失败: {str(e)[:150]}")
                out["failed"] = [email for email, _ in targets]
                return out

            emails_done = [email for email, _ in targets]
            self.profiles.delete_by_emails(emails_done)
            out["released"] = emails_done
            self._log(f"[AdsPower] 已随账号删除释放 {len(emails_done)} 个环境: "
                      + "、".join(emails_done))
            return out


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


def _resize_window(context, page, size=None):
    """把接管到的窗口调到 ADSPOWER_WINDOW_SIZE。尽力而为，失败不影响主流程。

    为什么不能只靠 launch_args 的 --window-size：那个开关只在**新建窗口**时生效，
    而 AdsPower 复用已有环境时窗口尺寸沿用上次记录的值，新传的参数被忽略。所以接管
    之后还要用 CDP Browser.setWindowBounds 明确压一次，两条路都走才在新建/复用两种
    情况下都拿到想要的尺寸。

    windowState 必须先置 normal：窗口处于 minimized/maximized 时 setWindowBounds
    不接受 width/height（CDP 会直接报错）。
    """
    w, h = size or ADSPOWER_WINDOW_SIZE
    try:
        cdp = context.new_cdp_session(page)
        window_id = cdp.send("Browser.getWindowForTarget")["windowId"]
        cdp.send("Browser.setWindowBounds",
                 {"windowId": window_id, "bounds": {"windowState": "normal"}})
        cdp.send("Browser.setWindowBounds",
                 {"windowId": window_id,
                  "bounds": {"left": 0, "top": 0, "width": w, "height": h}})
        return True
    except Exception as e:
        print(f"  ⚠️ 调整窗口尺寸失败(忽略): {str(e)[:80]}")
        return False


def _start_unlocking(client, pool, email, profile_id, args, headless):
    """start 环境；撞占用锁时试着解一次，仍锁则把 AdsPowerProfileLocked 抛出去。

    解锁动作 = stop + 等 _LOCK_RETRY_SEC + 再 start。这只对「刚关掉、云端状态还没
    翻转」那一类锁有效，对云端残留的占用记录无效（本机 stop 会直接回 `Profile is
    not open`，我们照样吞掉继续等——那一步本来就是尽力而为）。

    先试解锁而不是直接弃环境，是因为弃掉的代价是整份登录态：GitHub 授权态没了，
    下次跑要完整重登再过一次新设备邮箱验证，数分钟起步。为一次可能只是状态没翻转的
    锁付这个代价不值。
    """
    try:
        return client.start_profile(profile_id, launch_args=args, headless=headless)
    except AdsPowerProfileLocked as e:
        pool._log(f"[AdsPower] {email} 的环境 {profile_id} 被占用锁挡住"
                  f"（{str(e)[:120]}），尝试解除…")
        try:
            client.stop_profile(profile_id)
        except AdsPowerError:
            pass       # 本机认为它没开着——正是云端残留锁的典型表现，继续等着重试
        time.sleep(_LOCK_RETRY_SEC)
        return client.start_profile(profile_id, launch_args=args, headless=headless)


def create_driver_adspower(email, pool, client, launch_args=None, headless=False):
    """启动/接管该账号的 AdsPower 环境，返回与本地栈同构的 BrowserSession。

    用**原生 Playwright**（非 Patchright）：反检测由 AdsPower 的指纹内核负责，
    我们这一侧只需要 add_init_script 能正常前置注入——那是 Stripe enterprise hCaptcha
    的 token 交付前提，而 Patchright 恰好阉割了它（见 driver.create_driver_vanilla 注释）。

    映射失效自愈，两种：
      1. 本地记着 profile_id 但 AdsPower 侧已被手工删掉 → start 报「环境不存在」。
         删本地映射重建一次，而不是把它当致命错误——用户在客户端里整理环境是
         完全正常的操作。
      2. 环境被占用锁挡住（AdsPowerProfileLocked）→ 见 _start_unlocking。
    """
    from playwright.sync_api import sync_playwright as _vanilla_sync_playwright

    args = list(launch_args) if launch_args is not None else list(ADSPOWER_LAUNCH_ARGS)

    profile_id, proxy_id, created = pool.ensure_profile(email)
    try:
        ws, debug_port = _start_unlocking(client, pool, email, profile_id, args, headless)
    except AdsPowerProfileMissing:
        pool.drop_mapping(email)
        pool._log(f"[AdsPower] 环境 {profile_id} 在 AdsPower 侧已不存在，重新创建")
        profile_id, proxy_id, created = pool.ensure_profile(email)
        ws, debug_port = client.start_profile(profile_id, launch_args=args, headless=headless)
    except AdsPowerProfileLocked:
        # 解不开的锁：弃用这个环境，换一个新的重来。代价是登录态没了（下次跑要完整
        # 重登 + 过一次新设备邮箱验证），但比每一轮都栽在同一个环境上强——那种情况下
        # 这个账号等于永久出局，而日志里只有一句「本次未付成」，看不出它再也回不来。
        pool.drop_mapping(email)
        pool._log(
            f"[AdsPower] ⚠️ 环境 {profile_id} 被占用锁挡住且无法解除，已弃用并新建环境；"
            f"登录态丢失（{email} 下次需重登）。**该环境仍占着一格配额**，"
            f"请去 AdsPower 客户端手动关闭并删除编号对应 {profile_id} 的环境")
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

        # 窗口调大：AdsPower 默认窗口偏小，Stripe 结账页会挤成窄单栏
        if not headless:
            _resize_window(context, page)

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
