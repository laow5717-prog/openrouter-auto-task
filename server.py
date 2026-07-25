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

from src.web.app import create_app


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
            print(f"Web server (dev, auto-reload) started: http://localhost:{port}")
            make_server('0.0.0.0', port, app, threaded=True).serve_forever()

        run_with_reloader(serve)
        return

    # 生产模式：waitress，无热重载
    app = create_app()
    from waitress import serve
    print(f"Web server started: http://localhost:{port}")
    serve(app, host='0.0.0.0', port=port, threads=6)


if __name__ == '__main__':
    main()
