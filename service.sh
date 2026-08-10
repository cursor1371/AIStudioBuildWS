#!/usr/bin/env bash
#
# AIStudioBuildWS 服务管理脚本
#
# 用法：
#   ./service.sh start      启动服务
#   ./service.sh stop       停止服务
#   ./service.sh restart    重启服务
#   ./service.sh status     查看服务状态
#   ./service.sh logs       实时查看应用日志
#   ./service.sh slog       查看 supervisord 管理日志
#   ./service.sh update     更新代码并重启
#   ./service.sh uninstall  停止服务并清理 supervisord

set -euo pipefail

# =====================================================================
# 路径与颜色
# =====================================================================
PROJECT_DIR="$(cd "$(dirname "$0")" && pwd)"
VENV_DIR="${PROJECT_DIR}/venv"
SUPERVISOR_CONF="${PROJECT_DIR}/supervisord.conf"
SUPERVISOR_PID="${PROJECT_DIR}/supervisord.pid"
SUPERVISOR_SOCK="${PROJECT_DIR}/supervisor.sock"
APP_LOG="${PROJECT_DIR}/logs/app.log"
SUPERVISOR_STDOUT="${PROJECT_DIR}/logs/supervisor_stdout.log"
SUPERVISORD_LOG="${PROJECT_DIR}/logs/supervisord.log"
# 从 deploy.sh 生成的路径配置中读取 supervisor 实际位置
# 支持系统级 supervisor 和 venv 级 supervisor 两种来源
SUPERVISOR_PATHS="${PROJECT_DIR}/.supervisor_paths"
if [ -f "${SUPERVISOR_PATHS}" ]; then
    source "${SUPERVISOR_PATHS}"
else
    # 路径文件不存在时按优先级自动检测
    if command -v supervisord &>/dev/null; then
        SUPERVISORD_BIN="$(command -v supervisord)"
        SUPERVISORCTL_BIN="$(command -v supervisorctl)"
    elif [ -x "${VENV_DIR}/bin/supervisord" ]; then
        SUPERVISORD_BIN="${VENV_DIR}/bin/supervisord"
        SUPERVISORCTL_BIN="${VENV_DIR}/bin/supervisorctl"
    else
        SUPERVISORD_BIN=""
        SUPERVISORCTL_BIN=""
    fi
fi
RED='\033[0;31m'
GREEN='\033[0;32m'
YELLOW='\033[1;33m'
CYAN='\033[0;36m'
NC='\033[0m'

info()  { echo -e "${GREEN}[INFO]${NC} $*"; }
warn()  { echo -e "${YELLOW}[WARN]${NC} $*"; }
error() { echo -e "${RED}[ERROR]${NC} $*"; }

# =====================================================================
# 前置检查
# =====================================================================
check_deploy() {
    if [ ! -d "${VENV_DIR}" ]; then
        error "虚拟环境不存在，请先运行 ./deploy.sh"
        exit 1
    fi
    if [ ! -f "${SUPERVISOR_CONF}" ]; then
        error "supervisord 配置不存在，请先运行 ./deploy.sh"
        exit 1
    fi
    if [ ! -x "${SUPERVISORD_BIN}" ]; then
        error "supervisord 未安装，请先运行 ./deploy.sh"
        exit 1
    fi
}

# 检查 supervisord 是否正在运行
is_supervisord_running() {
    if [ -f "${SUPERVISOR_PID}" ]; then
        local pid
        pid=$(cat "${SUPERVISOR_PID}" 2>/dev/null || echo "")
        if [ -n "$pid" ] && kill -0 "$pid" 2>/dev/null; then
            return 0
        fi
    fi
    return 1
}

# supervisorctl 快捷方式
sctl() {
    "${SUPERVISORCTL_BIN}" -c "${SUPERVISOR_CONF}" "$@"
}

# =====================================================================
# 命令实现
# =====================================================================
cmd_start() {
    check_deploy

    if is_supervisord_running; then
        info "supervisord 已在运行"
        sctl status
        return
    fi

    info "启动 supervisord..."
    "${SUPERVISORD_BIN}" -c "${SUPERVISOR_CONF}"

    # 等待启动
    sleep 2

    if is_supervisord_running; then
        info "supervisord 启动成功"
        sctl status
    else
        error "supervisord 启动失败，请检查日志:"
        echo "  cat ${SUPERVISORD_LOG}"
        exit 1
    fi
}

cmd_stop() {
    if ! is_supervisord_running; then
        info "supervisord 未在运行"
        return
    fi

    info "停止服务..."
    sctl stop aistudio 2>/dev/null || true

    info "关闭 supervisord..."
    "${SUPERVISORCTL_BIN}" -c "${SUPERVISOR_CONF}" shutdown 2>/dev/null || true

    # 等待进程退出
    local wait_count=0
    while is_supervisord_running && [ $wait_count -lt 15 ]; do
        sleep 1
        wait_count=$((wait_count + 1))
    done

    if is_supervisord_running; then
        warn "supervisord 未正常退出，发送 SIGKILL..."
        local pid
        pid=$(cat "${SUPERVISOR_PID}" 2>/dev/null || echo "")
        if [ -n "$pid" ]; then
            kill -9 "$pid" 2>/dev/null || true
        fi
    fi

    # 清理残留文件
    rm -f "${SUPERVISOR_PID}" "${SUPERVISOR_SOCK}"

    info "服务已停止"
}

