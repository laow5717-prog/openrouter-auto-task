"""并行执行的 worker 运行时。

每个 worker 独占一个浏览器实例与一套隔离状态（日志/截图/活跃 driver），
互不干扰。AppState 保留全局聚合层（is_running、总计数、停止标志）。

关键约束（改这里之前务必读）：
1. Playwright sync API 是**线程绑定**的。BrowserSession 必须在创建它的线程内
   使用到底，禁止跨线程传递 driver 对象——跨线程 quit 会让挂起的调用永久 hang。
2. 同一 Chrome profile 目录（按 email 命名）**不能并发**。driver.py 的
   _clear_singleton_locks 会无条件删锁，两个 worker 撞同一 email 会互删对方的锁。
   账号排他由 AccountRegistry 保证。
3. contextvars **不跨线程继承**。每个 worker 线程必须在入口处调用
   bind_current_worker()，否则其日志会落到全局聚合流而非本 worker 分栏。
"""

import contextvars
import threading
import collections
from datetime import datetime


# 当前线程所属的 worker。默认 None = 不在 worker 上下文中（串行路径/请求线程）。
_current_worker = contextvars.ContextVar('current_worker', default=None)


def bind_current_worker(worker_state):
    """把当前线程绑定到指定 worker。必须在 worker 线程入口调用。

    contextvars 不会被新线程继承（新线程起始于空 context），这既是必须显式调用
    的原因，也正好给了我们想要的隔离：worker 之间不会互相看到对方的绑定。"""
    _current_worker.set(worker_state)


def get_current_worker():
    return _current_worker.get()


class WorkerState:
    """单个 worker 的隔离状态。

    从原 AppState 下沉而来：日志缓冲、MJPEG 帧、活跃 driver、持续截图线程。
    """

    MAX_LOGS = 500

    def __init__(self, worker_id):
        self.worker_id = worker_id            # 'W1' / 'W2' ...
        self.current_action = "空闲"
        self.busy = False

        # 日志：deque 自动淘汰旧条目；log_seq 单调递增，供前端按序增量拉取
        self._logs = collections.deque(maxlen=self.MAX_LOGS)
        self._log_start = 0                   # 已被 deque 淘汰的条数
        self.lock = threading.Lock()

        # MJPEG 流缓冲
        self.last_frame = None
        self.frame_lock = threading.Lock()

        # 活跃 driver（供停止时释放引用；实际 quit 由持有它的 worker 线程完成）
        self._active_driver = None
        self._active_driver_lock = threading.Lock()

        # 持续截图线程
        self._screenshot_driver = None
        self._screenshot_thread = None
        self._screenshot_stop = threading.Event()

    # ---------- 日志 ----------

    def add_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            if len(self._logs) == self.MAX_LOGS:
                self._log_start += 1           # 有一条被 deque 挤掉
            self._logs.append(f"[{timestamp}] {message}")

    @property
    def log_seq(self):
        """已产生的日志总条数（含已淘汰的），前端据此判断有无新增。"""
        with self.lock:
            return self._log_start + len(self._logs)

    def get_logs(self, start_index=0):
        """返回从全局序号 start_index 起的日志，以及下一次应传入的序号。"""
        with self.lock:
            end = self._log_start + len(self._logs)
            # start_index 落在已淘汰区间时，从当前缓冲最早一条开始给
            offset = max(0, start_index - self._log_start)
            return list(self._logs)[offset:], end

    # ---------- 截图帧 ----------

    def update_frame(self, frame_bytes):
        with self.frame_lock:
            self.last_frame = frame_bytes

    def get_frame(self):
        with self.frame_lock:
            return self.last_frame

    # ---------- 活跃 driver ----------

    def set_active_driver(self, driver):
        with self._active_driver_lock:
            self._active_driver = driver

    def clear_active_driver(self):
        with self._active_driver_lock:
            self._active_driver = None

    # ---------- 持续截图 ----------

    def start_screenshot_loop(self, driver):
        self._screenshot_driver = driver
        self._screenshot_stop.clear()
        if self._screenshot_thread and self._screenshot_thread.is_alive():
            return

        def _loop():
            # 注意：本线程不继承 worker 绑定，也不需要——它只截图不打日志。
            # 若将来在此处加日志，必须先 bind_current_worker(self)。
            while not self._screenshot_stop.is_set():
                try:
                    d = self._screenshot_driver
                    if d:
                        self.update_frame(d.get_screenshot_as_png())
                except Exception:
                    pass
                self._screenshot_stop.wait(0.3)

        self._screenshot_thread = threading.Thread(
            target=_loop, daemon=True, name=f'screenshot-{self.worker_id}')
        self._screenshot_thread.start()

    def stop_screenshot_loop(self):
        self._screenshot_stop.set()
        self._screenshot_driver = None

    # ---------- 供 registration.* 回调 ----------

    def make_monitor(self, app_state):
        """构造 monitor_callback(driver, step)，签名与原 AppState._monitor 一致。

        下游 registration.* 无需任何改动：它拿到的仍是一个 callable(driver, step)，
        只是现在闭包绑定到本 worker，截图与 driver 跟踪都落到本 worker 的状态上。
        """
        def _monitor(driver, step):
            if app_state.stop_requested:
                app_state.dispatch_print("收到停止请求，正在中断...")
                raise InterruptedError("User requested stop")
            self.set_active_driver(driver)
            if self._screenshot_driver is not driver:
                self.start_screenshot_loop(driver)
        return _monitor

    def reset_for_run(self):
        """一轮工作开始前清理上轮残留（帧/action），日志保留供回看。"""
        self.update_frame(None)
        self.current_action = "空闲"
