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


class AccountRegistry:
    """账号（email）的运行时排他。

    存在理由是硬约束而非优化：driver.py 的 _clear_singleton_locks 会无条件删除
    Chrome profile 的 Singleton 锁，其注释明确写着"本应用通过 open_browsers 保证
    同一 profile 单实例，可安全清理"。两个 worker 若同时使用同一 email，就会互删
    对方的锁，导致浏览器随机崩溃。

    单进程内用内存 set 即可，无需落库：进程重启意味着所有持有者都已消失。
    """

    def __init__(self, app_state):
        self._lock = threading.Lock()
        self._claimed = {}                 # email -> worker_id
        self._app_state = app_state        # 读 open_browsers（用户手动打开的会话）

    def claim(self, email):
        """尝试独占一个账号。成功返回 True。

        与用户手动打开的浏览器（open_browsers）互斥：那也是同一个 profile 目录。"""
        if not email:
            return False
        with self._lock:
            if email in self._claimed:
                return False
            if email in self._app_state.open_browsers:
                return False
            holder = _current_worker.get()
            self._claimed[email] = holder.worker_id if holder else '-'
            return True

    def release(self, email):
        with self._lock:
            self._claimed.pop(email, None)

    def try_open_manual(self, email):
        """用户手动打开浏览器时的预约，返回 (ok, reason)。

        必须与 claim() 共用同一把锁：否则"检查 worker 是否占用"与"登记
        open_browsers"之间存在窗口，worker 可能在此期间抢占同一 profile。
        """
        if not email:
            return False, "未指定账号"
        with self._lock:
            if email in self._claimed:
                return False, f"{email} 正在被任务 worker 使用（{self._claimed[email]}），请等待其完成"
            if email in self._app_state.open_browsers:
                return False, f"{email} 已有浏览器打开"
            self._app_state.open_browsers.add(email)
            return True, ""

    def is_claimed(self, email):
        with self._lock:
            return email in self._claimed

    def holder_of(self, email):
        with self._lock:
            return self._claimed.get(email)

    def release_all(self):
        with self._lock:
            self._claimed.clear()

    def snapshot(self):
        with self._lock:
            return dict(self._claimed)


class PaymentCardRegistry:
    """支付卡的运行时排他（in-flight 登记）。

    为什么需要它：registration.py 的选卡资格闸门（一卡绑一账号 / 单卡 24h≤2 次 /
    3DS 冷却）全部从 DB 实时派生，且 _eligible_cards 是**进入时一次性快照**。
    并发下两个 worker 会同时把同一张卡判为合格，各自拿去给不同账号支付，等 DB
    记录写下时违规已经发生——闸门形同虚设。

    本注册表补的正是"判定合格"到"写入结果"之间的时间差窗口，与 DB 派生规则叠加
    使用（双闸门）。内存态，进程崩溃即丢，此时 DB 规则仍是第二道闸。
    """

    def __init__(self):
        self._lock = threading.Lock()
        self._in_flight = {}               # card_number -> email

    def try_acquire(self, card_number, email):
        """占用一张支付卡。已被其它账号占用则返回 False。

        同一账号重复占用同一张卡视为成功（幂等），因为串行路径内允许一张卡
        为同一账号支付多笔。"""
        if not card_number:
            return False
        with self._lock:
            holder = self._in_flight.get(card_number)
            if holder is not None and holder != email:
                return False
            self._in_flight[card_number] = email
            return True

    def release(self, card_number):
        with self._lock:
            self._in_flight.pop(card_number, None)

    def in_flight_numbers(self):
        with self._lock:
            return set(self._in_flight)

    def release_all(self):
        with self._lock:
            self._in_flight.clear()


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


class ClaimReaper:
    """回收失联 worker 领取的卡。

    worker 崩溃或卡死时，它领取的卡会永远停在 processing，卡池被慢慢吃空。
    本线程周期性把超时的记录退回 pending。

    已知限制：无法区分"worker 已死"与"worker 很慢"。若某账号处理耗时超过阈值
    而 worker 仍活着，其卡会被回收并可能被另一 worker 重复尝试——不会写坏数据
    （mark_success 按 binding_id 更新），最坏是浪费一次绑卡机会。彻底杜绝需要
    worker 心跳续期，本期未做。阈值默认 20 分钟，应显著大于单账号正常耗时。
    """

    INTERVAL_SECONDS = 60

    def __init__(self, card_binding_model, app_state, timeout_minutes):
        self.card_binding_model = card_binding_model
        self.app_state = app_state
        self.timeout_minutes = timeout_minutes
        self._stop = threading.Event()
        self._thread = None

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._loop, daemon=True, name='claim-reaper')
        self._thread.start()

    def stop(self):
        self._stop.set()

    def reap_once(self):
        """执行一次回收，返回回收行数。异常不外溢——回收失败不该拖垮服务。"""
        try:
            n = self.card_binding_model.reap_stale(self.timeout_minutes)
            if n:
                self.app_state.add_log(
                    f"[回收] {n} 张卡领取超过 {self.timeout_minutes} 分钟无进展"
                    f"（worker 疑似失联），已重置为 pending")
            return n
        except Exception as e:
            self.app_state.add_log(f"[回收] 执行失败: {e}")
            return 0

    def _loop(self):
        while not self._stop.is_set():
            self._stop.wait(self.INTERVAL_SECONDS)
            if self._stop.is_set():
                break
            self.reap_once()


