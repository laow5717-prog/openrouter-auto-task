"""
Flask 应用工厂 & AppState
"""

import os
import sys
import time
import random
import json
import threading
import contextvars
from datetime import datetime

from flask import Flask, send_from_directory, Response

from src.config import cfg, get_base_dir, get_data_dir
from src.models.database import Database
from src.models.account import AccountModel
from src.models.platform_account import PlatformAccountModel
from src.models.task import TaskModel
from src.models.card_binding import CardBindingModel
from src.models.recharge_log import RechargeLogModel
from src.models.card_group import CardGroupModel
from src.models.card_pool import CardPoolModel
from src.models.valid_card import ValidCardModel
from src.models.card_payment_state import CardPaymentStateModel
from src.models.proxy import ProxyModel
from src.models.adspower_profile import AdsPowerProfileModel
from src.models.settings import SettingsModel
from src.services.adspower import AdsPowerError
from src.utils import is_identity_terminal, is_platform_terminal
from src.services import registration, card as card_service
from src.api.routes import api
from src.web.worker import (
    WorkerState, WorkerPool, AccountRegistry, PaymentCardRegistry, ProxyRegistry,
    ClaimReaper, get_current_worker,
)


# Stripe 付款域名绕过代理直连。i-proxy 等住宅/移动代理把支付类域名（stripe.com）
# 列入黑名单、拒绝建隧道（CONNECT 返回 503），导致 checkout.stripe.com 打不开。
# 实测本机直连 stripe 正常（302），故让 stripe 走直连、其余（github/opencode）走代理。
# Stripe 结账风控看卡/账单/设备指纹，不强依赖 IP 与 opencode 会话同源，可接受本机 IP。
_PROXY_BYPASS = "*.stripe.com, stripe.com, *.stripecdn.com, b.stripecdn.com, js.stripe.com, m.stripe.com, m.stripe.network, *.stripe.network"

# 充值任务连续多少轮「完全空转」才收敛。空转 = 既没付成一张卡、可选卡集合也一张没动。
# 取 2 而不是 1 是为了吸收一轮瞬时抖动（登录/网络类故障常常只坏一轮）。
# 注意这**不是**「失败多少轮就停」——只要还在消耗卡就一直跑，见 _try_claim 的轮边界。
IDLE_ROUNDS_LIMIT = 2


# ---------- 日志归属 ----------
#
# 被劫持的 print 指向下面的模块级 dispatch_print，由它从 contextvar 解析
# 「这条日志属于哪个平台」。
#
# 为什么不能像从前那样把某个实例的绑定方法装进各模块：_patch_prints 是往模块的
# globals 里塞一个 print 名字，两个平台各装一次的话，后装的直接覆盖先装的 ——
# 于是**所有平台的 print 都进同一个实例的日志流**。src.payments.stripe_checkout
# 尤其致命：opencode 与 infron 的 module_names() 都含它。
_RUN_CTX = contextvars.ContextVar('run_context', default=None)

# _patch_prints 只该执行一次。装的是下面这个模块级函数，与实例无关，
# 重复装没有意义，反而容易掩盖「到底装的是谁」的问题。
_prints_patched = False
_patch_lock = threading.Lock()


def dispatch_print(*args, **kwargs):
    """所有被劫持的 print 的统一入口。从 contextvar 解析归属后转交给对应的 ctx。

    无归属时（未绑定的线程、导入期的零星 print）如实退化成 builtins.print，
    **不猜平台**——猜错就是把日志写进另一个平台的流里，比丢掉更难查。
    """
    ctx = _RUN_CTX.get()
    if ctx is None:
        import builtins
        builtins.print(*args, **kwargs)
        return
    ctx.dispatch_print(*args, **kwargs)


def patch_prints():
    """把各业务模块的 print 换成 dispatch_print。进程内只生效一次。

    模块名由 adapter 自报（module_names），加平台时不用回来改硬编码列表。
    """
    global _prints_patched
    with _patch_lock:
        if _prints_patched:
            return
        _prints_patched = True

    registration.print = dispatch_print
    try:
        from src.browser import driver as browser_module
        browser_module.print = dispatch_print
    except Exception:
        pass

    import src.platforms as platforms
    mods = ['src.services.github_signup_service',
            'src.services.email', 'src.services.captcha']
    for _slug in platforms.all_slugs():
        mods.extend(platforms.get(_slug).module_names())
    import importlib
    for _mod in mods:
        try:
            importlib.import_module(_mod).print = dispatch_print
        except Exception:
            pass


def _to_pw_proxy(row):
    """proxies 表 row（host/port/username/password）→ Playwright proxy dict。
    server 只放 scheme+host+port，凭据走 username/password 字段（不 URL 内嵌）。
    bypass 让 Stripe 付款域名直连（代理商封了 stripe，见 _PROXY_BYPASS）。"""
    d = {"server": f"http://{row['host']}:{row['port']}", "bypass": _PROXY_BYPASS}
    if row.get('username'):
        d["username"] = row['username']
        d["password"] = row.get('password') or ''
    return d


class SharedResources:
    """跨平台共享的单例资源。

    多平台并发时**每个平台一个 AppState，但只有一个 SharedResources**。
    放进这里的依据不是「看起来像全局」，而是每一项都有具体的物理理由：

    - db / models —— Database 自带锁且 check_same_thread=False，本身线程安全；
      model 方法已全部显式收 platform 参数，天然按平台隔离。
    - open_browsers —— Chrome profile 目录按 email 命名，同一 email 不能被打开两次。
    - account_registry —— 同上，账号的排他是 email 级的，跨平台也必须互斥。
    - payment_registry —— _in_flight 全局是**硬要求**：同一张卡在几秒内被两个商户
      同时请求授权是典型盗刷特征，会直接触发发卡行风控。
    - proxy_registry —— 出口 IP 是物理资源，反关联的全部意义就在于不重复。
    - _adspower_client —— 其 _throttle 限流状态是**实例级**的，两个实例等于两倍
      请求速率，会撞 AdsPower 的接口频率限制。
    - _adspower_pool —— 其 _lock 串行化「挑代理→建环境→撞配额→回收→重试」整条链，
      拆开会活锁（A 刚删出的配额被 B 抢走），池的 docstring 里已写明。
    """

    def __init__(self, db, models):
        self.db = db
        self.models = models

        # 按账号独立跟踪的浏览器查看会话（不阻塞全局任务）
        self.open_browsers = set()

        # 并发排他：账号（Chrome profile 单实例约束）与支付卡（选卡闸门时间差）。
        # 三者都必须跨平台共享——理由见类 docstring。
        self.account_registry = AccountRegistry(self)
        self.payment_registry = PaymentCardRegistry()
        self.proxy_registry = ProxyRegistry()

        # AdsPower 指纹浏览器接入（生效配置的 enabled 为假时恒为 None，全链路不受影响）。
        # 惰性构造：启动时不去连 AdsPower，免得客户端没开就起不来服务。
        self._adspower_pool = None
        self._adspower_client = None
        # 建池时用的 (api_key, base_url)。UI 上改了配置就与它对不上，据此重建。
        # 光有「保存时主动 invalidate」是不够的：那条路径要求每个改配置的入口都记得调，
        # 漏一个就退化成「保存成功但毫无变化」——这类 bug 从现象上完全看不出根因。
        # 在使用点比对一次，是让新值生效这件事不依赖调用方的自觉。
        self._adspower_creds = None
        # 只保护「客户端/池的惰性构造」。注意与 AppState._adspower_started 的锁不是
        # 同一把——那个是每平台各自的收尾集合，混用会让两个平台互相阻塞。
        self._adspower_lock = threading.Lock()

        # AdsPower 环境配额的按平台仲裁（7/4，总 11，可借用）。
        # 必须共享：配额是两个平台共用的物理资源，各持一个仲裁器等于没有仲裁。
        from src.browser.adspower_quota import AdsPowerQuota
        self.quota = AdsPowerQuota(
            total=getattr(cfg.adspower, 'total_quota', None),
            reserved=getattr(cfg.adspower, 'platform_quota', None),
        )


