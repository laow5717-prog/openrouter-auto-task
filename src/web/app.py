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

from src.config import cfg, get_base_dir, get_data_dir, INVOICE_DAILY_CAP
from src.models.database import Database
from src.models.account import AccountModel
from src.models.task import TaskModel
from src.models.card_binding import CardBindingModel
from src.models.recharge_log import RechargeLogModel
from src.models.card_group import CardGroupModel
from src.models.card_pool import CardPoolModel
from src.models.valid_card import ValidCardModel
from src.models.card_payment_state import CardPaymentStateModel
from src.models.invoice_payment_state import InvoicePaymentStateModel
from src.services import registration, card as card_service
from src.api.routes import api
from src.web.worker import (
    WorkerState, WorkerPool, AccountRegistry, PaymentCardRegistry, ClaimReaper,
    get_current_worker,
)


class AppState:
    """全局应用状态（运行时内存数据）。

    分层：本类持有全局聚合信息（is_running / 停止标志 / 总计数 / 聚合日志），
    每个浏览器实例的隔离状态在 WorkerState 里（src/web/worker.py）。

    始终存在一个主 worker 'W1'：串行路径（单账号充值等）与 max_workers=1 时
    都走它，因此下列 set_active_driver / _stop_screenshot_loop / _monitor 等
    委托方法的行为与并行化改造前完全一致。
    """

    PRIMARY_WORKER_ID = 'W1'

    def __init__(self, db, models):
        self.db = db
        self.models = models

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

    def _recharge_one_account(self, email, login_password, payment_group_id=None,
                              single_step=False, invoice_daily_cap=None, worker=None):
        """充值单个账号并记账，返回 (result, err, info)。

        全量模式（single_step=False，现状）result 取值：
          - "success"      实际 Top-up 成功
          - "invoice_only" 未充值（今日已充），仅检查/处理了账单，预建 log 已删除，不计充值
          - "failed"       充值失败或异常
        单步模式（single_step=True，每日流水线轮询用）result 取值：
          - "cap_reached"  当日账单已达上限，未做 Top-up，预建 log 已删除
          - "stepped"      做了 1 次 Top-up 生成账单 + 至多付 1 张（info 含 today_count/generated/paid）
          - "failed"       失败或异常
        info: 单步模式为 registration 返回的 dict（含 today_count 等）；全量模式为 {}。

        只负责一个账号的一次操作 + 记账，不管理 is_running / 截图收尾（由调用方负责）。
        InterruptedError 向上抛出，供批量循环感知停止。

        worker: 执行本次操作的 WorkerState。并行时必须传入，使截图与活跃 driver
        落到该 worker 自己的状态上；为 None 时用主 worker（串行路径）。"""
        models = self.models
        worker = worker or self.primary_worker
        monitor = worker.make_monitor(self)

        # 是否有支付卡分组 → 决定是否处理 Unpaid invoices
        skip_invoice = not payment_group_id
        payment_cards = []
        if payment_group_id:
            payment_cards, unusable = models['card_pool'].get_usable_cards_as_list(payment_group_id)
            if unusable:
                self.add_log(f"支付卡分组已跳过 {len(unusable)} 张无效卡（过期/被拒）")
            if not payment_cards:
                self.add_log("支付卡分组无可用卡片数据，将仅执行 Top-up Credits")
                skip_invoice = True

        # 该账号绑定的卡片列表，用于按页面后四位匹配完整卡号
        cards = models['card_binding'].get_by_email(email)
        log_id = models['recharge_log'].create(email, card_display='', amount=10)

        def _match_full_card(card_last4):
            """页面后四位 → 完整卡号（写入 recharge_logs.card_display）。

            先在该账号已绑定的卡里找；找不到再到全局卡池按末 4 位反查（唯一命中才采信）。
            两级都落空时返回裸后四位——绝不写脱敏串，否则 card_display 会混入 '•••• '
            前缀，导致按完整卡号等值匹配的统计/冷却判定（如 success_count_since）静默失效。
            """
            if not card_last4:
                return ''
            for c in cards:
                if c.get('card_number', '').endswith(card_last4):
                    return c['card_number']
            return models['card_pool'].find_number_by_last4(card_last4) or card_last4

        try:
            result = registration.recharge_account(
                email, login_password,
                recharge_log_model=models['recharge_log'],
                monitor_callback=monitor,
                skip_invoice=skip_invoice,
                payment_cards=payment_cards,
                valid_card_model=models['valid_card'],
                card_pool_model=models['card_pool'],
                account_model=models['account'],
                should_stop=lambda: self.stop_requested,
                card_binding_model=models['card_binding'],
                card_state_model=models['card_state'],
                invoice_state_model=models['invoice_state'],
                invoice_daily_cap=(invoice_daily_cap if single_step else None),
                payment_registry=self.payment_registry,
            )

            # ===== 单步（round-robin）模式 =====
            if single_step:
                success, err, responses, card_last4, outcome, info = result
                info = info or {}
                matched_card = _match_full_card(card_last4)
                if matched_card:
                    models['recharge_log'].update_card(log_id, matched_card)

                if outcome == "cap_reached":
                    # 未做 Top-up：删除预建占位 log
                    models['recharge_log'].delete(log_id)
                    tc = info.get('today_count')
                    self.set_action(worker, f"{email} 当日账单已达上限（{tc}）")
                    self.add_log(f"{email} 当日账单数 {tc} 已达上限，跳过 Top-up")
                    return "cap_reached", '', info

                if outcome == "stepped":
                    # Top-up 本身是否真扣款成功由 registration（Stripe confirm 权威）判定；
                    # 常态是 bound 卡 $0 被拒 → 留 open invoice 交卡池卡付（付款记账已在 registration 内完成）。
                    if info.get('topup_ok'):
                        models['recharge_log'].mark_success(log_id, api_response={'responses': responses})
                    else:
                        models['recharge_log'].mark_failed(
                            log_id, error=(err or 'Top-up 未成功')[:200],
                            api_response={'responses': responses})
                    paid = info.get('paid', 0)
                    gen = info.get('generated')
                    tc = info.get('today_count')
                    self.set_action(worker, f"{email} 单步：当日账单 {tc}，付成 {paid} 张")
                    self.add_log(f"{email} 单步完成：当日账单 {tc}，"
                                 f"生成{'成功' if gen else '未增长'}，付成 {paid} 张")
                    return "stepped", '', info

                # outcome == "failed"
                models['recharge_log'].mark_failed(
                    log_id, error=(err or 'recharge failed')[:200],
                    api_response={'responses': responses})
                self.set_action(worker, f"{email} 单步失败: {err}")
                self.add_log(f"{email} 单步失败: {err}")
                return "failed", err or 'recharge failed', info

            # ===== 全量模式（现状，行为不变）=====
            success, err, responses, card_last4, outcome = result

            # 仅处理/检查了账单、未实际 Top-up：删除预建占位 log，避免误记为 $10 充值成功。
            # 账单支付本身的记账已由 recharge_account 内部（_on_invoice_paid/_on_invoice_failed）完成。
            if outcome == "invoice_only":
                models['recharge_log'].delete(log_id)
                self.set_action(worker, f"{email} 未重复充值（已检查/处理账单）")
                self.add_log(f"{email} 今日已充值，已执行账单支付检查，未重复 Top-up")
                return "invoice_only", '', {}

            # 用页面提取的后四位匹配完整卡号（用于 log 展示与有效卡记录）
            matched_card = _match_full_card(card_last4)
            if matched_card:
                models['recharge_log'].update_card(log_id, matched_card)

            # 收尾完全信任 registration 给出的 outcome——其内部以 Stripe payment_intents/confirm
            # 为权威、confirm 未捕获时用余额兜底判定。不再从 CF topup 200/success 推断成功
            # （那只代表已创建支付意图，非实际扣款成功）。
            if outcome == "topup":
                models['recharge_log'].mark_success(log_id, api_response={'responses': responses})
                self.set_action(worker, f"{email} 充值成功")
                self.add_log(f"{email} AI Credits 充值 $10 成功")
                if card_last4:
                    for c in cards:
                        if c.get('card_number', '').endswith(card_last4):
                            models['valid_card'].record(
                                c, source_type='payment', source_email=email,
                            )
                            break
                return "success", '', {}
            else:
                # outcome == "failed"：err 带拒付/未到账原因；拒付卡的失效标记已在 registration 内完成
                models['recharge_log'].mark_failed(log_id, error=err or 'Top-up 未成功',
                                                   api_response={'responses': responses})
                self.set_action(worker, f"{email} 充值失败: {err}")
                self.add_log(f"{email} 充值失败: {err}")
                return "failed", err or 'recharge failed', {}
        except InterruptedError:
            models['recharge_log'].mark_failed(log_id, error='用户中断')
            raise
        except Exception as e:
            models['recharge_log'].mark_failed(log_id, error=str(e))
            self.set_action(worker, f"充值异常: {e}")
            self.add_log(f"充值异常: {e}")
            return "failed", str(e), {}

    def run_daily_pipeline(self, bind_group_id, payment_group_id, login_password,
                           max_bindable_cards, captcha_api_key, mode='full'):
        """每日一键流水线：补绑已有账号 → 注册新号 → 批量充值，串行跑在单个后台线程。

        三段共享同一个 daily task 的 pending 卡池（消耗顺序：补绑 → 注册）。全程持有
        is_running 锁，复用现有日志/截图/停止机制。补绑与充值均设连续失败阈值兜底。

        mode 控制跑哪几段：
          full          绑卡 + 充值（阶段0/1a/1b/2，默认，与历史行为一致）
          bind_only     仅绑卡（跳过阶段2）
          recharge_only 仅充值（跳过阶段0/1a/1b，此时 bind_group_id 可为 None）

        recharge_only 下不建 daily task 记录，task_id 恒为 None——阶段1a/1b 与收尾的
        卡池清理/报告导出都以 task_id 为门，故无需在这些地方再判 mode。"""
        self.is_running = True
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "每日流水线启动中"
        self.update_frame(None)

        self._patch_prints()

        account_model = self.models['account']
        card_binding_model = self.models['card_binding']
        recharge_log_model = self.models['recharge_log']

        task_id = None
        bind_success_total = 0
        recharge_success_total = 0
        recharge_fail_total = 0
        recharge_invoice_only_total = 0

        # 并发执行器。max_workers=1 时走同线程分支，行为与串行实现等价。
        pool = WorkerPool(self, cfg.concurrency.max_workers)
        if not pool.is_serial:
            self._hooked_print(f"并发模式：{pool.max_workers} 个浏览器 worker")

        # 跨 worker 共享的计数器。并发下"连续失败"的语义由"某线程连续失败 N 次"
        # 变为"全局连续失败 N 次"——任一 worker 成功即清零。
        totals_lock = threading.Lock()

        def _bump(name, delta=1):
            """自增并返回自增后的值——调用方要打印计数时必须用返回值，
            事后再读会拿到别的 worker 又推高的数字。"""
            with totals_lock:
                counters[name] = counters.get(name, 0) + delta
                return counters[name]

        counters = {}

        try:
            # ===== 阶段0：准备卡池 =====
            mode_label = {'full': '绑卡 + 充值', 'bind_only': '仅绑卡',
                          'recharge_only': '仅充值'}.get(mode, mode)
            self._hooked_print(f"\n{'#' * 50}")
            self._hooked_print(f"每日流水线开始（模式：{mode_label}）")
            self._hooked_print(f"{'#' * 50}")

            # 仅充值模式不碰卡池：cards 留空后续过滤/建 task 自然全部空转，
            # task_id 保持 None，阶段1a/1b 与收尾的卡池清理都以它为门，无需再判 mode
            if mode == 'recharge_only':
                cards, unusable = [], []
            else:
                cards, unusable = self.models['card_pool'].get_usable_cards_as_list(bind_group_id)
            if unusable:
                self._hooked_print(f"绑卡分组已跳过 {len(unusable)} 张无效卡（过期/被拒）")

            # 过滤已成功绑定过的卡、Stripe 字段错误的卡，以及 Top-up 拒付被标记失效的卡
            # （数据无效或卡已失效，重试无意义）
            already_bound_numbers = card_binding_model.get_successfully_bound_card_numbers()
            stripe_error_numbers = card_binding_model.get_stripe_field_error_card_numbers()
            declined_numbers = card_binding_model.get_declined_card_numbers()
            filtered_cards = []
            skipped = 0
            skipped_stripe_err = 0
            skipped_declined = 0
            for c in cards:
                if c.get('number') in already_bound_numbers:
                    skipped += 1
                elif c.get('number') in stripe_error_numbers:
                    skipped_stripe_err += 1
                elif c.get('number') in declined_numbers:
                    skipped_declined += 1
                else:
                    filtered_cards.append(c)
            if skipped > 0:
                self._hooked_print(f"跳过 {skipped} 张已绑定的卡")
            if skipped_stripe_err > 0:
                self._hooked_print(f"跳过 {skipped_stripe_err} 张 Stripe 字段错误的卡")
            if skipped_declined > 0:
                self._hooked_print(f"跳过 {skipped_declined} 张 Top-up 拒付已失效的卡")

            if filtered_cards:
                task_id = self.models['task'].create('daily', config={
                    'bind_group_id': bind_group_id,
                    'payment_group_id': payment_group_id,
                    'total_cards': len(filtered_cards),
                    'max_bindable': max_bindable_cards,
                })
                self.current_card_task_id = task_id
                card_binding_model.create_batch(task_id, filtered_cards)
                self._hooked_print(f"阶段0：共 {len(filtered_cards)} 张卡待绑定")
            elif mode == 'recharge_only':
                self._hooked_print("阶段0：仅充值模式，跳过卡池准备与绑卡阶段")
            elif mode == 'bind_only':
                self._hooked_print("阶段0：无可用卡，仅绑卡模式无事可做")
            else:
                self._hooked_print("阶段0：无可用卡，跳过补绑/注册，仅执行充值阶段")

            # ===== 阶段1a：补绑已有账号 =====
            if task_id and not self.stop_requested:
                self._hooked_print(f"\n{'=' * 50}\n阶段1a：补绑已有账号\n{'=' * 50}")
                accts = account_model.get_all(order_desc=False)  # 按创建顺序（id 升序）
                emails = [a['email'] for a in accts]
                counts = card_binding_model.count_by_emails(emails)

                def _known_bound(a):
                    """该账号已知的绑卡数：取库内成功数与账单页实测数的较大者。

                    只看库内数会漏掉「卡不在卡池、由外部绑定」的情况——那类账号
                    库内恒为 0，于是每轮任务都要重新登录一次才发现已绑过，纯属浪费。
                    bound_card_count 是登录后从账单页读到的真实值（可能为 None）。
                    """
                    return max(counts.get(a['email'], 0), a.get('bound_card_count') or 0)

                candidates = [
                    a for a in accts
                    if a.get('login_password')
                    and (a.get('status') or '') != 'banned'
                    and _known_bound(a) < max_bindable_cards
                ]
                skipped_full = sum(
                    1 for a in accts
                    if a.get('login_password') and (a.get('status') or '') != 'banned'
                    and _known_bound(a) >= max_bindable_cards)
                self._hooked_print(
                    f"补绑候选账号 {len(candidates)} 个"
                    + (f"（已满 {skipped_full} 个，跳过）" if skipped_full else ""))

                max_consecutive_failures = 3
                counters['bind_fail_streak'] = 0

                def _bind_one(worker, acct):
                    """补绑单个账号。跑在 worker 线程内，全程独占该账号与其领取的卡。"""
                    email = acct['email']
                    if self.stop_requested:
                        return
                    if counters.get('bind_fail_streak', 0) >= max_consecutive_failures:
                        return

                    # 账号排他：同一 Chrome profile 不能被两个 worker 同时使用
                    if not self.account_registry.claim(email):
                        self._hooked_print(f"{email} 正被占用，跳过")
                        return

                    try:
                        # 领定额而非全量：并发下必须先占位，否则两个 worker 会绑同一批卡
                        claimed = card_binding_model.claim_batch(
                            task_id, worker.worker_id, max_bindable_cards)
                        if not claimed:
                            return

                        self.set_action(worker, f"补绑账号 {email}（{len(claimed)} 张卡）")
                        self._hooked_print(f"\n补绑账号: {email}")

                        bound_count, login_ok = registration.bind_cards_to_existing_account(
                            account_model=account_model,
                            card_binding_model=card_binding_model,
                            task_id=task_id,
                            email=email,
                            login_password=acct['login_password'],
                            batch_records=claimed,
                            max_bindable_cards=max_bindable_cards,
                            captcha_api_key=captcha_api_key,
                            monitor_callback=worker.make_monitor(self),
                            # 卡都试完仍未补够时再领一批，复用已登录的浏览器。
                            # 仍走 claim_batch，保持并发下的占位语义不变。
                            claim_more=lambda n: card_binding_model.claim_batch(
                                task_id, worker.worker_id, n),
                            card_pool_model=self.models.get('card_pool'),
                        )
                        if bound_count > 0:
                            with totals_lock:
                                self.success_count += bound_count
                                counters['bind_success'] = counters.get('bind_success', 0) + bound_count
                                counters['bind_fail_streak'] = 0
                            self._hooked_print(f"{email} 补绑了 {bound_count} 张卡")
                        elif not login_ok:
                            # 取 _bump 返回的自增后值，而非事后再读——并发下事后读会
                            # 拿到别的 worker 又推高的数字，日志里出现跳号
                            streak = _bump('bind_fail_streak')
                            self._hooked_print(
                                f"{email} 登录失败"
                                f"（{streak}/{max_consecutive_failures}），"
                                f"卡退回卡池待下个账号")
                        else:
                            self._hooked_print(f"{email} 无需补绑或账单页异常")
                    except InterruptedError:
                        self._hooked_print("补绑被中断")
                        raise
                    except Exception as e:
                        _bump('bind_fail_streak')
                        self._hooked_print(f"补绑 {email} 出错: {e}")
                    finally:
                        # 未用掉的卡退回 pending，供后续账号消费。
                        # 无条件调用：claim_batch 是「先 UPDATE 占位、再 SELECT 回读」，
                        # 若回读阶段抛异常，卡已占位但局部变量为空，加条件判断反而会漏放。
                        card_binding_model.release_unused(task_id, worker.worker_id)
                        self.account_registry.release(email)

                if not pool.is_serial:
                    self.current_action = f"阶段1a 并发补绑（{len(candidates)} 个账号 / {pool.max_workers} worker）"
                pool.map(candidates, _bind_one)
                bind_success_total += counters.get('bind_success', 0)
                if counters.get('bind_fail_streak', 0) >= max_consecutive_failures:
                    self._hooked_print(f"补绑连续失败达到 {max_consecutive_failures} 次，已结束补绑阶段")

            # ===== 阶段1b：注册新号消耗剩余卡 =====
            if task_id and not self.stop_requested:
                # 按「未完成」口径（pending + processing）判断，与 get_summary 一致。
                # 若只看 pending，阶段1a 万一有卡泄漏在 processing，这里会误判为
                # 卡池已空而整个跳过注册阶段，那批卡要等 20 分钟回收时早已收尾。
                remaining = card_binding_model.get_summary(task_id)['pending']
                if remaining:
                    self._hooked_print(f"\n{'=' * 50}\n阶段1b：注册新号（剩余 {remaining} 张卡）\n{'=' * 50}")
                    self._register_bind_loop(task_id, login_password, max_bindable_cards,
                                             captcha_api_key, pool=pool)
                else:
                    self._hooked_print("阶段1b：卡池已被补绑消耗完，无需注册新号")

            # ===== 阶段2：轮询式充值（生成账单 + 逐张支付）=====
            # 防封控：每个账号每轮只「生成 1 张账单 + 用掉 1 张」，随即切下一个账号；
            # 一整轮跑完所有账号后从头再来，直到每个账号当日账单数达 INVOICE_DAILY_CAP
            # 或触发停止条件。当日账单数以 CF invoice-history 接口为权威（recharge_account 内读取）。
            if mode == 'bind_only':
                self._hooked_print("\n阶段2：仅绑卡模式，跳过充值阶段")
            elif not self.stop_requested:
                self._hooked_print(f"\n{'=' * 50}\n阶段2：轮询式充值（每账号当日上限 {INVOICE_DAILY_CAP} 张账单）\n{'=' * 50}")
                accts_after = account_model.get_all(order_desc=False)
                emails_after = [a['email'] for a in accts_after]
                counts_after = card_binding_model.count_by_emails(emails_after)
                # 放行所有绑卡≥1 的账号（今日已充过的也进入，由 recharge_account 内部决定是否补生成账单）
                recharge_targets = [
                    a for a in accts_after
                    if a.get('login_password')
                    and counts_after.get(a['email'], 0) >= 1
                ]
                self._hooked_print(f"充值候选账号 {len(recharge_targets)} 个")

                if payment_group_id:
                    # === 有支付卡分组：轮询式补生成账单 + 逐张支付 ===
                    done = {}                       # email -> 完成原因（cap_reached / abandoned）
                    no_progress = {}                # email -> 连续无进展次数
                    MAX_NOPROG = 3                  # 单账号连续无进展阈值 → 本次流水线内放弃该账号
                    MAX_ROUNDS = INVOICE_DAILY_CAP + 2   # 兜底：正常每轮每账号 +1 张，30 轮内必达上限

                    round_num = 0
                    while round_num < MAX_ROUNDS:
                        if self.stop_requested:
                            self._hooked_print("用户停止了任务")
                            break
                        if all(a['email'] in done for a in recharge_targets):
                            self._hooked_print("所有账号当日账单均已达上限，充值阶段结束")
                            break
                        round_num += 1
                        round_stats = {'progressed': False, 'paid': 0, 'failed': 0}
                        round_lock = threading.Lock()
                        self._hooked_print(f"\n--- 充值轮次 {round_num} ---")

                        def _recharge_one(worker, acct):
                            """本轮推进单个账号一步。跑在 worker 线程内。

                            并行只让**不同账号**同时推进；单个账号在一轮内仍只被
                            推进一次——这是 map 的 barrier 语义保证的，也是原有
                            "每账号每轮只生成 1 张账单"反封控设计的要求。"""
                            email = acct['email']
                            if self.stop_requested or email in done:
                                return

                            # 账号排他：同一 profile 不可并发
                            if not self.account_registry.claim(email):
                                self._hooked_print(f"{email} 正被占用，本轮跳过")
                                return

                            try:
                                self.set_action(worker, f"轮次{round_num} 充值账号 {email}")
                                result, _err, info = self._recharge_one_account(
                                    email, acct['login_password'], payment_group_id,
                                    single_step=True, invoice_daily_cap=INVOICE_DAILY_CAP,
                                    worker=worker)

                                today_count = info.get('today_count')
                                if result == "cap_reached" or (
                                        today_count is not None and today_count >= INVOICE_DAILY_CAP):
                                    done[email] = 'cap_reached'
                                    return

                                # 支付卡池耗尽：再生成账单也无卡可付 → 放弃该账号
                                # （卡池跨账号共享，其它账号也将陆续耗尽并整体收束）
                                if info.get('cards_exhausted'):
                                    done[email] = 'no_cards'
                                    self._hooked_print(f"{email} 支付卡池已耗尽，停止对该账号补生成账单")
                                    if info.get('paid', 0) > 0:
                                        with round_lock:
                                            round_stats['paid'] += info.get('paid', 0)
                                            round_stats['progressed'] = True
                                    return

                                if result == "stepped":
                                    # 有进展：生成了新账单 或 付成了至少 1 张
                                    made_progress = bool(info.get('generated')) or (info.get('paid', 0) > 0)
                                    with round_lock:
                                        if info.get('paid', 0) > 0:
                                            round_stats['paid'] += info.get('paid', 0)
                                        if made_progress:
                                            no_progress[email] = 0
                                            round_stats['progressed'] = True
                                        else:
                                            no_progress[email] = no_progress.get(email, 0) + 1
                                else:
                                    with round_lock:
                                        round_stats['failed'] += 1
                                        no_progress[email] = no_progress.get(email, 0) + 1

                                if no_progress.get(email, 0) >= MAX_NOPROG:
                                    done[email] = 'abandoned'
                                    self._hooked_print(f"{email} 连续 {MAX_NOPROG} 次无进展，本次流水线内放弃该账号")
                            except InterruptedError:
                                self._hooked_print("充值阶段被中断")
                                raise
                            except Exception as e:
                                with round_lock:
                                    round_stats['failed'] += 1
                                    no_progress[email] = no_progress.get(email, 0) + 1
                                if no_progress.get(email, 0) >= MAX_NOPROG:
                                    done[email] = 'abandoned'
                                self._hooked_print(f"充值 {email} 出错: {e}")
                            finally:
                                self.account_registry.release(email)

                        # map 是 barrier：本轮所有账号都推进完才进入下一轮
                        pending_targets = [a for a in recharge_targets if a['email'] not in done]
                        if not pool.is_serial:
                            self.current_action = f"阶段2 轮次{round_num} 并发充值（{len(pending_targets)} 个账号）"
                        pool.map(pending_targets, _recharge_one)
                        recharge_success_total += round_stats['paid']
                        recharge_fail_total += round_stats['failed']

                        if not round_stats['progressed'] and not self.stop_requested:
                            self._hooked_print("整轮无任何账号取得进展，结束充值阶段（兜底防死循环）")
                            break
                else:
                    # === 无支付卡分组：无账单可付，轮询无意义 → 每账号仅 Top-up 一次（全量模式单遍）===
                    self._hooked_print("未选择支付卡分组，仅对每个账号执行一次 Top-up（不补生成账单）")
                    max_consecutive_failures = 3
                    topup = {'streak': 0, 'success': 0, 'invoice_only': 0, 'failed': 0}
                    topup_lock = threading.Lock()

                    def _topup_one(worker, acct):
                        email = acct['email']
                        if self.stop_requested:
                            return
                        with topup_lock:
                            if topup['streak'] >= max_consecutive_failures:
                                return
                        if not self.account_registry.claim(email):
                            self._hooked_print(f"{email} 正被占用，跳过")
                            return
                        try:
                            self.set_action(worker, f"充值账号 {email}")
                            result, _err, _info = self._recharge_one_account(
                                email, acct['login_password'], payment_group_id, worker=worker)
                            with topup_lock:
                                if result == "success":
                                    topup['success'] += 1
                                    topup['streak'] = 0
                                elif result == "invoice_only":
                                    topup['invoice_only'] += 1
                                    topup['streak'] = 0
                                else:
                                    topup['failed'] += 1
                                    topup['streak'] += 1
                        except InterruptedError:
                            self._hooked_print("充值阶段被中断")
                            raise
                        except Exception as e:
                            with topup_lock:
                                topup['failed'] += 1
                                topup['streak'] += 1
                            self._hooked_print(f"充值 {email} 出错: {e}")
                        finally:
                            self.account_registry.release(email)

                    if not pool.is_serial:
                        self.current_action = f"阶段2 并发充值（{len(recharge_targets)} 个账号 / {pool.max_workers} worker）"
                    pool.map(recharge_targets, _topup_one)
                    recharge_success_total += topup['success']
                    recharge_invoice_only_total += topup['invoice_only']
                    recharge_fail_total += topup['failed']
                    if topup['streak'] >= max_consecutive_failures:
                        self._hooked_print(f"充值连续失败达到 {max_consecutive_failures} 次，已停止充值阶段")

        except Exception as e:
            self._hooked_print(f"严重错误: {e}")
        finally:
            # 收尾所有 worker（并行时不止主 worker），并释放全部运行时占用
            for w in self.workers.values():
                w.clear_active_driver()
                w.stop_screenshot_loop()
                w.current_action = "空闲"
                w.busy = False
            self.account_registry.release_all()
            self.payment_registry.release_all()
            self.parallel_mode = False
            self.is_running = False

            # 释放各 worker 残留的已领取卡（异常路径可能漏掉 release_unused）。
            # 必须在下面 delete_pending_by_task 之前——否则残留的 processing
            # 会被当成未处理记录一并删掉，语义上没错但日志会少算。
            if task_id:
                try:
                    leaked = sum(card_binding_model.release_unused(task_id, w.worker_id)
                                 for w in self.workers.values())
                    if leaked:
                        self._hooked_print(f"已释放 {leaked} 张残留的已领取卡")
                except Exception as e:
                    self._hooked_print(f"释放残留卡失败: {e}")

            # 记录有效卡（本任务绑定成功的卡）+ 导出报告 + 结束任务记录
            if task_id:
                try:
                    success_records = self.db.fetchall(
                        "SELECT card_data_json, bound_to_email FROM card_bindings WHERE task_id=? AND status='success' AND card_data_json IS NOT NULL",
                        (task_id,),
                    )
                    valid_count = 0
                    for sr in success_records:
                        card_data = json.loads(sr['card_data_json'])
                        self.models['valid_card'].record(
                            card_data, source_type='bind',
                            source_email=sr['bound_to_email'] or '',
                            source_group_id=bind_group_id,
                        )
                        valid_count += 1
                    if valid_count > 0:
                        self._hooked_print(f"已记录 {valid_count} 张有效卡")
                except Exception as e:
                    self._hooked_print(f"记录有效卡失败: {e}")

                try:
                    records = card_binding_model.get_all_by_task(task_id)
                    report_path = card_service.export_report(records)
                    self._hooked_print(f"报告已导出: {report_path}")
                except Exception as e:
                    self._hooked_print(f"报告导出失败: {e}")

                try:
                    deleted = card_binding_model.delete_pending_by_task(task_id)
                    if deleted > 0:
                        self._hooked_print(f"已清理 {deleted} 条未处理记录")
                except Exception as e:
                    self._hooked_print(f"清理 pending 记录失败: {e}")

                final_summary = card_binding_model.get_summary(task_id)
                self.models['task'].update_counts(task_id, final_summary['success'], final_summary['failed'])
                self.models['task'].finish(task_id, 'completed' if not self.stop_requested else 'stopped')

            self.current_action = (
                f"每日流水线完成（补绑 {bind_success_total} 张 / "
                f"充值成功 {recharge_success_total} / 充值失败 {recharge_fail_total} / "
                f"仅账单处理 {recharge_invoice_only_total}）"
            )
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
        'task': TaskModel(db),
        'card_binding': CardBindingModel(db),
        'recharge_log': RechargeLogModel(db),
        'card_group': CardGroupModel(db),
        'card_pool': CardPoolModel(db),
        'valid_card': ValidCardModel(db),
        'card_state': CardPaymentStateModel(db),
        'invoice_state': InvoicePaymentStateModel(db),
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