cmd_restart() {
    info "重启服务..."
    if is_supervisord_running; then
        info "重启应用进程（保持 supervisord 运行）..."
        sctl restart aistudio
        sleep 2
        sctl status
    else
        cmd_start
    fi
}

cmd_status() {
    if ! is_supervisord_running; then
        warn "supervisord 未在运行"
        return
    fi
    echo ""
    sctl status
    echo ""

    # 显示资源占用
    local pid
    pid=$(sctl pid aistudio 2>/dev/null || echo "0")
    if [ "$pid" != "0" ] && [ -n "$pid" ]; then
        info "进程 PID: ${pid}"
        # 获取进程树的总内存
        if command -v pmap &>/dev/null; then
            local rss
            rss=$(ps --no-headers -o rss -p "$pid" 2>/dev/null || echo "0")
            if [ "$rss" != "0" ]; then
                local rss_mb=$((rss / 1024))
                info "主进程 RSS: ${rss_mb} MB"
            fi
        fi
        # 统计子进程数
        local children
        children=$(pgrep -P "$pid" 2>/dev/null | wc -l || echo "0")
        info "子进程数: ${children}"
    fi
}

cmd_logs() {
    if [ ! -f "${APP_LOG}" ]; then
        warn "日志文件不存在: ${APP_LOG}"
        if [ -f "${SUPERVISOR_STDOUT}" ]; then
            info "使用 supervisor stdout 日志替代..."
            tail -f "${SUPERVISOR_STDOUT}"
        fi
        return
    fi
    info "实时查看应用日志 (Ctrl+C 退出)..."
    tail -f "${APP_LOG}"
}

cmd_slog() {
    if [ ! -f "${SUPERVISORD_LOG}" ]; then
        warn "supervisord 日志不存在: ${SUPERVISORD_LOG}"
        return
    fi
    info "查看 supervisord 管理日志 (最近 50 行)..."
    tail -50 "${SUPERVISORD_LOG}"
}

cmd_update() {
    check_deploy
    info "更新项目代码..."

    # 检查是否是 git 仓库
    if [ -d "${PROJECT_DIR}/.git" ]; then
        cd "${PROJECT_DIR}"
        git pull
        info "代码已更新"
    else
        warn "非 git 仓库，请手动更新代码"
    fi

    # 更新 Python 依赖
    info "更新 Python 依赖..."
    source "${VENV_DIR}/bin/activate"
    pip install --quiet -r "${PROJECT_DIR}/requirements.txt"

    # 更新 Camoufox
    info "检查 Camoufox 更新..."
    camoufox fetch

    # 重启服务
    if is_supervisord_running; then
        info "重启应用..."
        sctl restart aistudio
        sleep 2
        sctl status
    else
        warn "supervisord 未运行，请手动启动: ./service.sh start"
    fi
}

cmd_uninstall() {
    warn "即将停止服务并清理 supervisord 配置文件"
    read -rp "确认继续？(y/N): " confirm
    if [ "$confirm" != "y" ] && [ "$confirm" != "Y" ]; then
        info "已取消"
        return
    fi

    cmd_stop

    rm -f "${SUPERVISOR_CONF}" "${SUPERVISOR_PID}" "${SUPERVISOR_SOCK}"
    info "supervisord 配置文件已清理"
    info "虚拟环境和项目文件未删除，如需完全清理请手动执行:"
    echo "  rm -rf ${VENV_DIR}"
    echo "  rm -rf ${PROJECT_DIR}/logs"
}

# =====================================================================
# 帮助信息
# =====================================================================
cmd_help() {
    echo ""
    echo -e "${CYAN}AIStudioBuildWS 服务管理${NC}"
    echo ""
    echo "用法: $0 <命令>"
    echo ""
    echo "命令:"
    echo "  start      启动服务"
    echo "  stop       停止服务"
    echo "  restart    重启应用进程"
    echo "  status     查看运行状态和资源占用"
    echo "  logs       实时查看应用日志"
    echo "  slog       查看 supervisord 管理日志"
    echo "  update     拉取代码更新并重启"
    echo "  uninstall  停止服务并清理配置"
    echo ""
}

# =====================================================================
# 入口
# =====================================================================
case "${1:-help}" in
    start)     cmd_start ;;
    stop)      cmd_stop ;;
    restart)   cmd_restart ;;
    status)    cmd_status ;;
    logs)      cmd_logs ;;
    slog)      cmd_slog ;;
    update)    cmd_update ;;
    uninstall) cmd_uninstall ;;
    help|--help|-h)  cmd_help ;;
    *)
        error "未知命令: $1"
        cmd_help
        exit 1
        ;;
esac