class AppState:
    """单个平台的运行时状态。

    分层（自外向内三层）：
      SharedResources —— 跨平台共享的单例资源（DB、models、三个排他注册表、AdsPower 池）
      AppState        —— **每个平台一个**，持有「这一次运行」的状态
      WorkerState     —— 每个浏览器实例的隔离状态（src/web/worker.py）

    类名保留 AppState 而不是改成 PlatformRunContext，是为了不动几百处既有引用；
    但语义已经从「全局唯一」变成「每平台一个」。共享资源通过 property 委托给
    self.shared，因此方法体里的 self.db / self.models / self.account_registry
    等写法**一行都不用改**——这是把改动面压到最小的关键。

    始终存在一个主 worker 'W1'：串行路径（单账号充值等）与 max_workers=1 时
    都走它，因此下列 set_active_driver / _stop_screenshot_loop / _monitor 等
    委托方法的行为与并行化改造前完全一致。
    """

    PRIMARY_WORKER_ID = 'W1'

    DEFAULT_PLATFORM = 'opencode'

    def __init__(self, db, models, platform=None, shared=None):
        # shared 为 None 时自建一个——保持既有调用方（含测试）不用改。
        self.shared = shared if shared is not None else SharedResources(db, models)

        # 本实例负责的平台。曾经是「当前流水线跑哪个平台」的全局字段，
        # 两个平台同时跑会互相覆盖；现在每平台一个实例，它就是这个实例的身份。
        self.platform = platform or self.DEFAULT_PLATFORM

        self.is_running = False
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "空闲"
        self.logs = []
        self.lock = threading.Lock()

        # 当前信用卡驱动任务 ID。由 run_batch_task 写入、收尾时清空，
        # 供 /api/card/history/cleanup 保护本任务的未完成行。
        self.current_card_task_id = None

        # worker 运行时。W1 是主 worker，恒存在。
        # workers 只增不减（旧 worker 的日志留着供回看）；对外展示以
        # active_worker_count 为准，见 active_workers()。
        self.workers = {self.PRIMARY_WORKER_ID: WorkerState(self.PRIMARY_WORKER_ID)}
        self.active_worker_count = 1
        self._workers_lock = threading.Lock()

        # 是否处于并行模式（由 WorkerPool 依 max_workers 设置）。仅影响聚合日志
        # 是否带 [Wn] 前缀，使串行输出与改造前逐字一致。
        self.parallel_mode = False

        # 本次运行启动过的环境（profile_id）。任务收尾时逐个 stop，避免用户点停止或
        # 任务异常退出后留下一堆开着的浏览器（每个都吃几百 MB 内存）。
        # 按平台各自持有：收尾时只该关自己起的，不能连另一个平台的一起关。
        self._adspower_started = set()
        self._adspower_started_lock = threading.Lock()

    # ---------- 共享资源的委托 ----------
    # 这些 property 的存在意义：让方法体里的 self.db / self.models /
    # self.account_registry 等既有写法保持不变。改成 self.shared.db 要动几百处，
    # 而 app.py 里跑通的 opencode 流程是本项目最贵的资产。

    @property
    def db(self):
        return self.shared.db

    @property
    def models(self):
        return self.shared.models

    @property
    def open_browsers(self):
        return self.shared.open_browsers

    @property
    def account_registry(self):
        return self.shared.account_registry

    @property
    def payment_registry(self):
        return self.shared.payment_registry

    @property
    def proxy_registry(self):
        return self.shared.proxy_registry

    @property
    def _adspower_pool(self):
        return self.shared._adspower_pool

    @_adspower_pool.setter
    def _adspower_pool(self, v):
        self.shared._adspower_pool = v

    @property
    def _adspower_client(self):
        return self.shared._adspower_client

    @_adspower_client.setter
    def _adspower_client(self, v):
        self.shared._adspower_client = v

    @property
    def _adspower_creds(self):
        # 必须与它守护的 client 存在同一处。放在 AppState 上的话每个平台各记一份，
        # 而 client 是共享的：A 平台按新配置重建完，B 平台一看自己记的还是旧值，
        # 又拆掉重建一次。两个平台会来回互相拆对方刚建好的池。
        return self.shared._adspower_creds

    @_adspower_creds.setter
    def _adspower_creds(self, v):
        self.shared._adspower_creds = v

    @property
    def _adspower_lock(self):
        return self.shared._adspower_lock

    @property
    def quota(self):
        return self.shared.quota

    # ---------- AdsPower 环境池 ----------

    def adspower_settings(self):
        """AdsPower 的**生效配置**：UI 存进 DB 的覆盖值优先，没设过回落 config.yaml。

        每次读库而不是缓存：这三项在 UI 上随时可改，缓存一份就得再解决「什么时候
        失效」的问题，而读一行 sqlite 的开销远小于那个复杂度。
        """
        return self.models['settings'].adspower_effective(cfg.adspower)

    @property
    def adspower_enabled(self):
        return bool(self.adspower_settings()['enabled'])

    def _ensure_adspower(self):
        """惰性创建 AdsPower 客户端与环境池。未启用时返回 (None, None)。

        凭据（api_key / base_url）变了就**丢掉旧的重建**。不这样做的话，UI 上存了新
        key 之后进程会继续拿着旧 client 跑，界面显示保存成功、行为却毫无变化——
        这类 bug 从现象上完全看不出根因。
        """
        if not self.adspower_enabled:
            return None, None
        s = self.adspower_settings()
        creds = (s['api_key'], s['base_url'])
        with self._adspower_lock:
            if self._adspower_pool is not None and self._adspower_creds != creds:
                # 只丢引用、不去关正在跑的环境：在飞的会话还攥着旧 client，
                # 让它们各自跑完；新值只对之后创建的会话生效。
                self.add_log("[AdsPower] 配置已变更，重建客户端与环境池")
                self._adspower_pool = None
                self._adspower_client = None
            if self._adspower_pool is None:
                from src.services.adspower import AdsPowerClient
                from src.browser.adspower_driver import AdsPowerProfilePool
                self._adspower_client = AdsPowerClient(s['base_url'], s['api_key'])
                self._adspower_pool = AdsPowerProfilePool(
                    self._adspower_client,
                    self.models['adspower_profile'],
                    group_id=cfg.adspower.group_id,
                    reclaim_batch=cfg.adspower.reclaim_batch,
                    ua_systems=cfg.adspower.ua_systems,
                    log=self.add_log,
                    # 回收时跳过正在被 worker 使用的账号——删掉正在用的环境
                    # 会让那个 worker 的浏览器凭空消失。
                    is_busy=self.account_registry.is_claimed,
                )
                self._adspower_creds = creds
            return self._adspower_client, self._adspower_pool

    def browser_factory(self, track_for_teardown=True):
        """返回 callable(email) -> BrowserSession；未启用 AdsPower 时返回 None。

        下游（registration.recharge_account / signup_one / _subscribe_one_account）
        统一以「factory 为 None 就走原路径」的方式接入，因此关掉开关时代码路径
        与接入前完全一致。

        track_for_teardown=False 用于**手动打开的会话**（账号列表的「查看」）：
        不把 profile 记进 _adspower_started。记进去的话，任何一次流水线跑完调用
        _stop_started_adspower() 都会顺手关掉用户正在看的那个浏览器，而且那里还有
        一段「把本平台仍持有的配额全部归还」的对账逻辑，会把手动会话的那一份也算成
        泄漏还掉——等用户真关掉浏览器时 _on_closed 再还一次，配额凭空多出来一个。
        手动会话的配额由它自己的 _on_closed 归还，生命周期与任务无关。
        """
        client, pool = self._ensure_adspower()
        if pool is None:
            return None
        from src.browser.adspower_driver import create_driver_adspower

        def _factory(email):
            # ⚠️ 配额必须在**进池之前**取。池的 _lock 串行化「挑代理→建环境→撞配额
            # →回收→重试」整条链；持着池锁再去等配额的话，释放方永远拿不到池锁来
            # 删环境 —— 直接死锁。顺序只能是「先配额，后池」。
            #
            # 拿不到是**等待**而不是报错：配额是两个平台共用的资源，对方跑完就会
            # 释放，直接判失败会让账号白白进失败集合。
            # 手动会话（track_for_teardown=False）**不看 stop_requested**。它是跨任务
            # 残留的标志：上一次任务被用户停掉后一直是 True，而只有三条流水线入口会
            # 复位它，「打开浏览器」不会。挂着它的话，只要之前停过一次任务，此后每次
            # 点「查看」都会在第一次检查点立刻放弃，报「等待配额超时」——而配额其实是
            # 空的。手动开的浏览器本来也不该被某个任务的停止操作掐掉。
            _should_stop = (lambda: self.stop_requested) if track_for_teardown else None
            if not self.quota.acquire(self.platform,
                                      timeout=cfg.adspower.quota_wait_seconds,
                                      should_stop=_should_stop):
                snap = self.quota.snapshot()
                raise AdsPowerError(
                    f"等待 AdsPower 环境配额超时（本平台 {snap['held'].get(self.platform, 0)}/"
                    f"{snap['reserved'].get(self.platform, '?')}，总 {snap['total_held']}/{snap['total']}）")

            released = threading.Event()

            def _give_back():
                # 幂等：close_driver 可能被重复调用，多还一次会把别人的额度也放掉。
                if not released.is_set():
                    released.set()
                    self.quota.release(self.platform)

            try:
                session = create_driver_adspower(email, pool, client)
            except BaseException:
                _give_back()          # 没起来就立刻还，否则额度只出不进
                raise

            session._on_closed = _give_back
            pid = getattr(session, 'adspower_profile_id', None)
            if pid and track_for_teardown:
                # 用本平台自己的锁，不是共享的 _adspower_lock —— 那把锁保护的是
                # 客户端/池的惰性构造，拿它护每平台各自的集合会让两个平台互相阻塞。
                with self._adspower_started_lock:
                    self._adspower_started.add(pid)
            return session

        return _factory

    def _stop_started_adspower(self):
        """收尾：关掉本次运行启动过的所有环境（幂等，异常不外溢）。"""
        with self._adspower_started_lock:
            pending = list(self._adspower_started)
            self._adspower_started.clear()
        client = self._adspower_client
        # 配额对账：正常路径由 close_driver 的 _on_closed 逐个归还，但异常路径可能
        # 漏掉（进程被杀、会话对象没走到 close_driver）。任务收尾时本平台不该再持有
        # 任何额度，把残留的全还回去——只出不进的话，几个账号之后就再也起不来浏览器。
        leaked = self.quota.held(self.platform)
        if leaked:
            for _ in range(leaked):
                self.quota.release(self.platform)
            self.add_log(f"[AdsPower] 收尾归还了 {leaked} 个泄漏的环境配额")

        if not pending or client is None:
            return
        closed = 0
        for pid in pending:
            try:
                client.stop_profile(pid)
                closed += 1
            except Exception:
                pass   # 已经关掉的会报错，属正常
        if closed:
            self.add_log(f"[AdsPower] 收尾关闭了 {closed} 个浏览器环境")

    # ---------- worker 管理 ----------

    @property
    def primary_worker(self):
        return self.workers[self.PRIMARY_WORKER_ID]

    def ensure_workers(self, count):
        """确保存在 count 个 worker（W1..Wn），返回有序列表。

        字典只增不减（已存在的 worker 复用，保留其日志供回看），但
        active_worker_count 会跟着降，使对外展示的 worker 数正确反映本次并发度。"""
        with self._workers_lock:
            for i in range(1, count + 1):
                wid = f'W{i}'
                if wid not in self.workers:
                    self.workers[wid] = WorkerState(wid)
            self.active_worker_count = count
            return [self.workers[f'W{i}'] for i in range(1, count + 1)]

    def get_worker(self, worker_id=None):
        """按 id 取 worker；不传或不存在时回落到主 worker（保证老接口可用）。"""
        if not worker_id:
            return self.primary_worker
        return self.workers.get(worker_id, self.primary_worker)

    def active_workers(self, count=None):
        """当前**生效**的 worker 列表（W1..W{active_worker_count}）。

        必须按 active_worker_count 截断，而不是返回 workers 字典的全部内容：
        workers 只增不减（保留旧 worker 的日志供回看），若直接暴露全部，用户把
        max_workers 从 4 调回 1 做应急回滚后，前端仍会看到 4 个 worker 而渲染
        并行分栏布局，回滚等于失效。"""
        limit = count if count is not None else self.active_worker_count
        return [self.workers[f'W{i}'] for i in range(1, limit + 1)
                if f'W{i}' in self.workers]

    # ---------- 聚合日志 ----------

    def add_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{timestamp}] {message}")
            if len(self.logs) > 1000:
                self.logs.pop(0)

    def get_logs(self, start_index=0):
        with self.lock:
            return list(self.logs[start_index:])

    # ---------- 委托给主 worker（向后兼容的旧接口）----------

    def update_frame(self, frame_bytes):
        self.primary_worker.update_frame(frame_bytes)

    def get_frame(self):
        return self.primary_worker.get_frame()

    def _stop_screenshot_loop(self):
        self.primary_worker.stop_screenshot_loop()

    def set_active_driver(self, driver):
        self.primary_worker.set_active_driver(driver)

    def clear_active_driver(self):
        self.primary_worker.clear_active_driver()

    def _monitor(self, driver, step):
        """串行路径用的 monitor 回调（绑定到主 worker）。"""
        return self.primary_worker.make_monitor(self)(driver, step)

    # ---------- 停止 ----------

    def force_stop(self):
        """协作式停止：设置标志、停所有 worker 的截图。不从本线程 quit driver。

        driver 由执行任务的工作线程持有；从请求线程跨线程 quit 会让工作线程里
        正在进行的 Patchright/Playwright sync 操作永久 hang（sync API 非线程安全，
        transport 被关后挂起的调用等不到响应）。改为设置 stop_requested，工作线程
        在各自的 should_stop / _monitor 检查点抛出中断、冒泡到其 finally 里 close_driver
        自行关闭浏览器。三条任务流程（register_one_account / register_and_bind_cards /
        recharge_account）都有 finally close_driver，故无需在此额外 quit。

        并发下同理：每个 worker 在自己的检查点退出并关自己的浏览器。"""
        self.stop_requested = True
        for w in list(self.workers.values()):
            w.stop_screenshot_loop()
            w.clear_active_driver()
        self.add_log("已请求停止任务（工作线程将在下个检查点安全退出并关闭浏览器）")

    def set_action(self, worker, text):
        """设置当前动作描述。

        始终写进 worker 自己的字段（前端分栏用）；串行时同步写全局字段，让老的
        单栏视图与改造前表现一致。并行时全局字段由流水线写聚合摘要，避免多个
        worker 互相覆盖成一个抖动不定的值。"""
        worker.current_action = text
        if not self.parallel_mode:
            self.current_action = text

    # ---------- 日志路由 ----------

    def dispatch_print(self, *args, **kwargs):
        """把一条日志写进**本 ctx** 的分栏与聚合流。

        这是「已经知道归属」之后的写入端。归属的解析在模块级的 `dispatch_print`
        里做——被劫持的 print 指向那个函数，不是这个绑定方法。
        """
        sep = kwargs.get('sep', ' ')
        msg = sep.join(map(str, args))
        w = get_current_worker()
        if w is not None:
            w.add_log(msg)
            # 串行时不加前缀：聚合流与改造前逐字一致（parallel_mode 由 WorkerPool 设置）
            self.add_log(f"[{w.worker_id}] {msg}" if self.parallel_mode else msg)
        else:
            self.add_log(msg)
        import builtins
        builtins.print(*args, **kwargs)

    # 旧名保留：内部调用点众多，且语义等价（未绑定 worker 时行为与从前一致）
    _hooked_print = dispatch_print

    def bind_logs(self):
        """把本 ctx 绑成当前线程/协程的日志归属，返回可 reset 的 token。

        用法（每个会跑业务代码的新线程入口都要来一次）：

            token = ctx.bind_logs()
            try:
                ...
            finally:
                ctx.unbind_logs(token)

        ⚠️ contextvars **不跨线程继承**。漏绑一处的表现是那条链路的日志跑到另一个
        平台的日志流里去，不报错、不崩溃——所以每个绑定点都得有对应测试。
        """
        return _RUN_CTX.set(self)

    @staticmethod
    def unbind_logs(token):
        try:
            _RUN_CTX.reset(token)
        except (ValueError, LookupError):
            # token 来自别的 context（跨线程误用）时 reset 会抛。吞掉即可：
            # 线程结束后它的 context 本就随之消失。
            pass

    def run_batch_task(self, count, card_info_list, login_password, max_bindable_cards, captcha_api_key):
        self.is_running = True
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = f"Starting batch: {count}"
        self.update_frame(None)

        # 劫持 print
        self._patch_prints()

        # 记录任务
        task_id = self.models['task'].create('batch', config={
            'count': count, 'has_cards': bool(card_info_list),
        })
        # 把 task_id 记到状态上，供 /api/card/history/cleanup 保护本任务的未完成行。
        # 此前这个字段只在 __init__ 里被置 None、再没人写过，于是清理接口拿到的永远是
        # None —— 而 cleanup_stale_pending(None) 走的是**无条件删除所有 pending/processing**
        # 的分支。任务运行中点一下清理，正在跑的这个任务的绑卡记录就被删了。
        self.current_card_task_id = task_id

        self._hooked_print(f"开始批量任务，目标: {count}")

        try:
            for i in range(count):
                if self.stop_requested:
                    self._hooked_print("用户停止了任务")
                    break

                self.current_action = f"注册中 ({i+1}/{count})..."

                try:
                    email, password, success = registration.register_one_account(
                        db=self.db,
                        account_model=self.models['account'],
                        card_info_list=card_info_list,
                        login_password=login_password,
                        monitor_callback=self._monitor,
                        max_bindable_cards=max_bindable_cards,
                        captcha_api_key=captcha_api_key,
                    )

                    if success:
                        self.success_count += 1
                    else:
                        self.fail_count += 1
                except InterruptedError:
                    self._hooked_print("任务已中断")
                    break
                except Exception as e:
                    self.fail_count += 1
                    self._hooked_print(f"错误: {str(e)}")

                if i < count - 1 and not self.stop_requested:
                    wait_time = random.randint(cfg.batch.interval_min, cfg.batch.interval_max)
                    self._hooked_print(f"冷却中，等待 {wait_time} 秒...")
                    for _ in range(wait_time):
                        if self.stop_requested:
                            break
                        time.sleep(1)

        except Exception as e:
            self._hooked_print(f"严重错误: {e}")
        finally:
            self.clear_active_driver()
            self._stop_screenshot_loop()
            self.is_running = False
            self.current_action = "任务已完成"
            self.models['task'].update_counts(task_id, self.success_count, self.fail_count)
            self.models['task'].finish(task_id, 'completed' if not self.stop_requested else 'stopped')
            # 任务已结束，撤销保护——否则下一次清理会去保护一个已完成的任务，
            # 它的残留 pending 行反而永远清不掉。
            self.current_card_task_id = None
            self._hooked_print("任务完成")

    def _register_bind_loop(self, task_id, login_password, max_bindable_cards, captcha_api_key,
                            pool=None):
        """注册新号 + 逐张绑卡。

        消耗 task_id 下的卡池：每轮注册一个新账号并绑到 max_bindable_cards 张，
        剩余卡留给下一账号；连续失败达阈值或卡池空则结束。

        并发模型是"生产者-消费者"：每个 worker 反复原子领取一批卡（produce），
        领到就注册一个新号消耗掉，领不到就退出。卡的原子领取本身就是任务分配，
        无需额外的任务队列。pool 为 None 时新建一个串行池（供旧调用方使用）。"""
        card_binding_model = self.models['card_binding']
        if pool is None:
            pool = WorkerPool(self, 1)

        max_consecutive_failures = 3
        state = {'fail_streak': 0, 'account_index': 0}
        state_lock = threading.Lock()

        def _produce():
            """领取下一批卡；返回 None 表示该 worker 可以退出了。"""
            if self.stop_requested:
                return None
            with state_lock:
                if state['fail_streak'] >= max_consecutive_failures:
                    return None
            worker = get_current_worker() or self.primary_worker
            batch = card_binding_model.claim_batch(
                task_id, worker.worker_id, max_bindable_cards)
            if not batch:
                return None
            with state_lock:
                state['account_index'] += 1
                index = state['account_index']
            return (index, batch)

        def _register_one(worker, item):
            index, batch = item
            try:
                summary = card_binding_model.get_summary(task_id)
                self.set_action(worker, f"正在注册账号 {index} (剩余 {summary['pending']} 张卡)")

                self._hooked_print(f"\n{'=' * 50}")
                self._hooked_print(f"正在注册账号 {index}")
                self._hooked_print(f"   卡片: {', '.join('****' + r['card_display'] for r in batch)}")
                self._hooked_print(f"   进度: 成功 {summary['success']} / 失败 {summary['failed']} / 待处理 {summary['pending']}")
                self._hooked_print(f"{'=' * 50}")

                email, password, bound_count = registration.register_and_bind_cards(
                    db=self.db,
                    account_model=self.models['account'],
                    card_binding_model=card_binding_model,
                    task_id=task_id,
                    batch_records=batch,
                    login_password=login_password,
                    max_bindable_cards=max_bindable_cards,
                    captcha_api_key=captcha_api_key,
                    monitor_callback=worker.make_monitor(self),
                    # 失败的卡不占名额：本批只领了 max_bindable_cards 张，
                    # 一张被拒就再补一张，否则该账号永远绑不满
                    claim_more=lambda n: card_binding_model.claim_batch(
                        task_id, worker.worker_id, n),
                    # 因卡自身原因失败的卡在卡池标为 invalid，后续不再选中
                    card_pool_model=self.models.get('card_pool'),
                )

                if email and bound_count > 0:
                    with state_lock:
                        self.success_count += bound_count
                        state['fail_streak'] = 0
                    self._hooked_print(f"本轮绑定了 {bound_count} 张卡")
                elif not email:
                    with state_lock:
                        state['fail_streak'] += 1
                        streak = state['fail_streak']
                    self._hooked_print(f"注册失败 ({streak}/{max_consecutive_failures})，卡片退回卡池待下个账号处理")
                else:
                    with state_lock:
                        state['fail_streak'] += 1
                    # 注册成功但没绑上卡，从 DB 刷新计数
                    updated = card_binding_model.get_summary(task_id)
                    self.fail_count = updated['failed']
                    self.success_count = updated['success']
            except InterruptedError:
                self._hooked_print("任务已中断")
                raise
            except Exception as e:
                with state_lock:
                    state['fail_streak'] += 1
                self._hooked_print(f"错误: {str(e)}")
                # 异常时不标记所有卡为 failed，留待下一轮重试
                updated = card_binding_model.get_summary(task_id)
                self.fail_count = updated['failed']
                self.success_count = updated['success']
            finally:
                # 没绑掉的卡退回 pending，交给下一个账号
                card_binding_model.release_unused(task_id, worker.worker_id)

            # 间隔等待（每个 worker 独立计时，保留原有的反封控节奏）。
            # 必须先确认卡池还有剩余：否则本 worker 在卡池耗尽后仍会空等一轮，
            # 并发时每个 worker 各等一次，纯属浪费。
            remaining = card_binding_model.get_pending(task_id)
            if remaining and not self.stop_requested:
                wait_time = random.randint(cfg.batch.interval_min, cfg.batch.interval_max)
                self._hooked_print(f"等待 {wait_time} 秒后注册下一个账号...")
                for _ in range(wait_time):
                    if self.stop_requested:
                        break
                    time.sleep(1)

        if not pool.is_serial:
            self.current_action = f"阶段1b 并发注册新号（{pool.max_workers} worker）"
        pool.run_until_empty(_produce, _register_one)

        if state['fail_streak'] >= max_consecutive_failures:
            self._hooked_print(f"连续失败达到 {max_consecutive_failures} 次，停止任务")
        elif not self.stop_requested:
            self._hooked_print("所有卡已处理完毕！")

    @staticmethod
    def _card_key(number):
        """卡号比对键：去空格。卡池里的卡号可能带内部空格，登记表里存的是原样字符串。"""
        return str(number or '').replace(' ', '')

    def _exclude_used_this_run(self, platform, cards):
        """剔除本轮已被**其它账号**试过的卡；全被试过时原样返回。

        为什么要剔除：_eligible_cards 是每个账号进入时的一次性快照，并行 worker 从同一
        有序列表头部出发，不去重就会让同一张卡在一轮里被两个账号各刷一次（2026-08-03
        实测 5 张）。第二次注定失败——第一次已被拒并标 invalid，只是后者的快照更早——
        纯属白烧一次拒付并给账号叠加风控 velocity。

        为什么留兜底：卡池偏紧时若硬性排除，后来的账号会一张卡都选不到，而
        registration 把「无可用支付卡」当作卡池耗尽、编排层据此永久放弃该账号——
        把**暂时的争用**误判成**永久的耗尽**。这个坑早先踩过（见
        tests/test_registry.py::test_release_lets_a_waiting_worker_proceed），
        宁可偶尔重复一张，也不能让账号被误弃。
        """
        used = self.payment_registry.used_numbers(platform)
        if not used:
            return cards
        used_keys = {self._card_key(n) for n in used}
        unused = [c for c in cards if self._card_key(c.get('number')) not in used_keys]
        if unused:
            return unused
        self.add_log("[选卡] 本轮可选卡已全部被其它账号试过，放开重复限制以免账号被误弃")
        return cards

    def _eligible_cards(self, group_id, exclude_used=True, platform=None):
        """返回该分组在指定平台当前「可选」的卡，有序：好卡优先，新卡垫后。

        platform 省略时用 self.platform（当前流水线/界面选中的平台）。整条判定链——
        可用状态、冷却、新卡还是好卡、本轮是否被试过——全部按这个平台算，所以同一张卡
        在别的平台的遭遇不会影响这里的结果。

        可选 = get_usable_cards_as_list（已排除 expired/invalid/bound）且不处于临时冷却
        （3DS / 充值失败冷却）中。排序：
          - 已成功付款过的好卡优先（paid 卡可反复支付），能过款的卡就接着用；
          - 之后才轮到新卡（从未成功付款过）。
        这个次序是**反过来**的：早先是新卡优先，想的是先把卡池消化掉。实际跑下来那等于
        每笔都拿一张没验证过的卡赌运气——拒付率高、还给账号叠 velocity 风控，而少数能过款的
        好卡反倒被晾在队尾。好卡优先之后，新卡只在好卡全部进了冷却或判废时才被动用。
        成功卡不被永久消耗、也**不进冷却**（否则同一账号连充第二笔就无卡可用）。
        一张卡退出可选集只有三种方式：被拒后进冷却（默认 24h，到期自动回来）、
        连续被拒达阈值判无效（默认 3 次，永久）、或过期。

        exclude_used=True 时再剔除本轮已被其它账号试过的卡（见 _exclude_used_this_run）。
        统计用途（启动前算「分组有多少可选卡」）应传 False，否则数字会随本轮进度缩水。"""
        models = self.models
        platform = platform or self.platform
        usable, _ = models['card_pool'].get_usable_cards_as_list(platform, group_id)
        cooldown_map = {}
        try:
            cooldown_map = models['card_state'].get_state_map(platform)
        except Exception:
            cooldown_map = {}
        try:
            success_nums = models['recharge_log'].all_success_card_numbers(platform)
        except Exception:
            success_nums = set()
        fresh, good = [], []
        for c in usable:
            num = c.get('number', '')
            if cooldown_map.get(num, {}).get('in_cooldown'):
                continue
            # success_nums 已去空格（all_success_card_numbers 内 replace），此处比对键同样
            # 去空格，保证「新卡/好卡」分类与记账口径一致（卡号含内部空格时也不误判）。
            (good if num.replace(' ', '') in success_nums else fresh).append(c)
        cards = good + fresh
        return self._exclude_used_this_run(platform, cards) if exclude_used else cards

    def _recharge_one_account(self, email, login_password, payment_group_id=None,
                              worker=None, captcha_api_key=None,
                              captcha_server="api.multibot.cloud", proxy=None,
                              verify_link=None, recharge_cfg=None):
        """为单个账号执行一次充值访问，返回 (result, err)，
        result ∈ {"success", "failed", "archived"(余额≥平台阈值已归档、未扣款)}。

        一次访问内账号可能连充**多笔**（见 registration.recharge_account 的连充循环），
        "success" 表示至少成功一笔。

        recharge_cfg: RechargeConfig（金额区间 / 余额上限 / 判废阈值 / 冷却时长）。
                      None 时由 recharge_account 回落到 cfg.recharge。

        captcha_api_key/captcha_server 透传给 registration.recharge_account 用于自动解 hCaptcha。

        用 payment_group_id 指定分组的可选卡（_eligible_cards：好卡优先，新卡垫后；已排除
        无效/过期/冷却）逐张尝试，付成一张即 success；付成之后会**继续用同一张卡**充下一笔
        （见 recharge_account 的粘卡循环），只有它失败了才换下一张。逐卡的卡状态标记（paid/invalid/冷却）
        与 recharge_logs 记账都在 registration.recharge_account 内部完成，本方法只负责取卡、
        调度、把结果转成计数用的 (result, err)。

        只负责一个账号的一次操作，不管理 is_running / 截图收尾（由调用方负责）。
        InterruptedError 向上抛出，供轮转循环感知停止。

        worker: 执行本次操作的 WorkerState；为 None 时用主 worker（串行路径）。"""
        import src.platforms as platforms

        models = self.models
        worker = worker or self.primary_worker
        monitor = worker.make_monitor(self)

        payment_cards = self._eligible_cards(payment_group_id) if payment_group_id else []
        if not payment_cards:
            self.set_action(worker, f"{email} 无可选卡（全部无效/过期或冷却中）")
            return "failed", "无可选卡"

        try:
            success, err, responses, card_last4, outcome = registration.recharge_account(
                email, login_password,
                recharge_log_model=models['recharge_log'],
                monitor_callback=monitor,
                payment_cards=payment_cards,
                valid_card_model=models['valid_card'],
                card_pool_model=models['card_pool'],
                account_model=models['account'],
                should_stop=lambda: self.stop_requested,
                card_state_model=models['card_state'],
                payment_registry=self.payment_registry,
                captcha_api_key=captcha_api_key,
                captcha_server=captcha_server,
                proxy=proxy,
                browser_factory=self.browser_factory(),
                verify_link=verify_link,
                platform=self.platform,
                platform_account_model=models['platform_account'],
                recharge_cfg=recharge_cfg,
            )

            if outcome == "topup":
                # 一次访问可能充了多笔。金额是每笔随机的，所以从 responses 现算
                # ——写死 "$20" 的旧文案在金额随机化后只会误导人。
                paid = [r for r in responses if r.get('ok')]
                total = sum(r.get('amount') or 0 for r in paid)
                self.set_action(worker, f"{email} 充值成功 {len(paid)} 笔（卡 {card_last4}）")
                self.add_log(f"{email} 充值成功 {len(paid)} 笔、合计 ${total:.0f}"
                             f"（末张卡 {card_last4}）：{err}")
                return "success", ''

            if outcome == "archived":
                # 余额≥阈值已归档（未扣款）：既非成功也非失败，该账号退出后续轮转
                skip_at = platforms.get(self.platform).recharge_skip_balance
                self.set_action(worker, f"{email} 余额≥${skip_at:.0f}，已归档跳过")
                self.add_log(f"{email} 余额≥${skip_at:.0f}，已归档跳过充值（{err}）")
                return "archived", err or 'archived'

            if outcome == "flagged":
                # GitHub 被 flag 无法授权 OAuth：身份层终态（registration 已标
                # identity_status='flagged'，对所有平台生效），同 archived 语义退出轮转
                self.set_action(worker, f"{email} GitHub 被 flagged，退出轮转")
                self.add_log(f"{email} GitHub 账号被 flagged，已标记并退出每日轮转（{err}）")
                return "flagged", err or 'flagged'

            # outcome == "failed"：逐卡的失效标记与记账已在 registration 内完成
            self.set_action(worker, f"{email} 本次未付成: {err}")
            self.add_log(f"{email} 本次未付成: {err}")
            return "failed", err or 'recharge failed'
        except InterruptedError:
            raise
        except Exception as e:
            self.set_action(worker, f"充值异常: {e}")
            self.add_log(f"充值异常: {e}")
            return "failed", str(e)

    def run_daily_pipeline(self, platform, group_id, login_password=None, captcha_api_key=None,
                           captcha_server="api.multibot.cloud", recharge_cfg=None):
        """每日充值任务：卡池驱动的账号轮转充值，串行跑在单个后台线程。

        platform 是目标平台 slug。账号状态、卡的占用与冷却全部按它隔离——同一邮箱、
        同一张卡在别的平台上的记录不影响本次运行。一次只能跑一个平台（AppState 单例）。

        选定一个卡池分组，用账号列表逐账号轮转充值：一个账号在其会话内**连续充多笔**，
        直到试卡上限 / 余额上限 / 风控拦截 / 无卡可用才轮转到下一个账号（连充循环在
        registration.recharge_account 内部，见那里的 docstring）。
        **以刷完卡池为第一标准**：只要分组还有可选卡且还有账号可用就继续跑；
        充值失败的账号只跳过**本轮**，一轮轮完（所有账号都试过一遍）后清空失败名单、
        回到头部开下一轮重试（A 失败→换 B→…→下一轮再试 A）。停止条件（满足其一）：
          1. 分组可选卡耗尽（全部无效/过期或冷却中）——**主收敛路径**，失败卡逐张进
             冷却/判无效，可选集有限且单调消耗，全失败也终会走到这里；
          2. 无账号可用：payable + imported 都领不到；或连续 IDLE_ROUNDS_LIMIT 整轮
             **完全空转**（没付成一张卡、可选卡也一张没动——流程根本没走到试卡环节，
             再轮转只会原样重复且永远到不了条件 1；容忍一轮是为了吸收瞬时抖动）。
             注意「失败但烧了卡」不算空转：那是进展，会继续开新一轮；
          3. 用户手动停止。

        选卡资格（见 _eligible_cards）：付款成功过的好卡优先（可反复复用），新卡垫后；卡退出可选集
        的方式有三种——被拒后进冷却（默认 24h，成功的卡不冷却）、连续被拒达阈值判无效、
        或过期。逐卡的卡状态标记与 recharge_logs 记账在 recharge_account 内部完成；
        本方法只负责取可选卡、轮转账号、计数与收尾。

        captcha_api_key/captcha_server 透传给充值流程用于自动解 hCaptcha（server 默认 Multibot）。
        recharge_cfg 透传充值策略（金额区间 / 余额上限 / 判废阈值 / 冷却时长），None 时
        由下游回落到 cfg.recharge。
        账号选取排除身份层终态（banned/suspended/rejected/flagged）与本平台的平台层终态
        （archived/recharged/subscribed，见 utils 里的两组终态常量）；登录后实时余额 ≥
        该平台 recharge_skip_balance 的账号会在本平台被归档并退出后续轮转；GitHub 被 flag
        无法授权 OAuth 的账号会被标身份层 flagged——那是 GitHub 侧的封禁，对所有平台一致。

        账号耗尽自动补号：当某轮无可充值账号时，从 imported 账号（hotmail.xlsx 有收码数据）
        取一个自动注册（复用 _register_one_account，全自动碰 Arkose 跳过），注册成功者
        转 registered 后于下一轮进入「登录→充值」；所有 imported 处理完仍无可充账号则结束。

        login_password 可选，用于覆盖账号自身密码（一般留空，用各账号 accounts.login_password）。
        并发度固定为 1（串行）：WorkerPool 的 is_serial 分支走同线程，保留截图/停止集成。"""
        self.is_running = True
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "每日充值任务启动中"
        self.update_frame(None)

        self._patch_prints()

        self.platform = platform
        account_model = self.models['account']
        platform_account_model = self.models['platform_account']
        recharge_log_model = self.models['recharge_log']

        # 本次运行的起始时刻，_reusable_recharged 用它界定「本次运行已充金额」。
        # 格式必须与 recharge_logs.created_at 的 datetime('now','localtime') 一致
        # （都是本地时间的 YYYY-MM-DD HH:MM:SS），才能直接做字符串比较。
        run_started_at = datetime.now().strftime('%Y-%m-%d %H:%M:%S')

        # 结果计数（收尾摘要用）。self.success_count/fail_count 另供前端进度条。
        stats = {'paid': 0, 'fail': 0, 'archived': 0, 'flagged': 0, 'registered': 0}
        # 本次运行**永久终结**（archived/flagged/注册未成，状态已离开可充值集）的 email。
        # 注意：充值失败不入 done——失败账号只禁本轮（failed_this_round），下一轮重试。
        done = set()
        # 本轮已失败、暂不重领的 email。整轮轮完（无人在飞且领不到新账号）后清空重来。
        failed_this_round = set()
        produce_lock = threading.Lock()   # 「找账号 + claim」原子领取
        state_lock = threading.Lock()     # 护 done + stats 的读写

        # 并发度读 config（clamp 1-4）。max_workers=1 仍走 WorkerPool 同线程分支（应急串行）。
        # 并发度按平台取：不同平台单账号耗时差很多，慢的那个多开几个才不至于
        # 拖住整体。未单独配置的平台回落到 concurrency.max_workers。
        pool = WorkerPool(self, cfg.concurrency.workers_for(self.platform))

        try:
            self._hooked_print(f"\n{'#' * 50}")
            self._hooked_print("每日充值任务开始（卡池驱动 + 账号轮转）")
            self._hooked_print(f"{'#' * 50}")

            # 可充值账号（实时快照）：有登录密码，且身份层与本平台状态都非终态。
            # 平台层排除 recharged/archived——本平台已充值过的账号不再进入轮转
            # （避免重复充值/空开浏览器），但这不影响它在别的平台被选中。
            # 排除 done——本次运行已终结的账号。
            def _payable_now():
                # 两次查询的**先后有讲究**：平台状态是排除依据，必须后读、取最新一份。
                # 反过来先读平台状态再读账号列表的话，worker 在两次查询之间刚写完
                # 'recharged'，这里就会拿着过期的平台状态把它当成可充值账号再发一次
                # ——同一账号被充两次。判据读得越晚，这个窗口越窄。
                accounts = account_model.get_all(order_desc=False)
                platform_status = platform_account_model.map_by_email(platform)
                return [
                    a for a in accounts
                    if (login_password or a.get('login_password'))
                    and not is_identity_terminal(a.get('identity_status'))
                    and not is_platform_terminal(
                        (platform_status.get(a['email']) or {}).get('status'))
                    and a['email'] not in done
                ]

            # 待注册 imported 账号（有收码数据：DB 自带 link 或 xlsx 命中；未在 done）。
            # 'imported' 是身份层状态——GitHub 都还没注册，与平台无关。
            def _registerable_imported():
                return [
                    a for a in account_model.get_all(order_desc=False)
                    if (a.get('identity_status') or '') == 'imported'
                    and a['email'] not in done
                    and self._hotmail_for_account(a)
                ]

            # ── 余额未满的已充值账号 ──
            # 与可充值账号**同一档**一起领（见 _try_claim），只有两类都领不到才去注册。
            # 曾经它排在待注册 imported 之后，于是几十个 imported 会把老账号饿死——
            # worker 一直忙着注册，余额只有 $20 的号几乎永远轮不到。而注册耗时且容易被
            # GitHub flag，现成账号优先反而更稳。
            #
            # 它同时是 AdsPower 环境紧张时的泄压阀：这些账号**已经有环境了**，复用
            # 一个新环境都不用建。而环境上限只有 12、几十个新账号在排队抢，抢不到就
            # 白跑一轮——此时改去充一个已有环境的账号，是纯赚。
            #
            # 判据是「余额还没到 balance_cap」而不是「状态非 recharged」：recharged 现在
            # 的含义是「有一些余额」，不再是「这个账号做完了」。真正做完的是 archived
            # （余额已达上限），它仍然是终态。
            #
            # ⚠️ 有效余额 = DB 余额 + **本次运行已充金额**，两项缺一不可。
            # 只看 DB 余额会死循环：credits_balance 可能是 NULL（balance_after 读不到时
            # update_balance 直接 return，infron 常态、opencode 偶发），也可能停在旧值
            # 不再更新。那样的账号永远满足「未达上限」，被一轮轮反复领走反复充，钱全堆
            # 到一个号上且任务不收敛。
            # 这里曾用「每次运行只复用一次」挡它，代价是余额 $20、上限 $200 的账号一轮
            # 只能充一笔，钱铺不开。现在换成金额累加：每成功一笔就推进至少 amount_min，
            # 最多 ceil(cap / amount_min) 次必然越过 cap 而出局——收敛由金额自己保证，
            # 不再需要次数闸。
            # 失败路径（充不成 → 金额不增长）不归这里管：failed_this_round 让它本轮出局，
            # 跨轮由轮边界的「连续完全空转」兜底，与可充值账号一视同仁。
            # ── 本次运行给每个账号充值的**内存估算**（email -> 累计金额）──
            # 领取即按 amount_min（每笔下界）计入，充值失败再回退。它是 recharge_logs
            # 之外的第二本账，两件事都靠它：
            #
            # 1. 补时间差窗口。判据全部从 DB 实时派生，而账号在 _do 里跑完、日志写下
            #    之前就可能被下一次 _try_claim 判成「未达上限」而再领一次，越过
            #    balance_cap。实测 3 worker 下约 4/10 的运行会撞上（cap $60 的账号充了
            #    4 笔 $20）。同构于 PaymentCardRegistry 的 in-flight 登记，双闸门。
            #
            # 2. **收敛不依赖记账可靠**。只看 recharge_logs 的话，一旦日志没写成、
            #    金额记成 0、或平台/时间过滤没命中，累计额恒为 0，账号永远「未达上限」，
            #    被无限重领——不是断言失败，是整个进程转到死。这条内存账单调递增，
            #    哪怕 DB 一笔都没记下，它也会把账号推到 cap 然后放手。
            #
            # 与 DB 金额取 max 而不是相加：两者记的是同一批钱，相加会双重计算让账号
            # 提前出局；DB 更准（会话内连充多笔时 amount_min 只是下界），内存账更可靠。
            run_topup = {}

            def _refund_topup(email):
                """退回领取时记的那一笔。**调用方须持有 state_lock**（锁不可重入）。"""
                left = run_topup.get(email, 0) - (recharge_cfg or cfg.recharge).amount_min
                if left > 0:
                    run_topup[email] = left
                else:
                    run_topup.pop(email, None)

            def _reusable_recharged():
                cap = (recharge_cfg or cfg.recharge).balance_cap
                # 内存账先读、DB 账后读。内存账在领取那一刻就记上（早于任何 DB 写入），
                # 先读它就不会漏掉正在飞的那笔；DB 账后读则取到最新一份。
                with state_lock:
                    estimated = dict(run_topup)
                topped = recharge_log_model.success_amount_by_email(platform, run_started_at)
                accounts = account_model.get_all(order_desc=False)
                platform_status = platform_account_model.map_by_email(platform)
                out = []
                for a in accounts:
                    if not (login_password or a.get('login_password')):
                        continue
                    if is_identity_terminal(a.get('identity_status')):
                        continue
                    if a['email'] in done:
                        continue
                    row = platform_status.get(a['email']) or {}
                    if (row.get('status') or '') != 'recharged':
                        continue
                    effective = (row.get('credits_balance') or 0) + max(
                        topped.get(a['email'], 0), estimated.get(a['email'], 0))
                    if effective >= cap:
                        continue
                    out.append(a)
                return out

            # 代理领取:每账号处理时领一个空闲代理出口 IP(反关联),用完释放。
            # 无代理配置时返回 (None, None) → 直连(行为同现状)。
            proxy_model = self.models['proxy']

            def _acquire_proxy_for(account_id):
                """领一个空闲代理(排他)。全忙(100≫worker 数,几乎不触发)则按
                account_id 取模兜底(不排他,满足「循环复用」)。返回 (pw_dict, proxy_key);
                proxy_key 为 None 表示取模兜底或无代理,_do 不 release。

                AdsPower 模式下代理由环境自带（建环境时用 proxyid 绑定，一绑长期有效），
                本地代理池完全不参与——两边都发代理会让浏览器走双层代理。"""
                if self.adspower_enabled:
                    return None, None
                usable = proxy_model.get_usable_list()
                if not usable:
                    return None, None
                worker = get_current_worker() or self.primary_worker
                # owner 必须传：worker_id 只是 'W1'..'W4'，每个平台各有一套同名的，
                # 不带 owner 会让两个平台的 W1 互相认成自己，同一个出口 IP 被同时发给
                # 两边——反关联失效，且不报错、不留日志。
                p = self.proxy_registry.acquire_free(usable, worker.worker_id, owner=self.platform)
                if p is not None:
                    return _to_pw_proxy(p), self.proxy_registry.key_of(p)
                p = usable[(account_id or 0) % len(usable)]   # 取模兜底,不排他
                return _to_pw_proxy(p), None

            accounts = _payable_now()
            imported_pending = len(_registerable_imported())
            reusable_pending = len(_reusable_recharged())
            eligible = len(self._eligible_cards(group_id, exclude_used=False))
            if self.adspower_enabled:
                proxy_note = "浏览器走 AdsPower 指纹环境，代理由环境绑定"
            else:
                proxy_count = proxy_model.count()
                proxy_note = (f"代理 {proxy_count} 个"
                              f"{'（未配置代理，直连）' if not proxy_count else ''}")
            self._hooked_print(
                f"可充值账号 {len(accounts)} 个，待注册 imported {imported_pending} 个，"
                f"可复用已充值账号 {reusable_pending} 个，"
                f"分组可选卡 {eligible} 张，{proxy_note}")
            if not eligible:
                self._hooked_print("分组内无可选卡（全部无效/过期或冷却中），无事可做")
                return
            # 回退池也要算进启动门。漏了它的话，「新号全跑完、只剩老号可加码」——正是
            # 复用功能存在的那个场景——任务会直接拒绝启动，功能等于没接。
            if not accounts and not imported_pending and not reusable_pending:
                self._hooked_print("无可充值账号、无 imported 可注册、也无余额未满的已充值账号，任务结束")
                return

            # ── 生产者：每个 worker 反复原子领一个账号，按「轮」轮转。优先充值现有可充
            #    账号（跳过本轮已失败的），无则领一个待注册 imported。都领不到时分三种情况：
            #      wait —— 还有账号在其它 worker 手上，本轮胜负未分，睡 5s 再看（不能直接
            #              退出：在飞账号失败后会回到轮转池，退出会白白减员）；
            #      开新一轮 —— 无人在飞且本轮有失败账号、且上一轮有进展（付成过或可选卡
            #              集合变过），清空失败名单从头重试，并清掉「本轮已试过的卡」标记；
            #      done —— 卡池耗尽（主路径）/ 无账号可用 / 整轮完全空转（一张卡都没动，
            #              再轮只会原样重复），收敛。
            #    produce_lock 让「找账号 + claim + 轮转判定」原子，两 worker 绝不领同一个。
            #    领到即用 account_registry 占坑，消费者 finally 释放。返回 None 表示任务收敛。
            round_state = {
                'no': 1,
                'paid_at_start': 0,
                'cards_at_start': None,   # 本轮开始时的可选卡键集合，判「有没有动卡」用
                'idle_rounds': 0,         # 连续完全空转轮数；≥2 才停（容忍一轮瞬时抖动）
            }
            end_logged = [False]          # 收敛原因只打一次（多 worker 会各拿到一次 done）
            reuse_logged = [False]        # 「开始复用老账号」也只打一次

            def _card_keys_now():
                return frozenset(
                    self._card_key(c.get('number'))
                    for c in self._eligible_cards(group_id, exclude_used=False))

            round_state['cards_at_start'] = _card_keys_now()

            def _try_claim():
                """一次领取尝试（须在 produce_lock 内调用）。
                返回 ('item', 工作项) / ('wait', None) / ('retry', None 已开新一轮) /
                ('done', 收敛原因)。"""
                if not self._eligible_cards(group_id, exclude_used=False):
                    return 'done', "分组可选卡已耗尽（全部无效/过期或冷却中）"
                # ── 第一档：现成账号，新的和余额未满的老的**一起领** ──
                # 曾经老账号排在待注册 imported 之后，理由是「优先把钱铺开到更多账号上」。
                # 实践中这条让老账号饿死：库里几十个 imported 时，worker 一直在注册，
                # 余额 $20 的老号几乎永远轮不到。而注册耗时、还容易被 GitHub flag，
                # 现成账号优先反而更稳。
                # 两个列表不会有交集：_payable_now 排除平台终态，而 recharged 正是终态之一。
                reusable = _reusable_recharged()
                if reusable and not reuse_logged[0]:
                    reuse_logged[0] = True
                    self._hooked_print(
                        f"余额未满的已充值账号 {len(reusable)} 个一并进入轮转"
                        f"（上限 ${(recharge_cfg or cfg.recharge).balance_cap:.0f}）")
                for a in _payable_now() + reusable:
                    if a['email'] in failed_this_round:
                        continue
                    if self.account_registry.claim(a['email'], owner=platform):
                        proxy, pkey = _acquire_proxy_for(a.get('id', 0))
                        # 记一笔内存账。**两条路径都要记**，不止复用池：新账号充完
                        # 第一笔就变 recharged，若此时日志尚未落库，它会以累计额 0 被
                        # 当成「余额几乎为零」再领一次——同样超充。
                        # 领到就记、不等结果：等成功再记的话，正在飞的那笔挡不住任何人。
                        # 失败由 _do 回退（那次没花钱，不该占额度）。
                        with state_lock:
                            run_topup[a['email']] = (run_topup.get(a['email'], 0)
                                                     + (recharge_cfg or cfg.recharge).amount_min)
                        return 'item', ('recharge', a, proxy, pkey)
                # ── 第二档：现成账号都领不到了，才去注册 ──
                for a in _registerable_imported():
                    if self.account_registry.claim(a['email'], owner=platform):
                        proxy, pkey = _acquire_proxy_for(a.get('id', 0))
                        return 'item', ('register', a, proxy, pkey)
                # 「本轮还有账号在飞吗」——**必须只看本平台**。registry 是跨平台共享的，
                # 不过滤的话本平台会把另一个平台正在跑的账号当成自己这轮在飞，于是永远
                # 走 'wait'、轮边界永不触发、失败账号永不重试、idle_rounds 永不递增——
                # 任务就这么静默地不收敛了，没有任何报错。
                if self.account_registry.snapshot(owner=platform):
                    return 'wait', None
                if not failed_this_round:
                    return 'done', "无可充值账号、无 imported 可注册、也无余额未满的已充值账号可复用"
                # ── 轮转边界：所有账号都试过一遍且无人在飞。有进展就开下一轮重试失败账号
                with state_lock:
                    paid_now = stats['paid']
                cards_now = _card_keys_now()
                # 「卡集合变了」= 有进展，**增减都算**。烧卡算进展是刻意的：任务的
                # 第一标准是刷完卡池，只要每轮还在消耗卡，就该继续轮转下去，剩多少张
                # 不该由轮数来裁决。终止性由卡集合「有限且单调消耗」保证——烧到耗尽时
                # 由上面的「分组可选卡已耗尽」分支收敛。
                #
                # 这一条 2026-08-05 曾被改成只认新增（`gained = cards_now - 起始集合`），
                # 为的是压掉「2 个账号反复失败、卡被逐张烧掉、跑到第 113 轮还在打转」
                # 的现场。代价是另一头翻车：08-06 那次运行成功付款 0 次，两轮后即收敛，
                # **卡池里还剩 2596 张没试**。用户要的是刷完卡池，故改回增减都算。
                # 113 轮那种场景现在是预期行为，不再是 bug。
                changed = cards_now != round_state['cards_at_start']
                progressed = paid_now > round_state['paid_at_start'] or changed
                if progressed:
                    round_state['idle_rounds'] = 0
                else:
                    # 完全空转：既没付成、卡也一张没动。说明流程根本没走到试卡环节
                    # （登录挂了 / 环境起不来 / hCaptcha 超时），再轮转只是原样重复，
                    # 而且永远到不了「卡耗尽」那个收敛点——这是唯一还会提前停的情况。
                    round_state['idle_rounds'] += 1
                    if round_state['idle_rounds'] >= IDLE_ROUNDS_LIMIT:
                        return 'done', (f"连续 {round_state['idle_rounds']} 轮完全空转"
                                        "（未付成一张卡，可选卡也一张没动），"
                                        "流程未走到试卡环节，再轮只会原样重复")
                retrying = len(failed_this_round)
                round_state['no'] += 1
                round_state['paid_at_start'] = paid_now
                round_state['cards_at_start'] = cards_now
                with state_lock:
                    failed_this_round.clear()
                # 「本轮已被试过的卡」标记随轮清零。只清本平台的归属，in-flight 不动
                # ——那是全局的发卡行 velocity 防护，不该被轮边界打断。
                self.payment_registry.release_all(platform)
                zero_note = ("，上轮完全空转（容忍一轮，可能是瞬时抖动）"
                             if round_state['idle_rounds'] else "")
                self._hooked_print(
                    f"\n一轮轮转完毕仍有可选卡 {len(cards_now)} 张，"
                    f"开始第 {round_state['no']} 轮（重试 {retrying} 个上轮失败账号{zero_note}）")
                return 'retry', None

            def _produce():
                while True:
                    if self.stop_requested:
                        return None
                    with produce_lock:
                        verdict, payload = _try_claim()
                        if verdict == 'done' and not end_logged[0]:
                            end_logged[0] = True
                            self._hooked_print(f"\n{payload}，任务收敛")
                    if verdict == 'item':
                        return payload
                    if verdict == 'done':
                        return None
                    if verdict == 'wait':
                        time.sleep(5)
                    # 'retry'：已开新一轮，立刻回头再领

            # ── 消费者：跑在 worker 线程。注册成功者「不入 done」——释放后会被（自己或另一个
            #    worker）当作 payable 领来充值，实现「注册→登录→充值」闭环且可跨 worker 并行；
            #    充值成功记 success；归档/flagged/注册未成入 done（终态，永久退出）；
            #    充值失败只入 failed_this_round（本轮跳过，下一轮重试）——防无限重领的职责
            #    移交给 _try_claim 的轮边界判定，卡池刷完前失败账号可循环使用。
            def _do(worker, item):
                kind, acct, proxy, pkey = item
                email = acct['email']
                # 这次是否真的花了钱。失败/异常/配额拿不到都算没花，finally 统一退回
                # 领取时记的那笔内存账——否则反复失败的账号会被自己的估算额推到 cap
                # 而永久退出轮转，而它其实一分钱都没充上。
                charged = False
                if self.adspower_enabled:
                    proxy_note = "（AdsPower 环境，代理随环境）"
                else:
                    proxy_note = (f"（代理 {proxy['server'].split('//')[-1]}）"
                                  if proxy else "（直连）")
                try:
                    if kind == 'register':
                        self.set_action(worker, f"账号耗尽，注册补号 {email}")
                        self._hooked_print(f"\n可充账号耗尽，注册补号: {email} {proxy_note}")
                        rr, rdetail = self._register_one_account(acct, worker, proxy=proxy)
                        self._hooked_print(f"补号 {email}: {rr}（{rdetail}）")
                        with state_lock:
                            if rr == "registered":
                                stats['registered'] += 1
                            else:
                                done.add(email)   # pending/suspended/failed，已离开 imported
                    else:  # recharge
                        self.set_action(worker, f"充值账号 {email}")
                        self._hooked_print(f"\n充值账号: {email} {proxy_note}")
                        result, err = self._recharge_one_account(
                            email, login_password or acct.get('login_password'),
                            payment_group_id=group_id, worker=worker,
                            captcha_api_key=captcha_api_key, captcha_server=captcha_server,
                            proxy=proxy,
                            # GitHub 新设备验证自动收码要用；指纹浏览器下每个账号都是
                            # 全新环境，这一关几乎必然触发。
                            verify_link=acct.get('email_verify_link'),
                            recharge_cfg=recharge_cfg)
                        with state_lock:
                            if result == "success":
                                stats['paid'] += 1
                                self.success_count += 1
                                charged = True
                            elif result == "archived":
                                stats['archived'] += 1
                                done.add(email)
                            elif result == "flagged":
                                stats['flagged'] += 1
                                done.add(email)
                            else:
                                stats['fail'] += 1
                                self.fail_count += 1
                                failed_this_round.add(email)   # 只禁本轮，下一轮重试
                except InterruptedError:
                    self._hooked_print("充值阶段被中断")
                    raise
                except AdsPowerError as e:
                    # 浏览器起不来（配额等不到 / 客户端没开 / Key 无效）。这不是账号的
                    # 问题，所以**不动账号状态**——标 failed 会让 imported 账号退出补号池
                    # 永不重试。
                    #
                    # 曾经这里是「置全局 stop_requested 整批收敛」，理由是「配额是全局的，
                    # 下一个账号必然撞同一堵墙」。多平台并发后这条不成立了：配额由仲裁器
                    # 管，拿不到会先**等**（quota_wait_seconds），能走到这里说明是等超时或
                    # 客户端真的不可用。而按平台拆分后置 stop 只会停自己，配额却是共用的
                    # ——结果是 A 平台饿死在等待、B 平台反复抛错自杀。
                    #
                    # 改为：只跳过本账号，并向借用方发一个归还请求。下一个账号还会重试，
                    # 真的持续不可用时由「整轮完全空转」兜底收敛。
                    #
                    # 进 failed_this_round 而**不是** done。注释一直写着「本轮不再重领」，
                    # 代码用的却是 done——那是**整次运行**的永久集合，从不清空。于是每一次
                    # 抢不到环境就永久损失一个账号：AdsPower 环境上限只有 12，几十个账号
                    # 排队抢，一轮下来大半账号被这条路径吃掉，并发度肉眼可见地塌下去，
                    # 而日志上只有一句「跳过」，看不出账号再也不回来了。
                    # 配额是会随别的账号跑完而释放的，下一轮重试完全合理。
                    with state_lock:
                        failed_this_round.add(email)   # 本轮跳过，下一轮重试；账号状态不动
                    asked = self.quota.request_recall(self.platform)
                    note = f"；已向借用方请求归还 {asked} 个额度" if asked else ""
                    self._hooked_print(f"AdsPower 环境暂不可用，跳过 {email}: {e}{note}")
                except Exception as e:
                    with state_lock:
                        stats['fail'] += 1
                        self.fail_count += 1
                        # 异常（浏览器崩溃等）多为瞬时问题，同充值失败：只禁本轮
                        failed_this_round.add(email)
                    self._hooked_print(f"处理 {email} 出错: {e}")
                finally:
                    if kind != 'register' and not charged:
                        with state_lock:
                            _refund_topup(email)
                    self.account_registry.release(email)
                    if pkey:
                        self.proxy_registry.release(pkey)   # 排他领取的代理释放回池

            # 收敛：卡被标 invalid/冷却后退出可选集，集合有限且单调消耗，所以「卡耗尽」
            # 一定会到达——这是失败路径的主收敛点，账号可无限跨轮循环。只有一张卡都没动
            # 的完全空转才由 _try_claim 提前判停。两条路都让 _produce 终返回 None。
            pool.run_until_empty(_produce, _do)

        except Exception as e:
            self._hooked_print(f"严重错误: {e}")
        finally:
            # 收尾所有 worker，释放运行时占用
            for w in self.workers.values():
                w.clear_active_driver()
                w.stop_screenshot_loop()
                w.current_action = "空闲"
                w.busy = False
            # ⚠️ 三个 registry 都是**跨平台共享**的，收尾只能释放自己那份。
            # 无参形式是全清——一个平台跑完就把另一个平台正在持有的账号、卡、代理
            # 全部放掉，它的排他保护瞬间蒸发：两个 worker 同用一个 Chrome profile
            # 互删 Singleton 锁、同一张卡被两边同时提交给发卡行、同一个出口 IP 被
            # 重复领取。三种后果都不报错。
            self.account_registry.release_all(owner=self.platform)
            self.payment_registry.release_all(self.platform, include_in_flight=True)
            self.proxy_registry.release_all(owner=self.platform)
            self._stop_started_adspower()
            self.parallel_mode = False
            self.is_running = False

            try:
                remaining = len(self._eligible_cards(group_id, exclude_used=False))
            except Exception:
                remaining = '?'
            self.current_action = (
                f"每日充值任务完成（成功付款 {stats['paid']} 次 / "
                f"未付成 {stats['fail']} 次 / 归档跳过 {stats['archived']} 个 / "
                f"flagged 退出 {stats['flagged']} 个 / 注册补号 {stats['registered']} 个 / "
                f"剩余可选卡 {remaining} 张）"
            )
            self._hooked_print(f"\n{'#' * 50}")
            self._hooked_print(self.current_action)
            self._hooked_print(f"{'#' * 50}")


    # ========= 每日订阅任务（Subscribe to Go）——additive，不改上面的充值路径 =========

    def _hotmail_by_email(self, email):
        """从 hotmail.xlsx 按 email 取 HotmailAccount（含 ruoanzhu link，注册收码所需）。

        惰性加载并缓存成 dict{email→HotmailAccount}。xlsx 缺失/无该邮箱时返回 None。
        """
        if getattr(self, '_hotmail_map', None) is None:
            self._hotmail_map = {}
            try:
                from src.services.hotmail_inbox import read_hotmail_accounts
                xlsx = os.path.join(get_base_dir(), 'hotmail.xlsx')
                for acc in read_hotmail_accounts(xlsx):
                    self._hotmail_map[acc.email] = acc
            except Exception as e:
                self._hooked_print(f"读取 hotmail.xlsx 失败: {str(e)[:120]}")
        return self._hotmail_map.get(email)

    def _hotmail_for_account(self, acct):
        """为账号取注册收码所需的 HotmailAccount（含 ruoanzhu 收信 link）。

        两个来源，优先账号自带：
          1. accounts 表自带 email_verify_link（导入时随邮箱一起入库的 ruoanzhu 链接）——
             多数 imported 账号走这条，收码数据在 DB，不在 hotmail.xlsx。
          2. 回退 hotmail.xlsx（_hotmail_by_email）——订阅任务那批只在 xlsx 的账号。
        两者都取不到（无 link 且 xlsx 无该邮箱）返回 None。
        """
        link = (acct.get('email_verify_link') or '').strip()
        if link:
            from src.services.hotmail_inbox import HotmailAccount
            return HotmailAccount(
                email=acct['email'],
                password=(acct.get('email_password') or ''),
                link=link,
                raw='',
            )
        return self._hotmail_by_email(acct['email'])

    # 单账号单次推进内最多试几张卡即换下一个账号（避免坏卡把一个账号卡死数小时；
    # 下一轮该账号会带新卡再来，坏卡已被标 invalid 退出可选集）。
    SUBSCRIBE_MAX_CARDS_PER_ACCOUNT = 5

    def _register_one_account(self, acct, worker=None, proxy=None):
        """未注册账号跑一次 GitHub 全自动注册（Patchright，碰 Arkose 自动跳过不等人工）。

        返回 (result, detail)，result ∈ {"registered","skipped","failed"}：
          registered — signup_complete：identity_status='registered' 并存 GitHub 密码
          skipped    — 无 hotmail 数据 / 碰 Arkose（identity_status='pending'）/ 注册即挂起（'suspended'）
          failed     — 其它注册未完成（identity_status='failed'）

        写的全是**身份层**状态：GitHub 注册结果对所有平台一致，与目标平台无关。

        供订阅任务（_subscribe_one_account）与充值任务（run_daily_pipeline 补号）共用，
        保证两条流水线注册行为一致。跑在 worker 线程；account 排他由外层 claim 保证。
        signup_one 内部用 create_driver(Patchright)，返回时其 session 已关。

        AdsPowerError（配额满 / 客户端没开）**向上抛出、不改账号状态**：浏览器都没起来，
        谈不上「这个账号注册失败」。若在此标 failed，账号会退出 imported 池永不重试——
        2026-08-03 一次配额耗尽就这样误废了 30 个刚导入的账号。
        """
        models = self.models
        worker = worker or self.primary_worker
        email = acct['email']

        hacc = self._hotmail_for_account(acct)
        if not hacc:
            self.set_action(worker, f"{email} 无收码数据，跳过")
            return "skipped", "无收码数据（账号无 email_verify_link 且 xlsx 缺该邮箱）"
        from src.services.github_signup_service import signup_one
        self.set_action(worker, f"{email} 未注册，尝试 GitHub 注册（Arkose 弹则跳过）")
        # auto_skip_captcha：不弹 Arkose 自动收码完成注册；弹了立即跳过不等人工（全自动）。
        r = signup_one(headless=False, semi_auto=False, account=hacc,
                       post_provision=None, auto_skip_captcha=True, proxy=proxy,
                       browser_factory=self.browser_factory())
        oc = r.get('outcome')
        if oc == 'reached_captcha':
            models['account'].update_identity_status(email, 'pending')
            return "skipped", "碰 Arkose，跳过（全自动模式不等人工）"
        if oc == 'account_suspended':
            models['account'].upsert(email, login_password=r.get('github_password'),
                                     email_password=hacc.password,
                                     identity_status='suspended',
                                     email_verify_link=hacc.link)
            return "skipped", "注册即挂起"
        if oc != 'signup_complete':
            models['account'].update_identity_status(email, 'failed')
            return "failed", f"注册失败: {oc}"
        models['account'].upsert(email, login_password=r.get('github_password'),
                                 email_password=hacc.password,
                                 identity_status='registered',
                                 email_verify_link=hacc.link)
        self.add_log(f"{email} GitHub 注册成功")
        return "registered", "GitHub 注册成功"

    def _subscribe_one_account(self, acct, payment_group_id, captcha_api_key, worker=None,
                               captcha_server="api.multibot.cloud", proxy=None):
        """单账号一次推进：未注册先注册（Patchright，Arkose 跳过），已注册走原生栈登录+逐卡订阅。

        返回 (result, detail)，result ∈ {"subscribed","registered_only","skipped","failed"}。
        逐卡消耗规则镜像 recharge：订阅成功→卡 paid、拒付→卡 invalid、error/unknown→不耗卡换下一张。
        单次最多试 SUBSCRIBE_MAX_CARDS_PER_ACCOUNT 张即换人（坏卡下不至于卡死单账号）。
        跑在 worker 线程；InterruptedError 向上抛供轮转感知停止。account 排他由外层 claim 保证。
        """
        from src.browser.driver import create_driver_vanilla, close_driver
        from src.services import captcha as captcha_solver
        import src.platforms as platforms
        from src.platforms.base import CAP_SUBSCRIBE, Credentials

        models = self.models
        worker = worker or self.primary_worker
        platform = self.platform
        adapter = platforms.get(platform)
        if CAP_SUBSCRIBE not in adapter.capabilities:
            return "skipped", f"{adapter.display_name} 不支持订阅"
        email = acct['email']
        identity_status = (acct.get('identity_status') or '')

        # --- A. 注册分支：GitHub 还没注册好就先注册（复用共享函数）---
        # 判据是身份层的 identity_status，不看平台状态——账号在别的平台已订阅，
        # 不代表它在本平台不用跑；反过来 GitHub 没注册好，哪个平台都跑不了。
        #
        # 顺带修掉一个旧缺陷：原判据是 status not in ('registered','subscribed')，
        # 于是充值管线标成 'recharged' 的账号（GitHub 明明注册好了）会被当成未注册，
        # 又跑一遍 GitHub 注册。状态分层后这类账号 identity_status 就是 'registered'。
        if identity_status != 'registered':
            rr, rdetail = self._register_one_account(acct, worker, proxy=proxy)
            if rr != "registered":
                return rr, rdetail
            self.add_log(f"{email} 转订阅")

        # --- B. 订阅分支：原生 Playwright 栈（hCaptcha token 注入只在原生栈生效）---
        if captcha_api_key:
            captcha_solver.init_solver(captcha_api_key, server=captcha_server)

        factory = self.browser_factory()
        session = (factory(email) if factory is not None
                   else create_driver_vanilla(profile_id=email, proxy=proxy))
        monitor = worker.make_monitor(self)
        worker.set_active_driver(session)
        try:
            # 导航前装 hCaptcha hook（原生栈下 add_init_script 真正生效）
            if captcha_solver.is_available():
                captcha_solver.install_hcaptcha_hook(session)

            sess = adapter.ensure_session(
                session,
                Credentials(email=email,
                            login_password=acct.get('login_password'),
                            verify_link=acct.get('email_verify_link')),
                monitor=monitor,
            )
            if not sess.ok:
                if sess.blocked_by_identity:
                    # 身份供给侧被封（GitHub 反滥用 flag，无法授权任何第三方 OAuth）
                    # → 标**身份层** flagged，对所有平台一致，不是本平台的状态。
                    models['account'].update_identity_status(email, 'flagged')
                    self.set_action(worker, f"{email} GitHub 被 flagged，跳过")
                    return "skipped", "GitHub 账号被 flagged，无法授权"
                self.set_action(worker, f"{email} 登录失败: {sess.detail[:60]}")
                return "failed", f"登录失败: {sess.detail}"
            wid = sess.tenant_id

            # 账号内逐卡试付，成功即止（快照迭代；拒付卡已被标 invalid 退出后续可选集）
            cards = self._eligible_cards(payment_group_id) if payment_group_id else []
            if not cards:
                return "registered_only", "无可选卡"
            # 单次卡数上限，避免坏卡卡死单账号。各平台自定——订阅拒付对账号的伤害
            # 比充值更大，所以这个值通常比 max_card_attempts 更保守。
            cards = cards[:getattr(adapter, 'max_subscribe_attempts',
                                   self.SUBSCRIBE_MAX_CARDS_PER_ACCOUNT)]
            for i, card in enumerate(cards, 1):
                if self.stop_requested:
                    raise InterruptedError("用户请求停止")
                num = card.get('number', '')
                last4 = str(num)[-4:]
                # 卡排他。此前订阅侧**整个跳过了这一步**，只有充值侧
                # （registration.recharge_account）在 acquire/release，于是：
                #   - 订阅任务的多个 worker 会从各自的 _eligible_cards 快照里挑中同一张卡
                #     并同时提交（快照是进入时一次性取的，挡不住这种同时选中）；
                #   - 订阅任务与充值任务本就能并发跑（RUN_CONTEXTS 按平台拆开就是为了这个），
                #     同一张卡会在几秒内被两个**商户号**分别请求授权。
                # 后者正是 AppSharedState 那段 docstring 点名要防的事：这是典型盗刷特征，
                # 直接触发发卡行风控，轻则拒付、重则锁卡。
                # try_acquire 里的 _in_flight 刻意不按平台隔离，就是为了拦住跨平台这一路。
                if not self.payment_registry.try_acquire(platform, num, email):
                    self.add_log(f"{email} 卡 ****{last4} 正被其它 worker 使用，跳过")
                    continue
                try:
                    self.set_action(worker, f"{email} 订阅试卡 ****{last4}")
                    self.add_log(f"{email} 订阅试卡 ****{last4}（第 {i}/{len(cards)} 张）")
                    log_id = models['recharge_log'].create(platform, email, num, amount=5)
                    pay = adapter.subscribe(session, wid, card, monitor=monitor,
                                            should_stop=lambda: self.stop_requested, dry=False)
                    res = vars(pay)
                    oc = pay.outcome
                    if oc == 'success':
                        models['card_pool'].mark_status_by_number(platform, num, 'paid')
                        try:
                            models['valid_card'].record(platform, card, source_type='payment',
                                                        source_email=email)
                        except Exception:
                            pass
                        models['platform_account'].update_status(platform, email, 'subscribed')
                        models['platform_account'].update_tenant_id(platform, email, wid)
                        models['recharge_log'].mark_success(log_id, api_response={"result": res})
                        self.add_log(f"{email} ✅ 订阅成功（卡 ****{last4}）")
                        return "subscribed", f"****{last4}"
                    elif oc == 'failed':
                    # 判废口径必须与 registration.recharge_account 逐字一致：无条件冷却
                    # + 连续失败计数 +1，达 max_fail_streak 才判无效。
                    #
                    # 两条流水线写的是**同一张** card_platform_state 表。这里若还按老口径
                    # 「从未成功过的卡首拒即 invalid」，一张卡在订阅侧被拒一次就永久出局，
                        # 充值侧那套「连续 3 次才判废」等于被静默绕过——用户配的阈值形同虚设，
                        # 而且从充值日志里完全看不出卡是被谁判死的。
                        # 直接取全局配置：max_fail_streak / fail_cooldown_hours 只在
                        # config.yaml 里配，不像金额区间那样按次从 UI 覆盖。
                        rc = cfg.recharge
                        models['card_state'].set_cooldown(
                            platform, num, hours=rc.fail_cooldown_hours,
                            reason='订阅支付失败，冷却')
                        streak = models['card_state'].bump_fail_streak(platform, num)
                        if streak >= rc.fail_threshold():
                            models['card_pool'].mark_invalid_by_number(platform, num)
                            note = f'连续失败 {streak} 次，标 invalid'
                        else:
                            note = (f'连续失败 {streak}/{rc.fail_threshold()} 次，'
                                    f'冷却 {rc.fail_cooldown_hours}h')
                        models['recharge_log'].mark_failed(log_id, error=res.get('err', ''),
                                                           api_response={"result": res})
                        self.add_log(f"{email} 卡 ****{last4} 拒付，{note}，换下一张")
                    elif oc == 'needs_captcha':
                        # hCaptcha 未过：非卡问题，不标卡无效，换下一张（换次提交 token 可能就过）
                        models['recharge_log'].mark_failed(log_id, error='hCaptcha 未过',
                                                           api_response={"result": res})
                        self.add_log(f"{email} 卡 ****{last4} hCaptcha 未过，换下一张")
                    else:  # error / unknown：不耗卡，换下一张
                        models['recharge_log'].mark_failed(
                            log_id, error=res.get('err', '') or oc,
                            api_response={"result": res})
                        self.add_log(f"{email} 卡 ****{last4} 未定案({oc}): "
                                     f"{(res.get('err') or '')[:90]}，换下一张")
                finally:
                    # 与充值侧同构：continue / return / 异常三条路径都要放开这张卡。
                    # 漏掉的话它在本进程内永久 in-flight，谁也再选不中，且没有任何报错。
                    self.payment_registry.release(num)
            return "registered_only", "账号内可选卡试尽未成功"
        except InterruptedError:
            raise
        except Exception as e:
            self.set_action(worker, f"{email} 订阅异常: {str(e)[:80]}")
            return "failed", str(e)[:200]
        finally:
            worker.clear_active_driver()
            close_driver(session)

    def run_daily_subscribe_pipeline(self, platform, group_id, captcha_api_key=None,
                                     captcha_server="api.multibot.cloud"):
        """每日订阅任务：账号轮转——未注册先注册、已注册登录订阅，成功即换下一个账号。

        platform 是目标平台 slug，语义同 run_daily_pipeline：账号状态与卡的占用按它隔离。

        captcha_server: hCaptcha 求解服务，默认 Multibot（api.multibot.cloud）；可传 2captcha.com。

        镜像 run_daily_pipeline 的串行轮转/停止/兜底骨架，但：待订阅账号集 = 身份层与本平台
        状态均非终态；单账号动作 = _subscribe_one_account；订阅成功的账号从后续轮次剔除。
        停止：无可选卡 / 无待订阅账号 / 用户停止 / 整轮零进展兜底。
        """
        self.is_running = True
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "每日订阅任务启动中"
        self.update_frame(None)
        self._patch_prints()

        self.platform = platform
        account_model = self.models['account']
        platform_account_model = self.models['platform_account']
        subscribed_total = 0
        fail_total = 0
        pool = WorkerPool(self, 1)      # 串行
        round_lock = threading.Lock()

        try:
            self._hooked_print(f"\n{'#' * 50}")
            self._hooked_print("每日订阅任务开始（账号轮转：注册/登录 + Stripe 订阅）")
            self._hooked_print(f"{'#' * 50}")

            eligible = len(self._eligible_cards(group_id, exclude_used=False))
            self._hooked_print(f"分组可选卡 {eligible} 张")
            if not eligible:
                self._hooked_print("分组内无可选卡，无事可做")
                return

            # 待订阅 = 身份层与本平台状态都非终态。
            # 身份层排除封禁/挂起/被拒/被 flag（flagged=GitHub 禁授权，无解，所有平台通吃）；
            # 平台层排除本平台已订阅/已充值/已归档——同一邮箱在别的平台的进度不影响这里。
            def _needing():
                # 同 _payable_now：作为排除依据的平台状态后读，缩小重复派发的窗口。
                accounts = account_model.get_all(order_desc=False)
                platform_status = platform_account_model.map_by_email(platform)
                return [a for a in accounts
                        if not is_identity_terminal(a.get('identity_status'))
                        and not is_platform_terminal(
                            (platform_status.get(a['email']) or {}).get('status'))]

            MAX_ROUNDS = len(_needing()) * 5 + 5
            round_num = 0
            while round_num < MAX_ROUNDS:
                if self.stop_requested:
                    self._hooked_print("用户停止了任务")
                    break
                remaining = len(self._eligible_cards(group_id, exclude_used=False))
                if not remaining:
                    self._hooked_print("已无可选卡，任务结束")
                    break
                accounts = _needing()
                if not accounts:
                    self._hooked_print("已无待订阅账号（都 subscribed/banned），任务结束")
                    break
                round_num += 1
                round_stats = {'subscribed': 0, 'other': 0}
                self._hooked_print(f"\n{'=' * 50}\n订阅轮次 {round_num}"
                                   f"（待订阅账号 {len(accounts)} 个，可选卡 {remaining} 张）\n{'=' * 50}")

                def _do(worker, acct):
                    email = acct['email']
                    if self.stop_requested or not self._eligible_cards(group_id, exclude_used=False):
                        return
                    if not self.account_registry.claim(email, owner=self.platform):
                        self._hooked_print(f"{email} 正被占用，本轮跳过")
                        return
                    try:
                        self.set_action(worker, f"轮次{round_num} 订阅账号 {email}")
                        self._hooked_print(f"\n订阅账号: {email}")
                        result, detail = self._subscribe_one_account(
                            acct, group_id, captcha_api_key, worker=worker,
                            captcha_server=captcha_server)
                        with round_lock:
                            if result == "subscribed":
                                round_stats['subscribed'] += 1
                                self.success_count += 1
                            else:
                                round_stats['other'] += 1
                                self.fail_count += 1
                        self._hooked_print(f"{email} → {result}（{detail}）")
                    except InterruptedError:
                        self._hooked_print("订阅阶段被中断")
                        raise
                    except AdsPowerError as e:
                        # 与充值管线同理，理由见那边的长注释：多平台并发后不能再
                        # 「置全局 stop 整批收敛」——配额由仲裁器管、拿不到会先等，
                        # 而按平台拆分后置 stop 只停自己、配额却是共用的，
                        # 结果是一个平台饿死等待、另一个反复抛错自杀。
                        # 改为只跳过本账号 + 请求归还，由「整轮零进展」兜底收敛。
                        asked = self.quota.request_recall(self.platform)
                        note = f"；已向借用方请求归还 {asked} 个额度" if asked else ""
                        self._hooked_print(f"AdsPower 环境暂不可用，跳过 {email}: {e}{note}")
                    except Exception as e:
                        with round_lock:
                            round_stats['other'] += 1
                            self.fail_count += 1
                        self._hooked_print(f"订阅 {email} 出错: {e}")
                    finally:
                        self.account_registry.release(email)

                pool.map(accounts, _do)
                subscribed_total += round_stats['subscribed']
                fail_total += round_stats['other']

                # 进展 = 本轮有订阅成功，或可选卡减少（拒付标无效）。零进展兜底防死循环。
                after = len(self._eligible_cards(group_id, exclude_used=False))
                progressed = round_stats['subscribed'] > 0 or after < remaining
                if not progressed and not self.stop_requested:
                    self._hooked_print("整轮无订阅成功且无卡被消耗，结束任务（兜底防死循环）")
                    break

        except Exception as e:
            self._hooked_print(f"严重错误: {e}")
        finally:
            for w in self.workers.values():
                w.clear_active_driver()
                w.stop_screenshot_loop()
                w.current_action = "空闲"
                w.busy = False
            # ⚠️ 三个 registry 都是**跨平台共享**的，收尾只能释放自己那份。
            # 无参形式是全清——一个平台跑完就把另一个平台正在持有的账号、卡、代理
            # 全部放掉，它的排他保护瞬间蒸发：两个 worker 同用一个 Chrome profile
            # 互删 Singleton 锁、同一张卡被两边同时提交给发卡行、同一个出口 IP 被
            # 重复领取。三种后果都不报错。
            self.account_registry.release_all(owner=self.platform)
            self.payment_registry.release_all(self.platform, include_in_flight=True)
            self.proxy_registry.release_all(owner=self.platform)
            self._stop_started_adspower()
            self.parallel_mode = False
            self.is_running = False
            try:
                remaining = len(self._eligible_cards(group_id, exclude_used=False))
            except Exception:
                remaining = '?'
            self.current_action = (f"每日订阅任务完成（订阅成功 {subscribed_total} 个 / "
                                   f"未成 {fail_total} 次 / 剩余可选卡 {remaining} 张）")
            self._hooked_print(f"\n{'#' * 50}")
            self._hooked_print(self.current_action)
            self._hooked_print(f"{'#' * 50}")

    def _patch_prints(self):
        """装 print 钩子，并把本 ctx 绑成**当前线程**的日志归属。

        名字与调用点都沿用改造前的，但语义变了两处：
        1. 钩子是进程级的、只装一次（装的是模块级 dispatch_print，与实例无关）；
        2. 「这条日志属于谁」不再由钩子里绑死的 self 决定，而是运行时从 contextvar 解析。

        返回 token 供调用方 reset。既有调用点都不接返回值——它们跑在各自的流水线
        线程里，线程结束后 context 随之消失，不 reset 也不会泄漏到别处。
        """
        patch_prints()
        return self.bind_logs()


