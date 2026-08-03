"""
Flask 应用工厂 & AppState
"""

import os
import sys
import time
import random
import json
import threading
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


def _to_pw_proxy(row):
    """proxies 表 row（host/port/username/password）→ Playwright proxy dict。
    server 只放 scheme+host+port，凭据走 username/password 字段（不 URL 内嵌）。
    bypass 让 Stripe 付款域名直连（代理商封了 stripe，见 _PROXY_BYPASS）。"""
    d = {"server": f"http://{row['host']}:{row['port']}", "bypass": _PROXY_BYPASS}
    if row.get('username'):
        d["username"] = row['username']
        d["password"] = row.get('password') or ''
    return d


class AppState:
    """全局应用状态（运行时内存数据）。

    分层：本类持有全局聚合信息（is_running / 停止标志 / 总计数 / 聚合日志），
    每个浏览器实例的隔离状态在 WorkerState 里（src/web/worker.py）。

    始终存在一个主 worker 'W1'：串行路径（单账号充值等）与 max_workers=1 时
    都走它，因此下列 set_active_driver / _stop_screenshot_loop / _monitor 等
    委托方法的行为与并行化改造前完全一致。
    """

    PRIMARY_WORKER_ID = 'W1'

    DEFAULT_PLATFORM = 'opencode'

    def __init__(self, db, models):
        self.db = db
        self.models = models

        # 当前流水线的目标平台。本次改造只支持「一次跑一个平台」——AppState 是
        # 单例，is_running / 计数器 / 三个内存注册表都是全局的，两个平台同时跑
        # 会互相清对方的占用。启动流水线时由入口显式设置，跑完不重置（列表页等
        # 读接口沿用它作为默认平台）。
        self.platform = self.DEFAULT_PLATFORM

        self.is_running = False
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "空闲"
        self.logs = []
        self.lock = threading.Lock()

        # 当前信用卡驱动任务 ID
        self.current_card_task_id = None

        # 按账号独立跟踪的浏览器查看会话（不阻塞全局任务）
        self.open_browsers = set()

        # worker 运行时。W1 是主 worker，恒存在。
        # workers 只增不减（旧 worker 的日志留着供回看）；对外展示以
        # active_worker_count 为准，见 active_workers()。
        self.workers = {self.PRIMARY_WORKER_ID: WorkerState(self.PRIMARY_WORKER_ID)}
        self.active_worker_count = 1
        self._workers_lock = threading.Lock()

        # 是否处于并行模式（由 WorkerPool 依 max_workers 设置）。仅影响聚合日志
        # 是否带 [Wn] 前缀，使串行输出与改造前逐字一致。
        self.parallel_mode = False

        # 并发排他：账号（Chrome profile 单实例约束）与支付卡（选卡闸门时间差）
        self.account_registry = AccountRegistry(self)
        self.payment_registry = PaymentCardRegistry()
        self.proxy_registry = ProxyRegistry()

        # AdsPower 指纹浏览器接入（cfg.adspower.enabled 为假时恒为 None，全链路不受影响）。
        # 惰性构造：启动时不去连 AdsPower，免得客户端没开就起不来服务。
        self._adspower_pool = None
        self._adspower_client = None
        self._adspower_lock = threading.Lock()
        # 本次运行启动过的环境（profile_id）。任务收尾时逐个 stop，避免用户点停止或
        # 任务异常退出后留下一堆开着的浏览器（每个都吃几百 MB 内存）。
        self._adspower_started = set()

    # ---------- AdsPower 环境池 ----------

    @property
    def adspower_enabled(self):
        return bool(cfg.adspower.enabled)

    def _ensure_adspower(self):
        """惰性创建 AdsPower 客户端与环境池。未启用时返回 (None, None)。"""
        if not self.adspower_enabled:
            return None, None
        with self._adspower_lock:
            if self._adspower_pool is None:
                from src.services.adspower import AdsPowerClient
                from src.browser.adspower_driver import AdsPowerProfilePool
                self._adspower_client = AdsPowerClient(
                    cfg.adspower.base_url, cfg.adspower.api_key)
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
            return self._adspower_client, self._adspower_pool

    def browser_factory(self):
        """返回 callable(email) -> BrowserSession；未启用 AdsPower 时返回 None。

        下游（registration.recharge_account / signup_one / _subscribe_one_account）
        统一以「factory 为 None 就走原路径」的方式接入，因此关掉开关时代码路径
        与接入前完全一致。
        """
        client, pool = self._ensure_adspower()
        if pool is None:
            return None
        from src.browser.adspower_driver import create_driver_adspower

        def _factory(email):
            session = create_driver_adspower(email, pool, client)
            pid = getattr(session, 'adspower_profile_id', None)
            if pid:
                with self._adspower_lock:
                    self._adspower_started.add(pid)
            return session

        return _factory

    def _stop_started_adspower(self):
        """收尾：关掉本次运行启动过的所有环境（幂等，异常不外溢）。"""
        with self._adspower_lock:
            pending = list(self._adspower_started)
            self._adspower_started.clear()
            client = self._adspower_client
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
        """被劫持的 print 入口：按调用线程所属 worker 路由日志。

        worker 线程内（已 bind_current_worker）→ 进该 worker 的分栏日志，
        同时以 [Wn] 前缀进聚合流；worker 外（串行路径/请求线程）→ 只进聚合流。
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
        """返回该分组在指定平台当前「可选」的卡，有序：新卡优先，再复用好卡。

        platform 省略时用 self.platform（当前流水线/界面选中的平台）。整条判定链——
        可用状态、冷却、新卡还是好卡、本轮是否被试过——全部按这个平台算，所以同一张卡
        在别的平台的遭遇不会影响这里的结果。

        可选 = get_usable_cards_as_list（已排除 expired/invalid/bound）且不处于临时冷却
        （3DS / 「曾成功卡本次被拒」的速率冷却）中。排序：
          - 新卡（从未成功付款过）优先，先把卡池的新卡消耗掉；
          - 之后才复用已成功过的好卡（paid 卡可反复支付）。
        成功卡不被永久消耗，只有被拒（好卡→冷却，坏卡→无效）或过期才退出可选集。

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
        cards = fresh + good
        return self._exclude_used_this_run(platform, cards) if exclude_used else cards

    def _recharge_one_account(self, email, login_password, payment_group_id=None,
                              worker=None, captcha_api_key=None,
                              captcha_server="api.multibot.cloud", proxy=None,
                              verify_link=None):
        """为单个账号执行一次充值访问，返回 (result, err)，
        result ∈ {"success", "failed", "archived"(余额≥$20 已归档、未扣款)}。

        captcha_api_key/captcha_server 透传给 registration.recharge_account 用于自动解 hCaptcha。

        用 payment_group_id 指定分组的可选卡（_eligible_cards：新卡优先，再复用好卡；已排除
        无效/过期/冷却）逐张尝试，付成一张即 success。逐卡的卡状态标记（paid/invalid/冷却）
        与 recharge_logs 记账都在 registration.recharge_account 内部完成，本方法只负责取卡、
        调度、把结果转成计数用的 (result, err)。

        只负责一个账号的一次操作，不管理 is_running / 截图收尾（由调用方负责）。
        InterruptedError 向上抛出，供轮转循环感知停止。

        worker: 执行本次操作的 WorkerState；为 None 时用主 worker（串行路径）。"""
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
            )

            if outcome == "topup":
                self.set_action(worker, f"{email} 充值成功（卡 {card_last4}）")
                self.add_log(f"{email} AI Credits 充值 $20 成功（卡 {card_last4}）")
                return "success", ''

            if outcome == "archived":
                # 余额≥阈值已归档（未扣款）：既非成功也非失败，该账号退出后续轮转
                self.set_action(worker, f"{email} 余额≥$20，已归档跳过")
                self.add_log(f"{email} 余额≥$20，已归档跳过充值（{err}）")
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
                           captcha_server="api.multibot.cloud"):
        """每日充值任务：卡池驱动的账号轮转充值，串行跑在单个后台线程。

        platform 是目标平台 slug。账号状态、卡的占用与冷却全部按它隔离——同一邮箱、
        同一张卡在别的平台上的记录不影响本次运行。一次只能跑一个平台（AppState 单例）。

        选定一个卡池分组，用账号列表逐账号轮转充值：一个账号在其会话内充成 1 张卡后即轮转
        到下一个账号。**以刷完卡池为第一标准**：只要分组还有可选卡且还有账号可用就继续跑；
        充值失败的账号只跳过**本轮**，一轮轮完（所有账号都试过一遍）后清空失败名单、
        回到头部开下一轮重试（A 失败→换 B→…→下一轮再试 A）。停止条件（满足其一）：
          1. 分组可选卡耗尽（全部无效/过期或冷却中）；
          2. 无账号可用：payable + imported 都领不到，且连续两整轮零进展
             （没付成一张卡、可选卡集合也没变化——再轮转只会原样重复，防死循环兜底；
             容忍一轮零进展是为了吸收登录/网络类瞬时抖动）；
          3. 用户手动停止。

        选卡资格（见 _eligible_cards）：新卡优先，付款成功过的好卡可反复复用；只有从未成功
        的坏卡被拒（判无效）、好卡被拒（24h 速率冷却）或过期，才退出可选集。逐卡的卡状态
        标记与 recharge_logs 记账在 recharge_account 内部完成；本方法只负责取可选卡、轮转
        账号、计数与收尾。

        captcha_api_key/captcha_server 透传给充值流程用于自动解 hCaptcha（server 默认 Multibot）。
        账号选取排除身份层终态（banned/suspended/rejected/flagged）与本平台的平台层终态
        （archived/recharged/subscribed，见 utils 里的两组终态常量）；登录后实时余额 ≥$20
        的账号会在本平台被归档并退出后续轮转；GitHub 被 flag 无法授权 OAuth 的账号会被标
        身份层 flagged——那是 GitHub 侧的封禁，对所有平台一致。

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
        pool = WorkerPool(self, cfg.concurrency.max_workers)

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
                p = self.proxy_registry.acquire_free(usable, worker.worker_id)
                if p is not None:
                    return _to_pw_proxy(p), self.proxy_registry.key_of(p)
                p = usable[(account_id or 0) % len(usable)]   # 取模兜底,不排他
                return _to_pw_proxy(p), None

            accounts = _payable_now()
            imported_pending = len(_registerable_imported())
            eligible = len(self._eligible_cards(group_id, exclude_used=False))
            if self.adspower_enabled:
                proxy_note = "浏览器走 AdsPower 指纹环境，代理由环境绑定"
            else:
                proxy_count = proxy_model.count()
                proxy_note = (f"代理 {proxy_count} 个"
                              f"{'（未配置代理，直连）' if not proxy_count else ''}")
            self._hooked_print(
                f"可充值账号 {len(accounts)} 个，待注册 imported {imported_pending} 个，"
                f"分组可选卡 {eligible} 张，{proxy_note}")
            if not eligible:
                self._hooked_print("分组内无可选卡（全部无效/过期或冷却中），无事可做")
                return
            if not accounts and not imported_pending:
                self._hooked_print("无可充值账号且无 imported 可注册，任务结束")
                return

            # ── 生产者：每个 worker 反复原子领一个账号，按「轮」轮转。优先充值现有可充
            #    账号（跳过本轮已失败的），无则领一个待注册 imported。都领不到时分三种情况：
            #      wait —— 还有账号在其它 worker 手上，本轮胜负未分，睡 5s 再看（不能直接
            #              退出：在飞账号失败后会回到轮转池，退出会白白减员）；
            #      开新一轮 —— 无人在飞且本轮有失败账号、且上一轮有进展（付成过或可选卡
            #              集合变过），清空失败名单从头重试，并清掉「本轮已试过的卡」标记；
            #      done —— 卡池耗尽 / 无账号可用 / 整轮零进展（再轮只会原样重复），收敛。
            #    produce_lock 让「找账号 + claim + 轮转判定」原子，两 worker 绝不领同一个。
            #    领到即用 account_registry 占坑，消费者 finally 释放。返回 None 表示任务收敛。
            round_state = {
                'no': 1,
                'paid_at_start': 0,
                'cards_at_start': None,   # 本轮开始时的可选卡键集合，判「零进展」用
                'zero_rounds': 0,         # 连续零进展轮数；≥2 才停（容忍一轮瞬时抖动）
            }
            end_logged = [False]          # 收敛原因只打一次（多 worker 会各拿到一次 done）

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
                for a in _payable_now():
                    if a['email'] in failed_this_round:
                        continue
                    if self.account_registry.claim(a['email']):
                        proxy, pkey = _acquire_proxy_for(a.get('id', 0))
                        return 'item', ('recharge', a, proxy, pkey)
                for a in _registerable_imported():
                    if self.account_registry.claim(a['email']):
                        proxy, pkey = _acquire_proxy_for(a.get('id', 0))
                        return 'item', ('register', a, proxy, pkey)
                if self.account_registry.snapshot():
                    return 'wait', None
                if not failed_this_round:
                    return 'done', "无可充值账号且无 imported 可注册"
                # ── 轮转边界：所有账号都试过一遍且无人在飞。有进展就开下一轮重试失败账号
                with state_lock:
                    paid_now = stats['paid']
                cards_now = _card_keys_now()
                progressed = (paid_now > round_state['paid_at_start']
                              or cards_now != round_state['cards_at_start'])
                if progressed:
                    round_state['zero_rounds'] = 0
                else:
                    round_state['zero_rounds'] += 1
                    if round_state['zero_rounds'] >= 2:
                        return 'done', (f"连续 {round_state['zero_rounds']} 轮零进展"
                                        "（未付成一张卡且可选卡集合未变化），账号已全部试尽")
                retrying = len(failed_this_round)
                round_state['no'] += 1
                round_state['paid_at_start'] = paid_now
                round_state['cards_at_start'] = cards_now
                with state_lock:
                    failed_this_round.clear()
                # 「本轮已被试过的卡」标记随轮清零。只清本平台的归属，in-flight 不动
                # ——那是全局的发卡行 velocity 防护，不该被轮边界打断。
                self.payment_registry.release_all(platform)
                zero_note = ("，上轮零进展（容忍一轮，可能是瞬时抖动）"
                             if round_state['zero_rounds'] else "")
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
            #    移交给 _try_claim 的「整轮零进展」判定，卡池刷完前失败账号可循环使用。
            def _do(worker, item):
                kind, acct, proxy, pkey = item
                email = acct['email']
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
                            verify_link=acct.get('email_verify_link'))
                        with state_lock:
                            if result == "success":
                                stats['paid'] += 1
                                self.success_count += 1
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
                    # 浏览器起不来（配额满 / 客户端没开 / Key 无效）。这不是账号的问题，
                    # 所以**不动账号状态**——标 failed 会让 imported 账号退出补号池永不重试。
                    # 也不必逐个账号重试：环境配额是全局的，下一个账号必然撞同一堵墙，
                    # 整批立刻收敛才能让「配额已满」这条关键信息留在日志顶端而不被淹掉。
                    with state_lock:
                        done.add(email)   # 本轮不再重领，但账号状态保持原样
                    self.stop_requested = True
                    self._hooked_print(f"AdsPower 环境不可用，终止本次任务: {e}")
                except Exception as e:
                    with state_lock:
                        stats['fail'] += 1
                        self.fail_count += 1
                        # 异常（浏览器崩溃等）多为瞬时问题，同充值失败：只禁本轮
                        failed_this_round.add(email)
                    self._hooked_print(f"处理 {email} 出错: {e}")
                finally:
                    self.account_registry.release(email)
                    if pkey:
                        self.proxy_registry.release(pkey)   # 排他领取的代理释放回池

            # 收敛：每轮要么有进展（付成 / 可选卡集合缩小——卡被标 invalid/冷却，集合有限
            # 单调消耗），要么零进展被 _try_claim 判停。账号可跨轮循环使用但轮数有界，
            # _produce 终会返回 None → 所有 worker 退出。
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
            self.account_registry.release_all()
            self.payment_registry.release_all()
            self.proxy_registry.release_all()
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
                       then_opencode=False, auto_skip_captcha=True, proxy=proxy,
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
        from src.platforms.opencode.login import login_and_open_own_go
        from src.platforms.opencode.subscribe import subscribe_via_stripe
        from src.services import captcha as captcha_solver

        models = self.models
        worker = worker or self.primary_worker
        platform = self.platform
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

            lg = login_and_open_own_go(session)
            if not lg.get('ok'):
                if lg.get('flagged'):
                    # 被 GitHub flag，无法授权任何第三方 OAuth → 标**身份层** flagged。
                    # 这是 GitHub 侧的封禁，对所有平台一致，不是本平台的状态。
                    models['account'].update_identity_status(email, 'flagged')
                    self.set_action(worker, f"{email} GitHub 被 flagged，跳过")
                    return "skipped", "GitHub 账号被 flagged，无法授权"
                self.set_action(worker, f"{email} 登录失败: {lg.get('detail','')[:60]}")
                return "failed", f"登录失败: {lg.get('detail')}"
            wid = lg['wid']

            # 账号内逐卡试付，成功即止（快照迭代；拒付卡已被标 invalid 退出后续可选集）
            cards = self._eligible_cards(payment_group_id) if payment_group_id else []
            if not cards:
                return "registered_only", "无可选卡"
            cards = cards[:self.SUBSCRIBE_MAX_CARDS_PER_ACCOUNT]   # 单次卡数上限，避免坏卡卡死
            for i, card in enumerate(cards, 1):
                if self.stop_requested:
                    raise InterruptedError("用户请求停止")
                num = card.get('number', '')
                last4 = str(num)[-4:]
                self.set_action(worker, f"{email} 订阅试卡 ****{last4}")
                self.add_log(f"{email} 订阅试卡 ****{last4}（第 {i}/{len(cards)} 张）")
                log_id = models['recharge_log'].create(platform, email, num, amount=5)
                res = subscribe_via_stripe(session, card, wid, monitor=monitor,
                                           should_stop=lambda: self.stop_requested, dry=False)
                oc = res.get('outcome')
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
                    # 曾成功过的有效卡（in valid_cards）本次被拒：不判无效，改打 24h 速率冷却，
                    # 本轮跳过、到期恢复可用；从未成功过的卡才判无效。与 registration.py 一致。
                    # 判据按平台算：卡在别的平台成功过不算数，那边的商户与风控都不同。
                    if models['valid_card'].is_valid(platform, num):
                        models['card_state'].set_cooldown(
                            platform, num, hours=24, reason='曾成功卡本次支付失败，速率冷却')
                        note = '曾成功有效卡，24h 冷却'
                    else:
                        models['card_pool'].mark_invalid_by_number(platform, num)
                        note = '标 invalid'
                    models['recharge_log'].mark_failed(log_id, error=res.get('err', ''),
                                                       api_response={"result": res})
                    self.add_log(f"{email} 卡 ****{last4} 拒付，{note}，换下一张")
                elif oc == 'needs_captcha':
                    # hCaptcha 未过：非卡问题，不标卡无效，换下一张（换次提交 token 可能就过）
                    models['recharge_log'].mark_failed(log_id, error='hCaptcha 未过',
                                                       api_response={"result": res})
                    self.add_log(f"{email} 卡 ****{last4} hCaptcha 未过，换下一张")
                else:  # error / unknown：不耗卡，换下一张
                    models['recharge_log'].mark_failed(log_id, error=res.get('err', '') or oc,
                                                       api_response={"result": res})
                    self.add_log(f"{email} 卡 ****{last4} 未定案({oc}): "
                                 f"{(res.get('err') or '')[:90]}，换下一张")
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
                    if not self.account_registry.claim(email):
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
                        # 与充值管线同理：浏览器起不来是全局故障，不是这个账号的问题。
                        # 下一个账号必然撞同一堵墙，整批立刻收敛。
                        self.stop_requested = True
                        self._hooked_print(f"AdsPower 环境不可用，终止本次任务: {e}")
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
            self.account_registry.release_all()
            self.payment_registry.release_all()
            self.proxy_registry.release_all()
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
        """劫持相关模块的 print 函数以捕获日志"""
        hooked = self._hooked_print
        registration.print = hooked
        try:
            from src.browser import driver as browser_module
            browser_module.print = hooked
        except Exception:
            pass
        # 订阅链路各模块的 print 也劫持，便于 Web 日志捕获
        for _mod in ('src.platforms.opencode.subscribe', 'src.platforms.opencode.login',
                     'src.platforms.opencode.billing', 'src.services.github_signup_service'):
            try:
                import importlib
                importlib.import_module(_mod).print = hooked
            except Exception:
                pass
        try:
            from src.services import email as email_module
            email_module.print = hooked
        except Exception:
            pass
        try:
            from src.services import captcha as captcha_module
            captcha_module.print = hooked
        except Exception:
            pass


def gen_frames(worker):
    """MJPEG 帧生成器。接 WorkerState，使每个 worker 有独立的实时画面。"""
    while True:
        frame = worker.get_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/png\r\n\r\n' + frame + b'\r\n')
        time.sleep(0.15)


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
    models = {
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
    }

    # 创建应用状态
    state = AppState(db, models)

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
    app.config['REAPER'] = reaper

    # 注册蓝图
    app.register_blueprint(api)

    # 首页
    @app.route('/')
    def index():
        return send_from_directory(static_dir, 'index.html')

    # MJPEG 流。?worker=W2 指定 worker；缺省取主 worker（老 URL 保持可用）
    @app.route('/video_feed')
    def video_feed():
        from flask import request
        worker = state.get_worker(request.args.get('worker'))
        return Response(gen_frames(worker),
                        mimetype='multipart/x-mixed-replace; boundary=frame')

    return app
