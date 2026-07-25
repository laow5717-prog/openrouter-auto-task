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


def main():
    app = create_app()

    from waitress import serve
    port = int(os.environ.get('PORT', '5000'))
    print(f"Web server started: http://localhost:{port}")
    serve(app, host='0.0.0.0', port=port, threads=6)


if __name__ == '__main__':
    main()