def gen_frames(worker):
    """MJPEG 帧生成器。接 WorkerState，使每个 worker 有独立的实时画面。"""
    while True:
        frame = worker.get_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/png\r\n\r\n' + frame + b'\r\n')
        time.sleep(0.15)


def build_models(db):
    """构造全部模型。抽成函数是为了让测试能拿到与生产**同一份**模型集合——
    在测试里手抄一份 dict，迟早会漏掉新加的 key，然后以「KeyError: 'settings'」
    这种与被测行为毫无关系的方式失败。"""
    return {
        'account': AccountModel(db),
        'platform_account': PlatformAccountModel(db),
        'task': TaskModel(db),
        'card_binding': CardBindingModel(db),
        'recharge_log': RechargeLogModel(db),
        'card_group': CardGroupModel(db),
        'card_pool': CardPoolModel(db),
        'valid_card': ValidCardModel(db),
        'card_state': CardPaymentStateModel(db),
        'proxy': ProxyModel(db),
        'adspower_profile': AdsPowerProfileModel(db),
        'settings': SettingsModel(db),
    }


def create_app(db_path=None):
    """Flask 应用工厂"""
    base_dir = get_base_dir()

    # 静态文件目录
    if getattr(sys, 'frozen', False):
        static_dir = os.path.join(sys._MEIPASS, 'static')
    else:
        static_dir = str(base_dir / 'static')

    app = Flask(__name__, static_url_path='', static_folder=static_dir)
    # 让 jsonify 直接输出中文，而非 \uXXXX 转义（错误提示等含中文时更可读）
    app.config['JSON_AS_ASCII'] = False
    try:
        app.json.ensure_ascii = False   # Flask 2.3+ 新式 JSON provider
    except Exception:
        pass

    # 初始化数据库（路径独立于程序目录，升级版本不丢数据）
    if db_path is None:
        db_path = str(get_data_dir() / "openrouter_auto.db")
    db = Database(db_path)

    # 创建模型
    models = build_models(db)

    # 创建应用状态：一份共享资源 + 每个已注册平台一个运行上下文。
    #
    # 平台列表从适配器注册表取——注册表是平台的唯一真相来源，没有 platforms 表。
    # 取不到（极端情况）就退化成只建默认平台，服务仍能起来。
    shared = SharedResources(db, models)
    try:
        import src.platforms as _platforms
        slugs = list(_platforms.all_slugs())
    except Exception:
        slugs = []
    if AppState.DEFAULT_PLATFORM not in slugs:
        slugs.append(AppState.DEFAULT_PLATFORM)

    contexts = {s: AppState(db, models, platform=s, shared=shared) for s in slugs}
    # APP_STATE 保留指向默认平台的 ctx：既有代码与测试大量依赖它，
    # 按平台寻址在 Stage 5 才切换。
    state = contexts[AppState.DEFAULT_PLATFORM]

    # 进程重启意味着所有 worker 都已消失，其领取的卡必须无条件释放，
    # 否则会永远停在 processing 把卡池慢慢吃空。
    try:
        reset = models['card_binding'].reset_all_processing()
        if reset:
            state.add_log(f"[启动] 重置了 {reset} 张上次运行残留的已领取卡")
    except Exception as e:
        state.add_log(f"[启动] 重置残留卡失败: {e}")

    # 回收失联 worker 领取的卡（运行中的超时兜底）
    reaper = ClaimReaper(models['card_binding'], state,
                         cfg.concurrency.claim_timeout_minutes)
    reaper.start()

    # 注入到 Flask app config
    app.config['DB'] = db
    app.config['MODELS'] = models
    app.config['APP_STATE'] = state
    # 按平台寻址用的全部上下文。Stage 5 之前只有 /api/platforms 之类的只读接口会用到，
    # 真正的按平台分发在 Stage 5 切换。
    app.config['RUN_CONTEXTS'] = contexts
    app.config['SHARED'] = shared
    app.config['REAPER'] = reaper

    # 注册蓝图
    app.register_blueprint(api)

    # 首页
    @app.route('/')
    def index():
        return send_from_directory(static_dir, 'index.html')

    # MJPEG 流。?worker=W2 指定 worker，?platform=infron 指定平台；
    # 两个都缺省时取默认平台的主 worker（老 URL 保持可用）。
    #
    # platform 不能省：两个平台各有一套同名的 W1..W4，而 get_worker 对未知 id
    # 会**回落到主 worker**——取错 ctx 不会 404，只会安静地播另一个平台的画面。
    @app.route('/video_feed')
    def video_feed():
        from flask import request
        ctx = contexts.get(request.args.get('platform') or '') or state
        worker = ctx.get_worker(request.args.get('worker'))
        return Response(gen_frames(worker),
                        mimetype='multipart/x-mixed-replace; boundary=frame')

    return app
