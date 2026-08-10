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
    echo -e "${CYAN}═══ 服务状态 ═══${NC}"
    echo ""
    sctl status
    echo ""

    # 获取主进程 PID
    local pid
    pid=$(sctl pid aistudio 2>/dev/null || echo "0")
    if [ "$pid" = "0" ] || [ -z "$pid" ]; then
        warn "应用进程未运行"
        return
    fi

    echo -e "${CYAN}═══ 进程信息 ═══${NC}"
    echo ""
    info "主进程 PID: ${pid}"

    # 主进程内存
    local main_rss
    main_rss=$(ps --no-headers -o rss -p "$pid" 2>/dev/null || echo "0")
    local main_rss_mb=$((main_rss / 1024))

    # 收集所有后代进程（递归查找整棵进程树）
    local all_pids
    all_pids=$(pstree -p "$pid" 2>/dev/null | grep -oP '\(\K[0-9]+(?=\))' | sort -u 2>/dev/null || echo "$pid")
    local child_count=0
    local total_rss=0
    local firefox_rss=0
    local firefox_count=0

    while IFS= read -r cpid; do
        [ -z "$cpid" ] && continue
        local crss cname
        crss=$(ps --no-headers -o rss -p "$cpid" 2>/dev/null || echo "0")
        cname=$(ps --no-headers -o comm -p "$cpid" 2>/dev/null || echo "")
        total_rss=$((total_rss + crss))

        if [ "$cpid" != "$pid" ]; then
            child_count=$((child_count + 1))
        fi

        # 识别 Firefox/Camoufox 进程
        case "$cname" in
            *firefox*|*camoufox*|*Web*Content*|*GPU*Process*|*Socket*Process*|*RDD*Process*)
                firefox_rss=$((firefox_rss + crss))
                firefox_count=$((firefox_count + 1))
                ;;
        esac
    done <<< "$all_pids"

    local total_rss_mb=$((total_rss / 1024))
    local firefox_rss_mb=$((firefox_rss / 1024))
    local python_rss_mb=$((total_rss_mb - firefox_rss_mb))

    # 进程启动时间
    local start_time
    start_time=$(ps --no-headers -o lstart -p "$pid" 2>/dev/null || echo "")
    if [ -n "$start_time" ]; then
        info "启动时间: ${start_time}"
    fi

    # CPU 使用率
    local cpu_usage
    cpu_usage=$(ps --no-headers -o %cpu -p "$pid" 2>/dev/null | tr -d ' ' || echo "")
    if [ -n "$cpu_usage" ]; then
        info "主进程 CPU: ${cpu_usage}%"
    fi

    echo ""
    echo -e "${CYAN}═══ 内存占用 ═══${NC}"
    echo ""
    printf "  %-28s %s\n" "Python 主进程:" "${main_rss_mb} MB"
    if [ $firefox_count -gt 0 ]; then
        printf "  %-28s %s\n" "浏览器进程 (${firefox_count} 个):" "${firefox_rss_mb} MB"
    fi
    printf "  %-28s %s\n" "其他子进程:" "$((python_rss_mb - main_rss_mb)) MB"
    echo "  ────────────────────────────────"
    printf "  %-28s %s\n" "总计 (${child_count} 个子进程):" "${total_rss_mb} MB"

    # 尝试从健康检查端点获取应用状态
    echo ""
    echo -e "${CYAN}═══ 应用状态 ═══${NC}"
    echo ""

    local health_url="http://127.0.0.1:7860/health"
    local health_json
    health_json=$(curl -s --connect-timeout 3 --max-time 5 "$health_url" 2>/dev/null || echo "")

    if [ -n "$health_json" ] && echo "$health_json" | python3 -c "import sys,json; json.load(sys.stdin)" 2>/dev/null; then
        # 解析 JSON（使用 Python，不引入 jq 依赖）
        echo "$health_json" | python3 -c "
import sys, json
d = json.load(sys.stdin)

# 服务状态
status = d.get('status', '?')
status_colors = {
    'healthy': '\033[0;32m',     # 绿色
    'partial': '\033[1;33m',     # 黄色
    'starting': '\033[0;36m',    # 青色
    'degraded': '\033[0;31m',    # 红色
    'stopping': '\033[0;31m',
    'waiting_cookie_update': '\033[1;33m',
}
color = status_colors.get(status, '\033[0m')
print(f'  服务状态:         {color}{status}\033[0m')

# 实例统计
configured = d.get('configured_instances', 0)
ready = d.get('ready_instances', 0)
connected = d.get('connected_instances', 0)
waiting = d.get('waiting_cookie_update_instances', 0)
terminal = d.get('terminal_instances', 0)
gen = d.get('browser_generation', 0)

print(f'  浏览器代际:       #{gen}')
print(f'  已注册账号:       {configured}')
print(f'  运行中:           {ready}')
print(f'  WS 已连接:        {connected}')
if waiting > 0:
    print(f'  等待Cookie更新:   \033[1;33m{waiting}\033[0m')
if terminal > 0:
    print(f'  已终止:           \033[0;31m{terminal}\033[0m')

# Provider Label
pl = d.get('provider_label', {})
if pl.get('enabled'):
    labeled = pl.get('labeled_instances', 0)
    failures = pl.get('injection_failures', 0)
    fail_str = f' (\033[1;33m{failures} 次注入失败\033[0m)' if failures > 0 else ''
    print(f'  Provider Label:   {labeled} 个已标记{fail_str}')

# 远程 Cookie
rc = d.get('remote_cookie', {})
if rc.get('configured'):
    print(f'  远程Cookie:       已配置')

# 告警
alert = d.get('alerting', {})
if alert.get('enabled'):
    depth = alert.get('queue_depth', 0)
    print(f'  邮件告警:         已启用 (队列: {depth})')
"
    else
        warn "健康检查端点不可用 (${health_url})"
        info "可能未启用 HG 模式或服务尚在启动中"
    fi
    echo ""
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
