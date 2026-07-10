"""
打包脚本 - 将项目打包为独立可执行文件
用户无需安装 Python 环境，双击即可运行

用法:
    python build.py          # 打包当前平台
    python build.py --clean  # 清理后重新打包
"""

import subprocess
import sys
import os
import shutil
import platform
from pathlib import Path

# Windows 环境强制 UTF-8 输出
if platform.system() == 'Windows':
    os.environ['PYTHONIOENCODING'] = 'utf-8'
    try:
        sys.stdout.reconfigure(encoding='utf-8')
        sys.stderr.reconfigure(encoding='utf-8')
    except Exception:
        pass

BASE_DIR = Path(__file__).parent

def clean():
    for d in ['build', 'dist']:
        p = BASE_DIR / d
        if p.exists():
            shutil.rmtree(p)
            print(f"  已清理 {d}/")
    spec = BASE_DIR / 'CloudflareAutoTask.spec'
    if spec.exists():
        spec.unlink()


def build():
    system = platform.system()
    print(f"当前系统: {system} ({platform.machine()})")
    print("=" * 50)

    # 清理旧文件
    clean()

    # 路径分隔符: macOS/Linux 用 ":", Windows 用 ";"
    sep = ';' if system == 'Windows' else ':'

    # PyInstaller 参数
    args = [
        sys.executable, '-m', 'PyInstaller',
        '--name', 'CloudflareAutoTask',
        '--onedir',
        '--console',
        # 添加静态资源
        '--add-data', f'static{sep}static',
        '--add-data', f'config.example.yaml{sep}.',
        # 隐式导入
        '--collect-submodules', 'src',
        '--hidden-import', 'selenium_stealth',
        '--hidden-import', 'webdriver_manager',
        '--hidden-import', 'selenium.webdriver',
        '--hidden-import', 'selenium.webdriver.chrome',
        '--hidden-import', 'selenium.webdriver.chrome.webdriver',
        '--hidden-import', 'selenium.webdriver.chrome.service',
        '--hidden-import', 'selenium.webdriver.chrome.options',
        '--hidden-import', 'selenium.webdriver.common',
        '--hidden-import', 'selenium.webdriver.common.by',
        '--hidden-import', 'selenium.webdriver.common.keys',
        '--hidden-import', 'selenium.webdriver.common.action_chains',
        '--hidden-import', 'selenium.webdriver.common.selenium_manager',
        '--hidden-import', 'selenium.webdriver.support',
        '--hidden-import', 'selenium.webdriver.support.ui',
        '--hidden-import', 'selenium.webdriver.support.expected_conditions',
        '--hidden-import', 'selenium.webdriver.remote',
        '--hidden-import', 'selenium.webdriver.remote.webdriver',
        '--hidden-import', 'selenium.webdriver.remote.webelement',
        '--collect-submodules', 'selenium',
        '--collect-submodules', 'selenium_stealth',
        '--collect-submodules', 'webdriver_manager',
        # 不确认覆盖
        '--noconfirm',
        # 入口
        'server.py',
    ]

    # macOS 特殊处理
    if system == 'Darwin':
        args.insert(-1, '--icon')
        args.insert(-1, 'NONE')

    print("正在打包，请耐心等待...")
    print(f"命令: {' '.join(args[2:])}")
    print()

    result = subprocess.run(args, cwd=str(BASE_DIR))

    if result.returncode != 0:
        print("\n打包失败!")
        sys.exit(1)

    # 复制配置文件模板到输出目录
    dist_dir = BASE_DIR / 'dist' / 'CloudflareAutoTask'
    if dist_dir.exists():
        shutil.copy2(
            BASE_DIR / 'config.example.yaml',
            dist_dir / 'config.example.yaml',
        )

    # 创建平台对应的启动器
    if system == 'Darwin':
        _create_mac_launcher(dist_dir)
    elif system == 'Windows':
        _create_win_launcher(dist_dir)

    print()
    print("=" * 50)
    print("打包成功!")
    print(f"输出目录: {dist_dir}")
    print()
    print("交付方式: 将 dist/CloudflareAutoTask 文件夹压缩为 zip 发送给客户")
    if system == 'Darwin':
        print("客户双击 '启动.command' 即可运行")
    else:
        print("客户双击 '启动.bat' 即可运行")
    print("=" * 50)


def _create_mac_launcher(dist_dir):
    launcher = dist_dir / '启动.command'
    launcher.write_text("""\
#!/bin/bash
cd "$(dirname "$0")"
clear

if [ ! -d "/Applications/Google Chrome.app" ]; then
    echo ""
    echo "[错误] 请先安装 Google Chrome 浏览器"
    echo "  下载地址: https://www.google.com/chrome/"
    echo ""
    read -n1 -rsp "按任意键关闭..."
    exit 1
fi

if [ ! -f "config.yaml" ]; then
    cp config.example.yaml config.yaml 2>/dev/null
fi

echo "============================================"
echo "   Cloudflare Auto Task"
echo "   正在启动..."
echo ""
echo "   浏览器将自动打开控制台"
echo "   如未打开请访问: http://localhost:5000"
echo ""
echo "   关闭此窗口即可停止程序"
echo "============================================"

(sleep 2 && open "http://localhost:5000") &
./CloudflareAutoTask
""", encoding='utf-8')
    launcher.chmod(0o755)
    print(f"  已创建启动器: {launcher.name}")


def _create_win_launcher(dist_dir):
    launcher = dist_dir / '启动.bat'
    launcher.write_text("""\
@echo off
chcp 65001 >nul 2>&1
title Cloudflare Auto Task
cd /d "%~dp0"

if not exist "config.yaml" (
    if exist "config.example.yaml" copy /y config.example.yaml config.yaml >nul
)

echo ============================================
echo    Cloudflare Auto Task
echo    正在启动...
echo.
echo    浏览器将自动打开控制台
echo    如未打开请访问: http://localhost:5000
echo.
echo    关闭此窗口即可停止程序
echo ============================================

start "" cmd /c "timeout /t 2 /nobreak >nul & start http://localhost:5000"
CloudflareAutoTask.exe

echo.
echo 程序已停止
pause
""", encoding='utf-8')
    print(f"  已创建启动器: {launcher.name}")


if __name__ == '__main__':
    if '--clean' in sys.argv:
        print("清理旧文件...")
        clean()
    build()
