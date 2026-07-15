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
from src.models.task import TaskModel
from src.models.card_binding import CardBindingModel
from src.models.recharge_log import RechargeLogModel
from src.models.card_group import CardGroupModel
from src.models.card_pool import CardPoolModel
from src.models.valid_card import ValidCardModel
from src.services import registration, card as card_service
from src.api.routes import api


class AppState:
    """全局应用状态（运行时内存数据）"""

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

        # MJPEG 流缓冲区
        self.last_frame = None
        self.frame_lock = threading.Lock()

        # 持续截图线程
        self._screenshot_driver = None
        self._screenshot_thread = None
        self._screenshot_stop = threading.Event()

        # 当前信用卡驱动任务 ID
        self.current_card_task_id = None

        # 当前活跃的自动化 driver（用于停止时强制关闭）
        self._active_driver = None
        self._active_driver_lock = threading.Lock()

        # 按账号独立跟踪的浏览器查看会话（不阻塞全局任务）
        self.open_browsers = set()

    def add_log(self, message):
        timestamp = datetime.now().strftime("%H:%M:%S")
        with self.lock:
            self.logs.append(f"[{timestamp}] {message}")
            if len(self.logs) > 1000:
                self.logs.pop(0)

    def get_logs(self, start_index=0):
        with self.lock:
            return list(self.logs[start_index:])

    def update_frame(self, frame_bytes):
        with self.frame_lock:
            self.last_frame = frame_bytes

    def get_frame(self):
        with self.frame_lock:
            return self.last_frame

    def _start_screenshot_loop(self, driver):
        """启动后台持续截图线程"""
        self._screenshot_driver = driver
        self._screenshot_stop.clear()
        if self._screenshot_thread and self._screenshot_thread.is_alive():
            return

        def _loop():
            while not self._screenshot_stop.is_set():
                try:
                    d = self._screenshot_driver
                    if d:
                        png = d.get_screenshot_as_png()
                        self.update_frame(png)
                except Exception:
                    pass
                self._screenshot_stop.wait(0.3)

        self._screenshot_thread = threading.Thread(target=_loop, daemon=True)
        self._screenshot_thread.start()

    def _stop_screenshot_loop(self):
        """停止后台截图线程"""
        self._screenshot_stop.set()
        self._screenshot_driver = None

    def set_active_driver(self, driver):
        with self._active_driver_lock:
            self._active_driver = driver

    def clear_active_driver(self):
        with self._active_driver_lock:
            self._active_driver = None

    def force_stop(self):
        """协作式停止：设置标志、停截图。不从本线程 quit driver。

        driver 由执行任务的工作线程持有；从请求线程跨线程 quit 会让工作线程里
        正在进行的 Patchright/Playwright sync 操作永久 hang（sync API 非线程安全，
        transport 被关后挂起的调用等不到响应）。改为设置 stop_requested，工作线程
        在各自的 should_stop / _monitor 检查点抛出中断、冒泡到其 finally 里 close_driver
        自行关闭浏览器。三条任务流程（register_one_account / register_and_bind_cards /
        recharge_account）都有 finally close_driver，故无需在此额外 quit。"""
        self.stop_requested = True
        self._stop_screenshot_loop()
        # 仅解除活跃 driver 引用，不 quit（quit 交给工作线程的 finally）
        self.clear_active_driver()
        self.add_log("已请求停止任务（工作线程将在下个检查点安全退出并关闭浏览器）")

    def _hooked_print(self, *args, **kwargs):
        sep = kwargs.get('sep', ' ')
        msg = sep.join(map(str, args))
        self.add_log(msg)
        # 同时输出到终端
        import builtins
        builtins.print(*args, **kwargs)

    def _monitor(self, driver, step):
        if self.stop_requested:
            self._hooked_print("收到停止请求，正在中断...")
            raise InterruptedError("User requested stop")
        # 跟踪活跃 driver
        self.set_active_driver(driver)
        # 首次调用时启动持续截图线程
        if self._screenshot_driver is not driver:
            self._start_screenshot_loop(driver)

    def run_batch_task(self, count, card_info_list, cf_password, max_bindable_cards, captcha_api_key):
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
                        cf_password=cf_password,
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

    def run_card_driven_task(self, cards, cf_password, max_bindable_cards, captcha_api_key, source_group_id=None):
        self.is_running = True
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "信用卡驱动模式启动中"
        self.update_frame(None)

        self._patch_prints()

        # 过滤已成功绑定过的卡，以及因 Stripe 字段错误失败的卡（数据本身有问题，重试无意义）
        card_binding_model = self.models['card_binding']
        already_bound_numbers = card_binding_model.get_successfully_bound_card_numbers()
        stripe_error_numbers = card_binding_model.get_stripe_field_error_card_numbers()
        filtered_cards = []
        skipped = 0
        skipped_stripe_err = 0
        for c in cards:
            if c.get('number') in already_bound_numbers:
                skipped += 1
            elif c.get('number') in stripe_error_numbers:
                skipped_stripe_err += 1
            else:
                filtered_cards.append(c)

        if skipped > 0:
            self._hooked_print(f"跳过 {skipped} 张已绑定的卡")
        if skipped_stripe_err > 0:
            self._hooked_print(f"跳过 {skipped_stripe_err} 张 Stripe 字段错误的卡（卡数据无效，无法绑定）")

        if not filtered_cards:
            self._hooked_print("所有卡已绑定，无需处理")
            self.is_running = False
            self.current_action = "所有卡已绑定"
            return

        # 创建任务和绑定记录
        task_id = self.models['task'].create('card_driven', config={
            'total_cards': len(filtered_cards), 'max_bindable': max_bindable_cards,
        })
        self.current_card_task_id = task_id
        binding_ids = card_binding_model.create_batch(task_id, filtered_cards)

        self._hooked_print(f"信用卡驱动模式: 共 {len(filtered_cards)} 张卡待处理")

        try:
            self._register_bind_loop(task_id, cf_password, max_bindable_cards, captcha_api_key)
        except Exception as e:
            self._hooked_print(f"严重错误: {e}")
        finally:
            self.clear_active_driver()
            self._stop_screenshot_loop()
            self.is_running = False
            final_summary = card_binding_model.get_summary(task_id)
            self.current_action = f"已完成 (成功 {final_summary['success']} / 失败 {final_summary['failed']})"
            self.models['task'].update_counts(task_id, final_summary['success'], final_summary['failed'])
            self.models['task'].finish(task_id, 'completed' if not self.stop_requested else 'stopped')
            self._hooked_print(f"任务完成 - 总计: {final_summary['total']}，成功: {final_summary['success']}，失败: {final_summary['failed']}")

            # 记录有效卡（绑定成功的卡）
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
                        source_group_id=source_group_id,
                    )
                    valid_count += 1
                if valid_count > 0:
                    self._hooked_print(f"已记录 {valid_count} 张有效卡")
            except Exception as e:
                self._hooked_print(f"记录有效卡失败: {e}")

            # 导出报告
            try:
                records = card_binding_model.get_all_by_task(task_id)
                report_path = card_service.export_report(records)
                self._hooked_print(f"报告已导出: {report_path}")
            except Exception as e:
                self._hooked_print(f"报告导出失败: {e}")

            # 清理本任务遗留的 pending 记录（未处理的卡不会再被此任务处理）
            try:
                deleted = card_binding_model.delete_pending_by_task(task_id)
                if deleted > 0:
                    self._hooked_print(f"已清理 {deleted} 条未处理记录")
            except Exception as e:
                self._hooked_print(f"清理 pending 记录失败: {e}")

    def _register_bind_loop(self, task_id, cf_password, max_bindable_cards, captcha_api_key):
        """注册新号 + 逐张绑卡的主循环。

        消耗 task_id 下的 pending 卡：每轮注册一个新账号并绑到 max_bindable_cards 张，
        剩余卡留给下一账号；连续失败达阈值或卡池空则结束。可被 run_card_driven_task
        与每日流水线的注册阶段复用。InterruptedError 会中断循环（视为用户停止）。"""
        card_binding_model = self.models['card_binding']
        account_index = 0
        consecutive_failures = 0
        max_consecutive_failures = 3

        while True:
            if self.stop_requested:
                self._hooked_print("用户停止了任务")
                break

            pending = card_binding_model.get_pending(task_id)
            if not pending:
                self._hooked_print("所有卡已处理完毕！")
                break

            if consecutive_failures >= max_consecutive_failures:
                self._hooked_print(f"连续失败达到 {max_consecutive_failures} 次，停止任务")
                break

            # 传所有 pending 卡，让注册函数尝试到绑够 max_bindable_cards 为止
            batch = pending
            account_index += 1

            summary = card_binding_model.get_summary(task_id)
            self.current_action = f"正在注册账号 {account_index} (剩余 {summary['pending']} 张卡)"

            self._hooked_print(f"\n{'=' * 50}")
            self._hooked_print(f"正在注册账号 {account_index}")
            self._hooked_print(f"   卡片: {', '.join('****' + r['card_display'] for r in batch)}")
            self._hooked_print(f"   进度: 成功 {summary['success']} / 失败 {summary['failed']} / 待处理 {summary['pending']}")
            self._hooked_print(f"{'=' * 50}")

            try:
                email, password, bound_count = registration.register_and_bind_cards(
                    db=self.db,
                    account_model=self.models['account'],
                    card_binding_model=card_binding_model,
                    task_id=task_id,
                    batch_records=batch,
                    cf_password=cf_password,
                    max_bindable_cards=max_bindable_cards,
                    captcha_api_key=captcha_api_key,
                    monitor_callback=self._monitor,
                )

                if email and bound_count > 0:
                    self.success_count += bound_count
                    consecutive_failures = 0
                    self._hooked_print(f"本轮绑定了 {bound_count} 张卡")
                elif not email:
                    consecutive_failures += 1
                    self._hooked_print(f"注册失败 ({consecutive_failures}/{max_consecutive_failures})，卡片保留待下个账号处理")
                else:
                    consecutive_failures += 1
                    # 注册成功但没绑上卡，从 DB 刷新计数
                    updated_summary = card_binding_model.get_summary(task_id)
                    self.fail_count = updated_summary['failed']
                    self.success_count = updated_summary['success']

            except InterruptedError:
                self._hooked_print("任务已中断")
                break
            except Exception as e:
                consecutive_failures += 1
                self._hooked_print(f"错误: {str(e)}")
                # 异常时不标记所有卡为 failed，留待下一轮重试
                updated_summary = card_binding_model.get_summary(task_id)
                self.fail_count = updated_summary['failed']
                self.success_count = updated_summary['success']

            # 间隔等待
            remaining = card_binding_model.get_pending(task_id)
            if remaining and not self.stop_requested:
                wait_time = random.randint(cfg.batch.interval_min, cfg.batch.interval_max)
                self._hooked_print(f"等待 {wait_time} 秒后注册下一个账号...")
                for _ in range(wait_time):
                    if self.stop_requested:
                        break
                    time.sleep(1)

    def _recharge_one_account(self, email, cf_password, payment_group_id=None):
        """充值单个账号并记账，返回 (result, err)。

        result 取值：
          - "success"      实际 Top-up 成功
          - "invoice_only" 未充值（今日已充），仅检查/处理了账单，预建 log 已删除，不计充值
          - "failed"       充值失败或异常

        只负责一个账号的 Top-up + 记账，不管理 is_running / 截图收尾（由调用方负责，
        以便批量场景复用）。InterruptedError 向上抛出，供批量循环感知停止。"""
        models = self.models

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

        try:
            success, err, responses, card_last4, outcome = registration.recharge_account(
                email, cf_password,
                recharge_log_model=models['recharge_log'],
                monitor_callback=self._monitor,
                skip_invoice=skip_invoice,
                payment_cards=payment_cards,
                valid_card_model=models['valid_card'],
                card_pool_model=models['card_pool'],
                account_model=models['account'],
                should_stop=lambda: self.stop_requested,
            )

            # 仅处理/检查了账单、未实际 Top-up：删除预建占位 log，避免误记为 $10 充值成功。
            # 账单支付本身的记账已由 recharge_account 内部（_on_invoice_paid/_on_invoice_failed）完成。
            if outcome == "invoice_only":
                models['recharge_log'].delete(log_id)
                self.current_action = f"{email} 未重复充值（已检查/处理账单）"
                self.add_log(f"{email} 今日已充值，已执行账单支付检查，未重复 Top-up")
                return "invoice_only", ''

            # 用页面提取的后四位匹配完整卡号
            matched_card = ''
            if card_last4:
                for c in cards:
                    if c.get('card_number', '').endswith(card_last4):
                        matched_card = c['card_number']
                        break
                if not matched_card:
                    matched_card = f'•••• {card_last4}'
            if matched_card:
                models['recharge_log'].update_card(log_id, matched_card)

            # 从 API 响应中提取 Top-up 结果
            topup_resp = None
            for resp in responses:
                url = resp.get('url', '')
                if 'topup' in url or 'payment_intents' in url:
                    topup_resp = resp
                    break

            if success and topup_resp:
                resp_data = topup_resp.get('data', {})
                http_status = topup_resp.get('status', 0)
                if isinstance(resp_data, dict) and resp_data.get('success') is True:
                    models['recharge_log'].mark_success(log_id, api_response=topup_resp)
                    self.current_action = f"{email} 充值成功"
                    self.add_log(f"{email} AI Credits 充值 $10 成功")
                    if matched_card and card_last4:
                        for c in cards:
                            if c.get('card_number', '').endswith(card_last4):
                                models['valid_card'].record(
                                    c, source_type='payment', source_email=email,
                                )
                                break
                    return "success", ''
                else:
                    # API 返回了错误（如 409 重复充值）
                    error_msg = ''
                    if isinstance(resp_data, dict):
                        errors = resp_data.get('errors', [])
                        if errors:
                            error_msg = errors[0].get('message', str(resp_data))
                        else:
                            error_msg = str(resp_data)
                    else:
                        error_msg = str(resp_data)
                    models['recharge_log'].mark_failed(log_id, error=f"[HTTP {http_status}] {error_msg}", api_response=topup_resp)
                    self.current_action = f"{email} 充值失败: {error_msg}"
                    self.add_log(f"{email} 充值失败: {error_msg}")
                    return "failed", error_msg
            elif success:
                # 点击成功但未捕获到 API 响应
                models['recharge_log'].mark_success(log_id)
                self.current_action = f"{email} 充值已提交（未捕获响应）"
                self.add_log(f"{email} AI Credits 充值 $10 已提交")
                return "success", ''
            else:
                models['recharge_log'].mark_failed(log_id, error=err)
                self.current_action = f"{email} 充值失败: {err}"
                self.add_log(f"{email} 充值失败: {err}")
                return "failed", err or 'recharge failed'
        except InterruptedError:
            models['recharge_log'].mark_failed(log_id, error='用户中断')
            raise
        except Exception as e:
            models['recharge_log'].mark_failed(log_id, error=str(e))
            self.current_action = f"充值异常: {e}"
            self.add_log(f"充值异常: {e}")
            return "failed", str(e)

    def run_daily_pipeline(self, bind_group_id, payment_group_id, cf_password,
                           max_bindable_cards, captcha_api_key):
        """每日一键流水线：补绑已有账号 → 注册新号 → 批量充值，串行跑在单个后台线程。

        三段共享同一个 daily task 的 pending 卡池（消耗顺序：补绑 → 注册）。全程持有
        is_running 锁，复用现有日志/截图/停止机制。补绑与充值均设连续失败阈值兜底。"""
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

        try:
            # ===== 阶段0：准备卡池 =====
            self._hooked_print(f"\n{'#' * 50}")
            self._hooked_print("每日流水线开始")
            self._hooked_print(f"{'#' * 50}")

            cards, unusable = self.models['card_pool'].get_usable_cards_as_list(bind_group_id)
            if unusable:
                self._hooked_print(f"绑卡分组已跳过 {len(unusable)} 张无效卡（过期/被拒）")

            # 过滤已成功绑定过的卡，以及 Stripe 字段错误的卡（数据无效，重试无意义）
            already_bound_numbers = card_binding_model.get_successfully_bound_card_numbers()
            stripe_error_numbers = card_binding_model.get_stripe_field_error_card_numbers()
            filtered_cards = []
            skipped = 0
            skipped_stripe_err = 0
            for c in cards:
                if c.get('number') in already_bound_numbers:
                    skipped += 1
                elif c.get('number') in stripe_error_numbers:
                    skipped_stripe_err += 1
                else:
                    filtered_cards.append(c)
            if skipped > 0:
                self._hooked_print(f"跳过 {skipped} 张已绑定的卡")
            if skipped_stripe_err > 0:
                self._hooked_print(f"跳过 {skipped_stripe_err} 张 Stripe 字段错误的卡")

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
            else:
                self._hooked_print("阶段0：无可用卡，跳过补绑/注册，仅执行充值阶段")

            # ===== 阶段1a：补绑已有账号 =====
            if task_id and not self.stop_requested:
                self._hooked_print(f"\n{'=' * 50}\n阶段1a：补绑已有账号\n{'=' * 50}")
                accts = account_model.get_all(order_desc=False)  # 按创建顺序（id 升序）
                emails = [a['email'] for a in accts]
                counts = card_binding_model.count_by_emails(emails)
                candidates = [
                    a for a in accts
                    if a.get('cf_password')
                    and (a.get('status') or '') != 'banned'
                    and counts.get(a['email'], 0) < max_bindable_cards
                ]
                self._hooked_print(f"补绑候选账号 {len(candidates)} 个")

                consecutive_failures = 0
                max_consecutive_failures = 3
                for acct in candidates:
                    if self.stop_requested:
                        self._hooked_print("用户停止了任务")
                        break
                    if consecutive_failures >= max_consecutive_failures:
                        self._hooked_print(f"补绑连续失败达到 {max_consecutive_failures} 次，停止补绑阶段")
                        break

                    pending = card_binding_model.get_pending(task_id)
                    if not pending:
                        self._hooked_print("卡池已空，结束补绑阶段")
                        break

                    email = acct['email']
                    self.current_action = f"补绑账号 {email}（剩余 {len(pending)} 张卡）"
                    self._hooked_print(f"\n补绑账号: {email}")

                    try:
                        bound_count, login_ok = registration.bind_cards_to_existing_account(
                            account_model=account_model,
                            card_binding_model=card_binding_model,
                            task_id=task_id,
                            email=email,
                            cf_password=acct['cf_password'],
                            batch_records=pending,
                            max_bindable_cards=max_bindable_cards,
                            captcha_api_key=captcha_api_key,
                            monitor_callback=self._monitor,
                        )
                        if bound_count > 0:
                            self.success_count += bound_count
                            bind_success_total += bound_count
                            consecutive_failures = 0
                            self._hooked_print(f"{email} 补绑了 {bound_count} 张卡")
                        elif not login_ok:
                            consecutive_failures += 1
                            self._hooked_print(f"{email} 登录失败（{consecutive_failures}/{max_consecutive_failures}），卡保留待下个账号")
                        else:
                            self._hooked_print(f"{email} 无需补绑或账单页异常")
                    except InterruptedError:
                        self._hooked_print("补绑阶段被中断")
                        break
                    except Exception as e:
                        consecutive_failures += 1
                        self._hooked_print(f"补绑 {email} 出错: {e}")

            # ===== 阶段1b：注册新号消耗剩余卡 =====
            if task_id and not self.stop_requested:
                remaining = card_binding_model.get_pending(task_id)
                if remaining:
                    self._hooked_print(f"\n{'=' * 50}\n阶段1b：注册新号（剩余 {len(remaining)} 张卡）\n{'=' * 50}")
                    self._register_bind_loop(task_id, cf_password, max_bindable_cards, captcha_api_key)
                else:
                    self._hooked_print("阶段1b：卡池已被补绑消耗完，无需注册新号")

            # ===== 阶段2：批量充值 =====
            if not self.stop_requested:
                self._hooked_print(f"\n{'=' * 50}\n阶段2：批量充值\n{'=' * 50}")
                accts_after = account_model.get_all(order_desc=False)
                emails_after = [a['email'] for a in accts_after]
                counts_after = card_binding_model.count_by_emails(emails_after)
                # 放行所有绑卡≥1 的账号：今日已充过的也要进入，由 recharge_account 内部
                # 决定是 Top-up 还是仅执行账单支付（Unpaid invoice）。不再用 has_today_record
                # 在此一刀切排除，否则绑卡账号的待付账单永远得不到处理。
                recharge_targets = [
                    a for a in accts_after
                    if a.get('cf_password')
                    and counts_after.get(a['email'], 0) >= 1
                ]
                self._hooked_print(f"充值候选账号 {len(recharge_targets)} 个")

                consecutive_failures = 0
                max_consecutive_failures = 3
                for acct in recharge_targets:
                    if self.stop_requested:
                        self._hooked_print("用户停止了任务")
                        break
                    if consecutive_failures >= max_consecutive_failures:
                        self._hooked_print(f"充值连续失败达到 {max_consecutive_failures} 次，停止充值阶段")
                        break

                    email = acct['email']
                    self.current_action = f"充值账号 {email}"
                    try:
                        result, _err = self._recharge_one_account(email, acct['cf_password'], payment_group_id)
                        if result == "success":
                            recharge_success_total += 1
                            consecutive_failures = 0
                        elif result == "invoice_only":
                            # 未实际充值，仅处理/检查了账单：不计成功也不计失败，不累加连续失败
                            recharge_invoice_only_total += 1
                            consecutive_failures = 0
                        else:
                            recharge_fail_total += 1
                            consecutive_failures += 1
                    except InterruptedError:
                        self._hooked_print("充值阶段被中断")
                        break
                    except Exception as e:
                        recharge_fail_total += 1
                        consecutive_failures += 1
                        self._hooked_print(f"充值 {email} 出错: {e}")

        except Exception as e:
            self._hooked_print(f"严重错误: {e}")
        finally:
            self.clear_active_driver()
            self._stop_screenshot_loop()
            self.is_running = False

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


def gen_frames(state):
    while True:
        frame = state.get_frame()
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

    # 初始化数据库（路径独立于程序目录，升级版本不丢数据）
    if db_path is None:
        db_path = str(get_data_dir() / "cloudflare_auto.db")
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
    }

    # 创建应用状态
    state = AppState(db, models)

    # 注入到 Flask app config
    app.config['DB'] = db
    app.config['MODELS'] = models
    app.config['APP_STATE'] = state

    # 注册蓝图
    app.register_blueprint(api)

    # 首页
    @app.route('/')
    def index():
        return send_from_directory(static_dir, 'index.html')

    # MJPEG 流
    @app.route('/video_feed')
    def video_feed():
        return Response(gen_frames(state),
                        mimetype='multipart/x-mixed-replace; boundary=frame')

    return app
