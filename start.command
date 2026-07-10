#!/bin/bash
# ==========================================
# Cloudflare Auto Task - 一键启动器
# 双击此文件即可启动程序
# ==========================================

cd "$(dirname "$0")"
clear

echo "============================================"
echo "   Cloudflare Auto Task"
echo "   正在启动，请稍候..."
echo "============================================"
echo ""

# ---------- 检查 Python 3 ----------
if ! command -v python3 &>/dev/null; then
    echo "[错误] 未检测到 Python 3，请先安装："
    echo "  方法1: 访问 https://www.python.org/downloads/ 下载安装"
    echo "  方法2: brew install python3"
    echo ""
    echo "安装完成后重新双击此文件即可。"
    echo ""
    read -n1 -rsp "按任意键关闭..."
    exit 1
fi

PYTHON=python3
echo "[OK] Python: $($PYTHON --version)"

# ---------- 检查 Google Chrome ----------
if [ ! -d "/Applications/Google Chrome.app" ]; then
    echo ""
    echo "[错误] 未检测到 Google Chrome 浏览器"
    echo "  请先安装: https://www.google.com/chrome/"
    echo ""
    read -n1 -rsp "按任意键关闭..."
    exit 1
fi
echo "[OK] Google Chrome 已安装"

# ---------- 首次运行：创建虚拟环境并安装依赖 ----------
if [ ! -d ".venv" ]; then
    echo ""
    echo "[首次运行] 正在创建虚拟环境..."
    $PYTHON -m venv .venv
fi

source .venv/bin/activate

# 检查依赖是否已安装
if ! $PYTHON -c "import flask, selenium, selenium_stealth" &>/dev/null; then
    echo "[安装] 正在安装依赖包（仅首次需要）..."
    pip install --quiet --upgrade pip
    pip install --quiet flask pyyaml requests selenium selenium-stealth waitress webdriver-manager faker
    echo "[OK] 依赖安装完成"
fi

# ---------- 创建默认配置文件 ----------
if [ ! -f "config.yaml" ]; then
    echo "[配置] 正在生成默认配置文件..."
    cp config.example.yaml config.yaml
    echo "[OK] 已生成 config.yaml"
fi

# ---------- 启动服务 ----------
echo ""
echo "============================================"
echo "   启动成功！"
echo "   浏览器将自动打开控制台"
echo ""
echo "   如未自动打开，请手动访问："
echo "   http://localhost:5000"
echo ""
echo "   关闭此窗口即可停止程序"
echo "============================================"
echo ""

# 延迟 1 秒后打开浏览器
(sleep 1 && open "http://localhost:5000") &

# 启动 Web 服务
$PYTHON server.py
