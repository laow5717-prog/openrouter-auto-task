"""AdsPower 指纹浏览器本地 API 客户端。

只做 HTTP 层：鉴权、限流、错误分类。不认识「账号」这个概念——账号↔环境的编排在
src/browser/adspower_driver.py。

三条实测约束（2026-08-03 本机验证，改动前务必知道）：

1. **鉴权只认 `Authorization: Bearer <key>` 头。** v1 文档里的 `?api_key=` query 参数
   在本机构建上无效（返回 `Require api-key`），v1/v2 两套路径都得带这个头。

2. **有请求频率限制，超了直接报错而不是排队。** 0-200 个环境时 2 次/秒；
   `browser-profile/list`、`proxy-list/*` 等固定 1 秒/次。并发裸调会收到
   `{"code":-1,"msg":"Too many request per second, please check"}`。限流做在客户端内部
   （模块级锁 + 上次调用时间戳）而不是交给调用方自觉——调用点分散在多个 worker 线程，
   靠约定守不住。

3. **环境数有硬上限（本机实测 12）。** 创建到上限时返回 code=-1 且 msg 含
   `exceeds the limit`，这是需要触发环境回收的唯一信号，故单独归为
   AdsPowerQuotaExceeded，不能和普通失败混在一起。
"""

import json
import threading
import time
import urllib.error
import urllib.request


DEFAULT_BASE_URL = "http://local.adspower.net:50325"

# 限流间隔（秒）。文档给的是 2 次/秒与 1 次/秒，这里各留一点余量——间隔卡得太紧时
# 客户端与服务端对「一秒」的起点算法不同，边界上仍会被判超频。
_FAST_INTERVAL = 0.55
_SLOW_INTERVAL = 1.15

# 固定 1 秒/次的接口（文档「请求速率」章节明确列出）。
_SLOW_PATHS = (
    "/api/v1/user/list",
    "/api/v2/browser-profile/list",
    "/api/v1/group/list",
    "/api/v2/browser-profile/cookies",
    "/api/v2/browser-profile/download-kernel",
    "/api/v2/proxy-list/",
)


class AdsPowerError(Exception):
    """AdsPower 接口返回 code != 0。msg 原样带出，供日志直接展示。"""


class AdsPowerUnavailable(AdsPowerError):
    """连不上本地 API：客户端没运行、接口没开启、端口不对，或 API Key 无效。

    与 AdsPowerError 分开，是因为处置方式不同：这个错整个任务都别想跑了，
    应该立刻停手并提示用户去开客户端，而不是换个账号重试。
    """


class AdsPowerQuotaExceeded(AdsPowerError):
    """环境数达到套餐上限。唯一该触发环境回收的错误。"""


class AdsPowerProfileMissing(AdsPowerError):
    """指定的 profile_id 在 AdsPower 侧不存在（通常是用户在客户端里手工删了）。

    调用方应删掉本地映射并重新创建，而不是把这个当致命错误。
    """


def _classify(msg):
    """把 AdsPower 的错误文案映射到异常类。文案匹配是唯一可用的信号——
    接口对所有业务错误一律返回 code=-1，没有细分错误码。"""
    low = (msg or "").lower()
    if "exceeds the limit" in low or "number of imported accounts" in low:
        return AdsPowerQuotaExceeded
    if "api-key" in low or "api key" in low or "unauthorized" in low:
        return AdsPowerUnavailable
    if "not exist" in low or "not found" in low or "不存在" in (msg or ""):
        return AdsPowerProfileMissing
    return AdsPowerError


