"""原始 CDP 前置注入器——绕过 Patchright 禁用的 add_init_script，把 JS 在每个目标
（含跨域 OOPIF 子帧，如 Stripe 的 b.stripecdn/HCaptchaInvisible）**脚本加载前**注入。

背景（见 .trellis/tasks/07-25-hotmail-github-signup/design.md 第三轮结论）：
- Patchright 为反检测静默禁用 Playwright 的 add_init_script（底层 addScriptToEvaluateOnNewDocument）。
- Stripe enterprise hCaptcha 的 window.hcaptcha 在点 Subscribe 瞬间才于 OOPIF 帧诞生，随即
  api.js 定义并 execute()，任何"事后" evaluate 注入都拦不到。
- Playwright 的 CDPSession 无法按 sessionId 给 flatten 子会话发命令，且它自己的 auto-attach
  已把子目标 attach 并立即 resume（waiting=false），拿不到 waitForDebugger 暂停窗口。

方案：经 Chrome --remote-debugging-port 旁开一条**独立**的原始 CDP 连接（websocket-client），
在**浏览器级** Target.setAutoAttach(waitForDebuggerOnStart, flatten)。每个新目标 attach 时会
暂停在 debugger，在暂停窗口内按 sessionId 发 Page.addScriptToEvaluateOnNewDocument(source)
注册前置脚本，再 Runtime.runIfWaitingForDebugger 放行——脚本遂在该目标首个文档脚本前运行。

线程模型：recv 循环独立线程，与业务线程/Playwright 线程隔离（不碰任何 Playwright 对象），
符合 driver 的线程红线。start()/stop() 幂等。
"""
import json
import threading
import urllib.request

try:
    import websocket  # websocket-client
except ImportError:
    websocket = None


def _browser_ws_url(port, timeout=5):
    """从 http://127.0.0.1:port/json/version 取浏览器级 webSocketDebuggerUrl。"""
    url = f"http://127.0.0.1:{port}/json/version"
    with urllib.request.urlopen(url, timeout=timeout) as r:
        data = json.loads(r.read().decode("utf-8"))
    return data["webSocketDebuggerUrl"]


class CDPPreInjector:
    """浏览器级原始 CDP 前置注入器。

    用法：
        inj = CDPPreInjector(session.debug_port, HOOK_JS)
        inj.start()          # 装 auto-attach，此后新目标都会被前置注入
        ...  # 触发含 hcaptcha 的操作（点 Subscribe）
        inj.stop()
    """

    def __init__(self, port, source_js, on_log=None):
        self.port = port
        self.source = source_js
        self._ws = None
        self._thread = None
        self._id = 0
        self._id_lock = threading.Lock()
        self._running = False
        self._attached = []          # 记录 attach 到的目标 URL（诊断用）
        self._injected = 0           # 成功发出 addScript 的目标数
        self._on_log = on_log or (lambda m: None)

    def _next_id(self):
        with self._id_lock:
            self._id += 1
            return self._id

    def _send(self, method, params=None, session_id=None):
        """发一条 CDP 命令（fire-and-forget；flatten 下用顶层 sessionId 字段寻址子会话）。"""
        msg = {"id": self._next_id(), "method": method, "params": params or {}}
        if session_id:
            msg["sessionId"] = session_id
        try:
            self._ws.send(json.dumps(msg))
            return True
        except Exception as e:
            self._on_log(f"[cdp] send {method} 失败: {str(e)[:80]}")
            return False

    def start(self):
        if self._running:
            return True
        if websocket is None:
            self._on_log("[cdp] websocket-client 未安装，前置注入不可用")
            return False
        try:
            ws_url = _browser_ws_url(self.port)
        except Exception as e:
            self._on_log(f"[cdp] 取浏览器 ws 端点失败(port={self.port}): {str(e)[:100]}")
            return False
        try:
            # 长超时 recv：靠独立线程阻塞读；发送端另行 send。
            self._ws = websocket.create_connection(ws_url, max_size=None,
                                                   suppress_origin=True, timeout=30)
        except Exception as e:
            self._on_log(f"[cdp] 连接浏览器 ws 失败: {str(e)[:100]}")
            return False
        self._running = True
        self._thread = threading.Thread(target=self._recv_loop, daemon=True)
        self._thread.start()
        # 浏览器级 auto-attach：waitForDebuggerOnStart 让新目标暂停，flatten 用 sessionId 寻址
        self._send("Target.setAutoAttach",
                   {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True})
        self._on_log(f"[cdp] 前置注入器已启动 (port={self.port})")
        return True

    def _recv_loop(self):
        while self._running:
            try:
                raw = self._ws.recv()
            except Exception:
                break
            if not raw:
                continue
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("method") == "Target.attachedToTarget":
                self._handle_attached(msg.get("params", {}))

    def _handle_attached(self, params):
        sid = params.get("sessionId")
        ti = params.get("targetInfo", {})
        ttype = ti.get("type")
        url = ti.get("url") or ""
        waiting = params.get("waitingForDebugger")
        if not sid:
            return
        self._attached.append({"type": ttype, "url": url[:70], "waiting": waiting})
        # 只对页面/iframe 目标注入前置脚本（worker/other 跳过但仍需放行+传播 auto-attach）。
        # 关键顺序：先 Page.enable（把 addScript 挂上文档创建生命周期，否则命令被接受却不生效），
        # 再 addScriptToEvaluateOnNewDocument，最后才 runIfWaitingForDebugger 放行——
        # 保证脚本在该目标首个文档脚本前登记完毕。全部在 waitForDebugger 暂停窗口内完成。
        if ttype in ("page", "iframe"):
            self._send("Page.enable", None, session_id=sid)
            if self._send("Page.addScriptToEvaluateOnNewDocument",
                          {"source": self.source}, session_id=sid):
                self._injected += 1
        # 让该子会话继续 auto-attach 其嵌套子目标（OOPIF 可多层嵌套）
        self._send("Target.setAutoAttach",
                   {"autoAttach": True, "waitForDebuggerOnStart": True, "flatten": True},
                   session_id=sid)
        # 放行暂停的目标（未暂停时此命令无副作用）。放在最后：确保上面的 addScript 先登记。
        self._send("Runtime.runIfWaitingForDebugger", None, session_id=sid)

    def stats(self):
        return {"attached": len(self._attached), "injected": self._injected,
                "targets": self._attached}

    def stop(self):
        self._running = False
        try:
            if self._ws:
                self._ws.close()
        except Exception:
            pass
        self._ws = None
