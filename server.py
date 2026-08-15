"""
OpenRouter Auto Task - Web Server Entry Point
"""

import platform
import os

# Windows UTF-8 handling
if platform.system() == 'Windows':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    try:
        import sys
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

from src.web.app import create_app, shutdown_runtime


def _install_shutdown_hook(app):
    """Ctrl+C / kill 时先关掉本次开过的 AdsPower 环境，再退出。

    为什么非得拦信号：默认的 SIGTERM 处理是立刻终止进程，AdsPower 云端的占用记录
    就此停在「打开中」，那个环境下次 start 会被直接拒——理由与代价见
    app.shutdown_runtime 的 docstring。

    收尾完用 os._exit 而不是 sys.exit：后者靠抛 SystemExit 退出，而这个 handler 可能
    在任意线程栈上执行，异常未必能一路冒到 serve() 外面。所有工作线程都是 daemon，
    没有需要等待的收尾，直接退干净即可。

    第二次信号强退：收尾要挨个 stop 环境，受 AdsPower 接口限流（约 0.55 秒/次）拖着，
    十来个环境就是几秒。用户等不及再按一次 Ctrl+C 时，应该立刻走人而不是被忽略。
    """
    import signal

    leaving = {'yes': False}

    def _bye(signum, _frame):
        if leaving['yes']:
            os._exit(1)
        leaving['yes'] = True
        print(f"\n收到信号 {signum}，正在收尾（关闭 AdsPower 环境）…")
        try:
            shutdown_runtime(app)
        finally:
            print("已退出")
            os._exit(0)

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _bye)
        except (ValueError, OSError):
            pass       # 非主线程注册不了；那种情况下退回默认行为即可


def _dev_enabled():
    """开发模式开关：环境变量 DEV / FLASK_DEBUG 取真值时启用代码热重载。"""
    return (os.environ.get('DEV') or os.environ.get('FLASK_DEBUG') or '').strip().lower() \
        in ('1', 'true', 'yes', 'on')


def main():
    port = int(os.environ.get('PORT', '5000'))

    if _dev_enabled():
        # 开发模式：werkzeug 文件监视热重载。它会把本模块跑成两层进程——
        # 监视源码变化的「监督进程」和真正处理请求的「工作进程」(WERKZEUG_RUN_MAIN=true)。
        # create_app 会起后台 ClaimReaper 线程，故把它放进只在工作进程执行的 serve() 回调里，
        # 避免监督进程里重复起一个 reaper 抢同一个数据库。
        from werkzeug.serving import make_server
        from werkzeug._reloader import run_with_reloader

        def serve():
            app = create_app()
            _install_shutdown_hook(app)
            print(f"Web server (dev, auto-reload) started: http://localhost:{port}")
            make_server('0.0.0.0', port, app, threaded=True).serve_forever()

        run_with_reloader(serve)
        return

    # 生产模式：waitress，无热重载
    app = create_app()
    _install_shutdown_hook(app)
    from waitress import serve
    print(f"Web server started: http://localhost:{port}")
    serve(app, host='0.0.0.0', port=port, threads=6)


if __name__ == '__main__':
    main()
