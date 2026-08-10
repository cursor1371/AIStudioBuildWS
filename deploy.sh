#!/usr/bin/env bash
#
# AIStudioBuildWS 一键安装部署脚本（VPS 非 Docker 环境）
#
# 功能：
#   1. 检测并安装系统依赖（浏览器运行库、xvfb、supervisord）
#   2. 创建 Python 虚拟环境并安装项目依赖
#   3. 下载 Camoufox 浏览器
#   4. 生成 supervisord 配置文件
#   5. 初始化目录结构和 .env 配置
#
# 用法：
#   chmod +x deploy.sh
#   ./deploy.sh
#
# 支持系统：Debian 12 / Ubuntu 22.04+
# 要求：root 或 sudo 权限（仅系统依赖安装阶段）

set -euo pipefail

# =====================================================================
# 颜色输出
# =====================================================================
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m' # No Color

info()    { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()    { echo -e "${YELLOW}[WARN]${NC} $*"; }
error()   { echo -e "${RED}[ERROR]${NC} $*"; }
section() { echo -e "\n${CYAN}━━━ $* ━━━${NC}"; }

# =====================================================================
# 基础检测
# =====================================================================
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
SUPERVISOR_CONF="${PROJECT_DIR}/supervisord.conf"
ENV_FILE="${PROJECT_DIR}/.env"

cd "${PROJECT_DIR}"

section "环境检测"

# 检测操作系统
if [ -f /etc/os-release ]; then
    . /etc/os-release
    info "操作系统: ${PRETTY_NAME:-$ID}"
else
    error "无法识别操作系统，此脚本仅支持 Debian/Ubuntu"
    exit 1
fi

# 检测包管理器
if ! command -v apt-get &>/dev/null; then
    error "未找到 apt-get，此脚本仅支持 Debian/Ubuntu 系列"
    exit 1
fi

# 检测 Python 版本
PYTHON_CMD=""
for cmd in python3.12 python3.11 python3; do
    if command -v "$cmd" &>/dev/null; then
        ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
        major=$(echo "$ver" | cut -d. -f1)
        minor=$(echo "$ver" | cut -d. -f2)
        if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
            PYTHON_CMD="$cmd"
            info "Python: $("$PYTHON_CMD" --version)"
            break
        fi
    fi
done

if [ -z "$PYTHON_CMD" ]; then
    warn "未找到 Python 3.11+，将尝试安装..."
fi

# =====================================================================
# 系统依赖安装
# =====================================================================
section "安装系统依赖"

# 判断是否需要 sudo
SUDO=""
if [ "$(id -u)" -ne 0 ]; then
    if command -v sudo &>/dev/null; then
        SUDO="sudo"
        info "使用 sudo 安装系统依赖"
    else
        error "非 root 用户且未安装 sudo，请以 root 用户运行"
        exit 1
    fi
fi

info "更新包列表..."
$SUDO apt-get update -qq

# 安装 Python（如果需要）
if [ -z "$PYTHON_CMD" ]; then
    info "安装 Python 3.11..."
    $SUDO apt-get install -y -qq python3.11 python3.11-venv python3.11-dev 2>/dev/null \
        || $SUDO apt-get install -y -qq python3 python3-venv python3-dev
    # 重新检测
    for cmd in python3.12 python3.11 python3; do
        if command -v "$cmd" &>/dev/null; then
            ver=$("$cmd" -c 'import sys; print(f"{sys.version_info.major}.{sys.version_info.minor}")')
            major=$(echo "$ver" | cut -d. -f1)
            minor=$(echo "$ver" | cut -d. -f2)
            if [ "$major" -ge 3 ] && [ "$minor" -ge 11 ]; then
                PYTHON_CMD="$cmd"
                break
            fi
        fi
    done
    if [ -z "$PYTHON_CMD" ]; then
        error "Python 3.11+ 安装失败，请手动安装后重试"
        exit 1
    fi
    info "Python: $("$PYTHON_CMD" --version)"
fi

# 安装浏览器运行依赖和 supervisord
info "安装浏览器运行库..."
$SUDO apt-get install -y -qq --no-install-recommends \
    libatk1.0-0 libatk-bridge2.0-0 libcups2 libdbus-1-3 libdrm2 libgbm1 libgtk-3-0 \
    libnspr4 libnss3 libx11-6 libx11-xcb1 libxcb1 libxcomposite1 libxdamage1 \
    libxext6 libxfixes3 libxrandr2 libxrender1 libxtst6 ca-certificates \
    fonts-liberation libasound2 libpangocairo-1.0-0 libpango-1.0-0 libu2f-udev xvfb

info "系统依赖安装完成"

# =====================================================================
# Python 虚拟环境
# =====================================================================
section "配置 Python 虚拟环境"

if [ -d "${VENV_DIR}" ]; then
    info "虚拟环境已存在: ${VENV_DIR}"
else
    info "创建虚拟环境..."
    "$PYTHON_CMD" -m venv "${VENV_DIR}"
    info "虚拟环境创建完成"
fi

# 激活虚拟环境
source "${VENV_DIR}/bin/activate"
info "Python 路径: $(which python)"

# 安装 Python 依赖
info "安装 Python 依赖包..."
pip install --quiet --upgrade pip
pip install --quiet -r "${PROJECT_DIR}/requirements.txt"
# 检测 supervisor：优先使用系统已有的，避免重复安装
info "检测 supervisor..."
SUPERVISORD_BIN=""
SUPERVISORCTL_BIN=""

# 优先检测系统级 supervisor（apt 安装的）
if command -v supervisord &>/dev/null && command -v supervisorctl &>/dev/null; then
    SUPERVISORD_BIN="$(command -v supervisord)"
    SUPERVISORCTL_BIN="$(command -v supervisorctl)"
    info "使用系统已安装的 supervisor: ${SUPERVISORD_BIN}"
# 其次检测 venv 内是否已安装
elif [ -x "${VENV_DIR}/bin/supervisord" ] && [ -x "${VENV_DIR}/bin/supervisorctl" ]; then
    SUPERVISORD_BIN="${VENV_DIR}/bin/supervisord"
    SUPERVISORCTL_BIN="${VENV_DIR}/bin/supervisorctl"
    info "使用 venv 内已安装的 supervisor: ${SUPERVISORD_BIN}"
else
    # 均不存在，在 venv 内安装（不影响系统环境）
    info "未检测到 supervisor，安装到项目虚拟环境中..."
    pip install --quiet supervisor
    SUPERVISORD_BIN="${VENV_DIR}/bin/supervisord"
    SUPERVISORCTL_BIN="${VENV_DIR}/bin/supervisorctl"
    info "supervisor 已安装到 venv: ${SUPERVISORD_BIN}"
fi

# 将检测到的路径写入项目内的路径配置文件，供 service.sh 读取
cat > "${PROJECT_DIR}/.supervisor_paths" << PATHS_EOF
SUPERVISORD_BIN="${SUPERVISORD_BIN}"
SUPERVISORCTL_BIN="${SUPERVISORCTL_BIN}"
PATHS_EOF
info "Python 依赖安装完成"

# =====================================================================
# 下载 Camoufox 浏览器
# =====================================================================
section "下载 Camoufox 浏览器"

if python -c "import camoufox; camoufox.Camoufox" &>/dev/null; then
    info "Camoufox 已安装，执行 fetch..."
fi
camoufox fetch
info "Camoufox 浏览器下载完成"

# =====================================================================
# 初始化目录
# =====================================================================
section "初始化项目目录"

mkdir -p "${PROJECT_DIR}/cookies"
mkdir -p "${PROJECT_DIR}/logs"
info "目录结构已就绪"

# =====================================================================
# .env 配置文件
# =====================================================================
section "配置文件"

if [ -f "${ENV_FILE}" ]; then
    info ".env 配置文件已存在，跳过创建"
else
    if [ -f "${PROJECT_DIR}/.env.example" ]; then
        cp "${PROJECT_DIR}/.env.example" "${ENV_FILE}"
        info "已从 .env.example 创建 .env，请编辑填入必要配置："
        warn "  nano ${ENV_FILE}"
    else
        warn ".env.example 不存在，请手动创建 .env 配置文件"
    fi
fi

# =====================================================================
# 生成 supervisord 配置
# =====================================================================
section "生成 supervisord 配置"

# 确定当前用户
RUN_USER="$(whoami)"

cat > "${SUPERVISOR_CONF}" << SUPERVISORD_EOF
; =====================================================================
; AIStudioBuildWS supervisord 配置
; 由 deploy.sh 自动生成
; =====================================================================

[supervisord]
logfile=${PROJECT_DIR}/logs/supervisord.log
logfile_maxbytes=10MB
logfile_backups=3
loglevel=info
pidfile=${PROJECT_DIR}/supervisord.pid
nodaemon=false
directory=${PROJECT_DIR}

[unix_http_server]
file=${PROJECT_DIR}/supervisor.sock

[supervisorctl]
serverurl=unix://${PROJECT_DIR}/supervisor.sock

[rpcinterface:supervisor]
supervisor.rpcinterface_factory = supervisor.rpcinterface:make_main_rpcinterface

[program:aistudio]
command=${VENV_DIR}/bin/python main.py
directory=${PROJECT_DIR}
user=${RUN_USER}
autostart=true
autorestart=true
startretries=5
startsecs=10
stopwaitsecs=20
stopsignal=TERM
redirect_stderr=true
stdout_logfile=${PROJECT_DIR}/logs/supervisor_stdout.log
stdout_logfile_maxbytes=10MB
stdout_logfile_backups=3
environment=MALLOC_ARENA_MAX="2",PYTHONDONTWRITEBYTECODE="1",PYTHONUNBUFFERED="1"
SUPERVISORD_EOF

info "supervisord 配置已生成: ${SUPERVISOR_CONF}"

# =====================================================================
# 完成
# =====================================================================
section "部署完成"

echo ""
info "部署目录: ${PROJECT_DIR}"
info "虚拟环境: ${VENV_DIR}"
info "配置文件: ${ENV_FILE}"
info "supervisord: ${SUPERVISOR_CONF}"
echo ""
warn "下一步操作："
echo "  1. 编辑 .env 文件，填入必要配置（CAMOUFOX_INSTANCE_URL、Cookie 等）"
echo "     nano ${ENV_FILE}"
echo ""
echo "  2. 将 Cookie 文件放入 cookies/ 目录，或配置环境变量/远程 Cookie"
echo ""
echo "  3. 使用管理脚本启动服务："
echo "     ./service.sh start     # 启动"
echo "     ./service.sh status    # 查看状态"
echo "     ./service.sh logs      # 查看日志"
echo "     ./service.sh stop      # 停止"
echo "     ./service.sh restart   # 重启"
echo ""