MIN_WORKERS = 1
MAX_WORKERS = 4        # 有头 Chrome 每实例约 300-500MB，再高内存与风控都吃不消


def clamp_workers(n, log=None):
    """把配置的并发度夹到 [1, 4]，越界时说明原因。"""
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = MIN_WORKERS
    clamped = max(MIN_WORKERS, min(MAX_WORKERS, n))
    if clamped != n and log:
        log(f"并发度 {n} 超出允许范围 {MIN_WORKERS}-{MAX_WORKERS}，已调整为 {clamped}")
    return clamped


class WorkerPool:
    """把工作分发给多个 worker 线程执行。

    max_workers=1 时走**同线程直接调用**分支，不创建任何线程——串行等价性由
    结构保证，而不是靠"线程池恰好只有一个线程"这种偶然。
    """

    def __init__(self, app_state, max_workers):
        self.app_state = app_state
        self.max_workers = clamp_workers(max_workers, log=app_state.add_log)
        self.workers = app_state.ensure_workers(self.max_workers)
        # 串行时聚合日志不加 [Wn] 前缀，保持与改造前逐字一致；
        # 并行时必须加，否则聚合流里分不清哪条来自哪个浏览器。
        app_state.parallel_mode = not self.is_serial

    @property
    def is_serial(self):
        return self.max_workers == 1

    def _run_in_worker(self, worker, fn, *args):
        """worker 线程体：绑定上下文 → 执行 → 收尾。

        绑定必须在这里做：contextvars 不跨线程继承，漏掉的话该 worker 的日志
        会落到聚合流而不是它自己的分栏。

        用 token 复位而非直接 set(None)：串行分支跑在协调线程上，若不复位，
        绑定会泄漏到后续阶段，让阶段之间的日志被错记到 W1 名下。"""
        token = _current_worker.set(worker)
        worker.busy = True
        try:
            return fn(worker, *args)
        finally:
            worker.busy = False
            worker.current_action = "空闲"
            worker.stop_screenshot_loop()
            worker.clear_active_driver()
            _current_worker.reset(token)

    def map(self, items, fn):
        """把 items 分发给 worker 并发执行，**全部完成后**返回结果列表（barrier）。

        结果与 items 顺序一一对应；某项抛异常时该位置为 None，不影响其它项
        （异常已在 worker 内被吞掉并记日志——一个账号失败不该拖垮整批）。
        """
        items = list(items)
        if not items:
            return []

        results = [None] * len(items)
        next_index = [0]
        index_lock = threading.Lock()

        def consume(worker):
            while True:
                with index_lock:
                    if next_index[0] >= len(items):
                        return
                    i = next_index[0]
                    next_index[0] += 1
                results[i] = self._safe_call(worker, fn, items[i])

        self._dispatch(consume)
        return results

    def run_until_empty(self, produce, fn):
        """无界模式：worker 反复调 produce() 领取下一份工作，返回 None 则退出。

        用于事先不知道总量的场景（如"注册新号直到卡池耗尽"）。produce 必须是
        线程安全的——通常它就是一次原子的 claim_batch。
        """
        results = []
        results_lock = threading.Lock()

        def consume(worker):
            while True:
                item = produce()
                if item is None:
                    return
                r = self._safe_call(worker, fn, item)
                with results_lock:
                    results.append(r)

        self._dispatch(consume)
        return results

    def _dispatch(self, consume):
        """把 consume(worker) 跑在每个 worker 上，等全部结束。

        串行时直接同线程调用，不创建线程——这是 max_workers=1 与改造前等价的
        结构性保证。"""
        if self.is_serial:
            self._run_in_worker(self.workers[0], consume)
            return

        threads = [
            threading.Thread(target=self._run_in_worker, args=(w, consume),
                             daemon=True, name=f'pool-{w.worker_id}')
            for w in self.workers
        ]
        for t in threads:
            t.start()
        for t in threads:
            t.join()

    def _safe_call(self, worker, fn, item):
        """执行单项工作，异常不外溢。

        InterruptedError 是用户停止，转成全局 stop_requested 让其它 worker 也收敛；
        其它异常只记日志——单个账号出错不应终止整批。"""
        try:
            return fn(worker, item)
        except InterruptedError:
            self.app_state.stop_requested = True
            self.app_state.dispatch_print("已中断")
            return None
        except Exception as e:
            self.app_state.dispatch_print(f"处理出错: {e}")
            return None