class AdsPowerClient:
    """AdsPower 本地 API 的同步客户端。线程安全（内部限流锁）。

    用 urllib 而非 requests：项目未依赖 requests，且 PyInstaller 打包时少一个隐式依赖。
    """

    def __init__(self, base_url=None, api_key="", timeout=180):
        self.base_url = (base_url or DEFAULT_BASE_URL).rstrip("/")
        self.api_key = api_key or ""
        self.timeout = timeout
        self._rate_lock = threading.Lock()
        self._last_call_at = 0.0

    # ---------- 底层 ----------

    def _throttle(self, path):
        """按接口类别等待到最小间隔。持锁期间 sleep 是刻意的：
        必须让并发调用真正排成队，只记时间戳不占锁等于没限流。"""
        interval = _SLOW_INTERVAL if any(path.startswith(p) for p in _SLOW_PATHS) else _FAST_INTERVAL
        with self._rate_lock:
            wait = self._last_call_at + interval - time.monotonic()
            if wait > 0:
                time.sleep(wait)
            self._last_call_at = time.monotonic()

    def _call(self, path, body=None, method=None):
        """发一次请求并返回 data 字段。code != 0 抛对应异常。"""
        self._throttle(path)
        url = self.base_url + path
        data = json.dumps(body).encode("utf-8") if body is not None else None
        req = urllib.request.Request(
            url, data=data, method=method or ("POST" if data is not None else "GET"))
        req.add_header("Authorization", f"Bearer {self.api_key}")
        req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                payload = json.loads(resp.read().decode("utf-8"))
        except urllib.error.URLError as e:
            raise AdsPowerUnavailable(
                f"无法连接 AdsPower 接口 {self.base_url}（{getattr(e, 'reason', e)}）；"
                f"请确认 AdsPower 客户端已启动且「自动化 - API」接口已开启") from e
        except json.JSONDecodeError as e:
            raise AdsPowerError(f"AdsPower 接口返回非 JSON 内容: {str(e)[:120]}") from e

        if payload.get("code") != 0:
            msg = payload.get("msg") or "未知错误"
            raise _classify(msg)(f"AdsPower 接口失败（{path}）: {msg}")
        return payload.get("data") or {}

    # ---------- 环境 ----------

    def list_profiles(self, page=1, page_size=100):
        """返回环境列表。每项含 profile_id / name / remark / profile_no。

        **走 v1 的 /api/v1/user/list 而不是 v2 的 browser-profile/list**：本机构建上
        v2 无视 page_size，永远每页只返 1 条（响应里 page_size 被回写成 1），
        且 total_count 只在第一页出现——按它翻页要发 N 次请求，而接口又限 1 秒/次。
        v1 正常尊重 page_size，一次就能取全。字段名不同（user_id / serial_number），
        这里统一映射成 v2 的命名，调用方不必关心走的是哪一版。
        """
        data = self._call(
            f"/api/v1/user/list?page={int(page)}&page_size={int(page_size)}")
        out = []
        for row in data.get("list") or []:
            item = dict(row)
            item["profile_id"] = row.get("user_id") or row.get("profile_id")
            item["profile_no"] = row.get("serial_number") or row.get("profile_no") or ""
            out.append(item)
        return out

    def list_all_profiles(self, page_size=100, max_pages=20):
        """翻完所有分页返回环境全量。"""
        out = []
        page = 1
        while page <= max_pages:
            rows = self.list_profiles(page=page, page_size=page_size)
            out.extend(rows)
            if len(rows) < page_size:
                break
            page += 1
        return out

    def create_profile(self, payload):
        """创建环境，返回 (profile_id, profile_no)。配额满时抛 AdsPowerQuotaExceeded。"""
        data = self._call("/api/v2/browser-profile/create", payload)
        return data.get("profile_id"), data.get("profile_no")

    def start_profile(self, profile_id, launch_args=None, headless=False):
        """启动环境，返回 (ws_endpoint, debug_port)。

        对已在运行的环境重复调用是安全的——AdsPower 会返回同一个 ws 地址。
        cdp_mask=1 隐藏 CDP 特征（反检测）；proxy_detection=0 跳过启动时的代理连通性
        检测，那一步会拖慢启动几秒且失败时直接拒绝启动，而我们更希望进到页面再判断。
        """
        body = {
            "profile_id": profile_id,
            "headless": "1" if headless else "0",
            "cdp_mask": "1",
            "proxy_detection": "0",
            "last_opened_tabs": "0",
            "password_filling": "0",
            "password_saving": "0",
        }
        if launch_args:
            body["launch_args"] = list(launch_args)
        data = self._call("/api/v2/browser-profile/start", body)
        ws = (data.get("ws") or {}).get("puppeteer")
        if not ws:
            raise AdsPowerError(f"AdsPower 启动环境 {profile_id} 未返回 ws 调试地址")
        return ws, data.get("debug_port")

    def stop_profile(self, profile_id):
        self._call("/api/v2/browser-profile/stop", {"profile_id": profile_id})

    def delete_profiles(self, profile_ids):
        """批量删除环境（单次上限 100，由调用方分批）。"""
        ids = [str(p) for p in profile_ids if p]
        if not ids:
            return 0
        self._call("/api/v2/browser-profile/delete", {"profile_id": ids})
        return len(ids)

    def profile_active(self, profile_id):
        """查询环境是否已打开，返回 (是否 Active, ws_endpoint)。"""
        data = self._call(f"/api/v2/browser-profile/active?profile_id={profile_id}")
        return data.get("status") == "Active", (data.get("ws") or {}).get("puppeteer")

    # ---------- 代理 ----------

    def list_proxies(self, page=1, limit=100):
        """返回 (代理列表, 总数)。每条含 proxy_id / host / port / profile_count /
        related_profile_no —— profile_count 是「这个代理已被几个环境绑定」的权威来源。"""
        data = self._call("/api/v2/proxy-list/list", {"page": str(page), "limit": str(limit)})
        return data.get("list") or [], int(data.get("total") or 0)

    def list_all_proxies(self, page_size=100, max_pages=20):
        """翻完所有分页返回代理全量。max_pages 是防呆上限（2000 个代理足够）。"""
        out = []
        page = 1
        while page <= max_pages:
            rows, total = self.list_proxies(page=page, limit=page_size)
            out.extend(rows)
            if len(out) >= total or not rows:
                break
            page += 1
        return out

    # ---------- 健康检查 ----------

    def ping(self):
        """接口是否可用。/status 不需要鉴权，可用来区分「客户端没开」与「Key 不对」。"""
        self._call("/status")
        return True
