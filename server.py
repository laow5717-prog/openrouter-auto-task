"""
Cloudflare 自动注册工具 - Web 服务端
提供可视化控制台，支持实时监控浏览器操作
"""

import platform
import threading
import time
import builtins
import os
import random

# Windows 环境强制 UTF-8 输出
if platform.system() == 'Windows':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    try:
        import sys as _sys
        _sys.stdout.reconfigure(encoding='utf-8')
        _sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass
from datetime import datetime
from flask import Flask, jsonify, request, send_from_directory

# 导入业务逻辑
import main
import browser
import email_service
from config import cfg

import sys

# PyInstaller 打包后的资源路径处理
if getattr(sys, 'frozen', False):
    BASE_DIR = os.path.dirname(sys.executable)
    STATIC_DIR = os.path.join(sys._MEIPASS, 'static')
else:
    BASE_DIR = os.path.dirname(__file__)
    STATIC_DIR = os.path.join(BASE_DIR, 'static')

app = Flask(__name__, static_url_path='', static_folder=STATIC_DIR)


# ==========================================
# 状态管理与日志捕获
# ==========================================

class AppState:
    """全局应用状态"""
    def __init__(self):
        self.is_running = False
        self.stop_requested = False
        self.success_count = 0
        self.fail_count = 0
        self.current_action = "等待启动"
        self.logs = []
        self.lock = threading.Lock()

        # MJPEG 流缓冲区
        self.last_frame = None
        self.frame_lock = threading.Lock()

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


state = AppState()

# 劫持 print 函数以捕获日志
original_print = builtins.print


def hooked_print(*args, **kwargs):
    sep = kwargs.get('sep', ' ')
    msg = sep.join(map(str, args))
    state.add_log(msg)
    original_print(*args, **kwargs)


# 应用劫持
main.print = hooked_print
browser.print = hooked_print
email_service.print = hooked_print


# ==========================================
# 后台工作线程
# ==========================================

def worker_thread(count, card_info=None, cf_password=None):
    """后台执行注册任务的工作线程"""
    state.is_running = True
    state.stop_requested = False
    state.success_count = 0
    state.fail_count = 0
    state.current_action = f"🚀 任务启动，目标: {count}"

    # 清空上一轮的画面
    state.update_frame(None)

    main.print(f"🚀 开始批量任务，计划注册: {count} 个")

    try:
        def monitor(driver, step):
            # 检查是否请求停止
            if state.stop_requested:
                main.print("🛑 检测到停止请求，正在中断任务...")
                raise InterruptedError("用户请求停止")

            # 截图更新流 (MJPEG)
            try:
                png_bytes = driver.get_screenshot_as_png()
                state.update_frame(png_bytes)
            except Exception as e:
                main.print(f"⚠️ 截图流更新失败: {e}")

        for i in range(count):
            if state.stop_requested:
                main.print("🛑 用户停止了任务")
                break

            state.current_action = f"正在注册 ({i+1}/{count})..."

            try:
                email, password, success = main.register_one_account(
                    card_info=card_info,
                    cf_password=cf_password,
                    monitor_callback=monitor,
                )

                if success:
                    state.success_count += 1
                else:
                    state.fail_count += 1
            except InterruptedError:
                main.print("🛑 任务已中断")
                break
            except Exception as e:
                state.fail_count += 1
                main.print(f"❌ 异常: {str(e)}")

            # 间隔等待
            if i < count - 1 and not state.stop_requested:
                wait_time = random.randint(cfg.batch.interval_min, cfg.batch.interval_max)
                main.print(f"⏳ 冷却中，等待 {wait_time} 秒...")
                for _ in range(wait_time):
                    if state.stop_requested:
                        break
                    time.sleep(1)

    except Exception as e:
        main.print(f"💥 严重错误: {e}")
    finally:
        state.is_running = False
        state.current_action = "任务已完成"
        main.print("🏁 任务结束")


# ==========================================
# MJPEG 流生成器
# ==========================================

def gen_frames():
    """生成 MJPEG 流数据"""
    while True:
        frame = state.get_frame()
        if frame:
            yield (b'--frame\r\n'
                   b'Content-Type: image/png\r\n\r\n' + frame + b'\r\n')
        time.sleep(0.5)


@app.route('/video_feed')
def video_feed():
    return Flask.response_class(gen_frames(),
                                mimetype='multipart/x-mixed-replace; boundary=frame')


# ==========================================
# API 接口
# ==========================================

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')


@app.route('/api/status')
def get_status():
    """获取当前任务状态"""
    total_inventory = 0
    accounts_path = os.path.join(BASE_DIR, cfg.files.accounts_file)
    if os.path.exists(accounts_path):
        try:
            with open(accounts_path, 'r', encoding='utf-8') as f:
                total_inventory = sum(1 for line in f if '@' in line)
        except Exception:
            pass

    return jsonify({
        "is_running": state.is_running,
        "current_action": state.current_action,
        "success": state.success_count,
        "fail": state.fail_count,
        "total_inventory": total_inventory,
        "logs": state.get_logs(int(request.args.get('log_index', 0))),
    })


@app.route('/api/start', methods=['POST'])
def start_task():
    """启动注册任务"""
    if state.is_running:
        return jsonify({"error": "任务已在运行中"}), 400

    data = request.json or {}
    count = data.get('count', 1)

    # 从请求中获取信用卡信息和自定义密码
    card_info = data.get('card_info', None)
    cf_password = data.get('cf_password', None)

    threading.Thread(
        target=worker_thread,
        args=(count, card_info, cf_password),
        daemon=True,
    ).start()

    return jsonify({"status": "started"})


@app.route('/api/stop', methods=['POST'])
def stop_task():
    """停止当前任务"""
    if not state.is_running:
        return jsonify({"error": "没有正在运行的任务"}), 400

    state.stop_requested = True
    return jsonify({"status": "stopping"})


@app.route('/api/accounts')
def get_accounts():
    """获取已注册账号列表"""
    accounts = []
    accounts_path = os.path.join(BASE_DIR, cfg.files.accounts_file)
    if os.path.exists(accounts_path):
        try:
            with open(accounts_path, 'r', encoding='utf-8') as f:
                for line in f:
                    parts = line.strip().split('----')
                    if len(parts) >= 2:
                        accounts.append({
                            "email": parts[0].strip(),
                            "password": parts[1].strip(),
                            "status": parts[3].strip() if len(parts) > 3 else "",
                            "time": parts[2].strip() if len(parts) > 2 else "",
                            "email_password": parts[4].strip() if len(parts) > 4 else "",
                        })
        except Exception as e:
            return jsonify({"error": str(e)}), 500

    # 最新的在前
    return jsonify(accounts[::-1])


if __name__ == '__main__':
    from waitress import serve
    print("🌐 Web 服务已启动: http://localhost:5000")
    serve(app, host='0.0.0.0', port=5000, threads=6)
