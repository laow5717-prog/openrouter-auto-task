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

from src.config import cfg, get_base_dir
from src.models.database import Database
from src.models.account import AccountModel
from src.models.task import TaskModel
from src.models.card_binding import CardBindingModel
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

        # 当前信用卡驱动任务 ID
        self.current_card_task_id = None

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
        try:
            png_bytes = driver.get_screenshot_as_png()
            self.update_frame(png_bytes)
        except Exception:
            pass

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
            self.is_running = False
            self.current_action = "任务已完成"
            self.models['task'].update_counts(task_id, self.success_count, self.fail_count)
            self.models['task'].finish(task_id, 'completed' if not self.stop_requested else 'stopped')
            self._hooked_print("任务完成")

    def run_card_driven_task(self, cards, cf_password, max_bindable_cards, captcha_api_key):
        self.is_running = True
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "信用卡驱动模式启动中"
        self.update_frame(None)

        self._patch_prints()

        # 过滤已成功绑定过的卡
        card_binding_model = self.models['card_binding']
        already_bound_numbers = card_binding_model.get_successfully_bound_card_numbers()
        filtered_cards = []
        skipped = 0
        for c in cards:
            if c.get('number') in already_bound_numbers:
                skipped += 1
            else:
                filtered_cards.append(c)

        if skipped > 0:
            self._hooked_print(f"跳过 {skipped} 张已绑定的卡")

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

        account_index = 0
        consecutive_failures = 0
        max_consecutive_failures = 3

        try:
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

        except Exception as e:
            self._hooked_print(f"严重错误: {e}")
        finally:
            self.is_running = False
            final_summary = card_binding_model.get_summary(task_id)
            self.current_action = f"已完成 (成功 {final_summary['success']} / 失败 {final_summary['failed']})"
            self.models['task'].update_counts(task_id, final_summary['success'], final_summary['failed'])
            self.models['task'].finish(task_id, 'completed' if not self.stop_requested else 'stopped')
            self._hooked_print(f"任务完成 - 总计: {final_summary['total']}，成功: {final_summary['success']}，失败: {final_summary['failed']}")

            # 导出报告
            try:
                records = card_binding_model.get_all_by_task(task_id)
                report_path = card_service.export_report(records)
                self._hooked_print(f"报告已导出: {report_path}")
            except Exception as e:
                self._hooked_print(f"报告导出失败: {e}")

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
        time.sleep(0.5)


def create_app(db_path=None):
    """Flask 应用工厂"""
    base_dir = get_base_dir()

    # 静态文件目录
    if getattr(sys, 'frozen', False):
        static_dir = os.path.join(sys._MEIPASS, 'static')
    else:
        static_dir = str(base_dir / 'static')

    app = Flask(__name__, static_url_path='', static_folder=static_dir)

    # 初始化数据库
    if db_path is None:
        db_path = str(base_dir / cfg.database.path)
    db = Database(db_path)

    # 创建模型
    models = {
        'account': AccountModel(db),
        'task': TaskModel(db),
        'card_binding': CardBindingModel(db),
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
