"""
main.py — 入口、异步编排、信号处理、后台 Cookie 刷新与健康检查服务

职责：
1. 加载 .env 文件
2. 初始化目录与全局 Logger
3. 解析运行时配置
4. 创建告警管理器并启动
5. 创建 Cookie 生命周期管理器并执行初始化（含首次远程拉取）
6. 创建 BrowserSupervisor
7. 启动后台 Cookie 刷新与失效恢复检查任务
8. 注册 asyncio 信号处理器
9. 在 HG 模式下启动 aiohttp 健康检查服务（服务常驻，不随监督器结束而关闭）
10. 在独立模式下运行监督器直到完成
"""

import asyncio
import os
import signal

from utils import (
    load_env_file, ensure_dir, logs_dir, cookies_dir,
    clean_env_value, safe_int_env, parse_headless_mode,
    RuntimeConfig, mask_proxy_for_logging,
    parse_provider_label_config,
)

# 在导入其他项目模块之前先加载 .env
load_env_file()

from logger import setup_root_logger, get_logger
from cookies import CookieLifecycleManager
from browser import BrowserSupervisor
from notifier import AlertManager


# =====================================================================
# 配置构建
# =====================================================================

def build_runtime_config(logger) -> RuntimeConfig:
    """从环境变量解析运行时配置"""
    target_url = clean_env_value(os.getenv("CAMOUFOX_INSTANCE_URL")) or ""
    if not target_url:
        logger.error("错误: 缺少环境变量 CAMOUFOX_INSTANCE_URL")

    headless = clean_env_value(os.getenv("CAMOUFOX_HEADLESS")) or "virtual"
    proxy = clean_env_value(os.getenv("CAMOUFOX_PROXY"))

    instance_delay = safe_int_env("INSTANCE_START_DELAY", 30, minimum=0, maximum=3600)
    max_instance = safe_int_env("MAX_RESTART_RETRIES", 5, minimum=0, maximum=100)
    max_browser = safe_int_env(
        "MAX_BROWSER_RESTART_RETRIES", max_instance, minimum=0, maximum=100
    )
    hg_mode = os.getenv('HG', '').lower() == 'true'
    
    # ── Provider Label Cookie 注入 ──
    provider_label = parse_provider_label_config(logger)

    config = RuntimeConfig(
        target_url=target_url,
        headless_mode=parse_headless_mode(headless),
        proxy=proxy,
        instance_start_delay=instance_delay,
        max_instance_retries=max_instance,
        max_browser_retries=max_browser,
        hg_mode=hg_mode,
        # ── Cookie 远程集中管理 ──
        cookie_remote_url=clean_env_value(os.getenv("COOKIE_REMOTE_URL")) or "",
        cookie_remote_token=clean_env_value(os.getenv("COOKIE_REMOTE_TOKEN")) or "",
        cookie_remote_timeout=safe_int_env("COOKIE_REMOTE_TIMEOUT", 20, minimum=5, maximum=60),
        cookie_refresh_interval=safe_int_env("COOKIE_REFRESH_INTERVAL", 3600, minimum=60, maximum=86400),
        recovery_check_interval=safe_int_env("RECOVERY_CHECK_INTERVAL", 60, minimum=15, maximum=3600),
        cookie_context_update_delay=safe_int_env("COOKIE_CONTEXT_UPDATE_DELAY", 3, minimum=0, maximum=60),
        cookie_remote_failure_alert_threshold=safe_int_env(
            "COOKIE_REMOTE_FAILURE_ALERT_THRESHOLD", 3, minimum=1, maximum=100
        ),
        # ── 邮件通知 ──
        notification_enabled=os.getenv('NOTIFICATION_EMAIL_ENABLED', '').lower() == 'true',
        notification_prefix=clean_env_value(os.getenv("NOTIFICATION_INSTANCE_PREFIX")) or "",
        notification_from=clean_env_value(os.getenv("NOTIFICATION_EMAIL_FROM")) or "",
        notification_to=clean_env_value(os.getenv("NOTIFICATION_EMAIL_TO")) or "",
        notification_cooldown=safe_int_env("NOTIFICATION_EMAIL_COOLDOWN", 1800, minimum=60, maximum=86400),
        smtp_host=clean_env_value(os.getenv("NOTIFICATION_EMAIL_SMTP_HOST")) or "smtp.gmail.com",
        smtp_port=safe_int_env("NOTIFICATION_EMAIL_SMTP_PORT", 587, minimum=1, maximum=65535),
        smtp_user=clean_env_value(os.getenv("EMAIL_SMTP_USER")) or "",
        smtp_password=clean_env_value(os.getenv("EMAIL_SMTP_APP_PASSWORD")) or "",
        provider_label=provider_label,    
    )

    # 安全日志：代理地址脱敏，不输出敏感信息
    proxy_display = mask_proxy_for_logging(proxy) if proxy else '无'
    logger.info(
        f"配置: headless={headless}, proxy={proxy_display}, "
        f"启动间隔={instance_delay}s, 实例重试={max_instance}, 浏览器重试={max_browser}, "
        f"远程Cookie={'已配置' if config.cookie_remote_url else '未配置'}, "
        f"邮件通知={'已启用' if config.notification_enabled else '未启用'}, "
        f"ProviderLabel={'已启用' if provider_label.enabled else '未启用'}"
    )

    # 非浏览器出站代理日志
    if config.proxy:
        logger.info(
            f"非浏览器出站代理: {proxy_display} "
            f"(远程Cookie拉取 + SMTP邮件发送)"
        )
        
    # Provider Label 详细日志
    if provider_label.enabled:
        logger.info(
            f"Provider Label Cookie 注入: "
            f"{len(provider_label.cookie_names)} 个 Cookie 名称 × "
            f"{len(provider_label.gateway_domains)} 个网关域名, "
            f"前缀: {provider_label.prefix}"
        )
        if provider_label.prefix == "default":
            logger.warning(
                "Provider Label 注入已启用但未配置 WS_LABEL_PREFIX，"
                "使用默认前缀 default；多容器部署时可能无法区分来源"
            )
    elif provider_label.requested:
        logger.warning(
            f"Provider Label Cookie 注入已禁用: {provider_label.disabled_reason}"
        )

    return config


# =====================================================================
# 健康检查 aiohttp 应用
# =====================================================================

def build_health_app(supervisor: BrowserSupervisor, cookie_mgr: CookieLifecycleManager,
                     notifier: AlertManager):
    """构建 aiohttp 应用（健康检查端点）"""
    from aiohttp import web

    async def health_check(request):
        """健康检查端点 — 根据实例运行状态返回分级状态"""
        snap = supervisor.snapshot()
        configured = snap['configured_instances']
        ready = snap['ready_instances']
        terminal = snap['terminal_instances']
        waiting = snap.get('waiting_cookie_update_instances', 0)

        if supervisor.shutdown_event.is_set():
            status = 'stopping'
        elif configured == 0:
            status = 'degraded'
        elif ready == configured:
            # 所有账号均在运行
            status = 'healthy'
        elif ready > 0:
            # 部分账号运行中，其余在启动/恢复/终态/等待Cookie
            status = 'partial'
        elif waiting > 0:
            # 无账号运行，但有账号在等待Cookie更新
            status = 'waiting_cookie_update'
        elif terminal == configured:
            # 所有账号已达终态
            status = 'degraded'
        else:
            # 无账号运行，但仍有非终态账号（启动中/恢复中）
            status = 'starting'

        return web.json_response({
            'status': status,
            **snap,
            'remote_cookie': {
                'configured': bool(cookie_mgr.config.cookie_remote_url),
            },
            'alerting': {
                'enabled': notifier._enabled,
                'queue_depth': notifier.queue_depth,
            },
            'message': (
                f'Application is running with '
                f'{snap["ready_instances"]}/{configured} active browser instances'
            ),
        })

    async def index(request):
        """主页端点"""
        snap = supervisor.snapshot()
        return web.json_response({
            'status': 'running',
            'run_mode': 'server',
            **snap,
            'message': 'Camoufox Browser Automation is running in server mode',
        })

    app = web.Application()
    app.router.add_get('/health', health_check)
    app.router.add_get('/', index)
    return app


# =====================================================================
# 后台任务
# =====================================================================

async def _wait_or_shutdown(shutdown_event, seconds):
    """
    可中断的异步等待。

    每秒检查一次 shutdown_event，收到关闭信号时立即返回。

    Args:
        shutdown_event: asyncio.Event 关闭事件
        seconds: 等待时间（秒）

    Returns:
        "shutdown" 或 "completed"
    """
    elapsed = 0.0
    while elapsed < seconds:
        if shutdown_event.is_set():
            return "shutdown"
        await asyncio.sleep(min(1.0, seconds - elapsed))
        elapsed += 1.0
    return "completed"


async def background_cookie_refresh(cookie_mgr, supervisor, config,
                                     shutdown_event, notifier):
    """
    定时远程 Cookie 刷新后台任务。

    按 COOKIE_REFRESH_INTERVAL 定时执行远程 Cookie 拉取，
    拉取成功且有变更时通知 BrowserSupervisor 进行 Context 操作。
    新增账号会动态创建对应的 BrowserContext。

    当没有就绪实例时（无 Cookie / 全部失效 / 启动中），
    自动使用 RECOVERY_CHECK_INTERVAL 作为更短的刷新间隔。

    日志策略：
    - 有远程配置 + 正常间隔：INFO（每小时一次，合理）
    - 无远程配置 + 短间隔（recovery 模式）：DEBUG（避免每分钟重复）
    - 有变更：始终 INFO
    """
    logger = get_logger("refresh")

    while not shutdown_event.is_set():
        # 动态确定刷新间隔：无就绪实例时使用更短的恢复间隔
        snap = supervisor.snapshot()
        if snap['ready_instances'] == 0:
            wait_interval = config.recovery_check_interval
        else:
            wait_interval = config.cookie_refresh_interval

        result = await _wait_or_shutdown(shutdown_event, wait_interval)
        if result == "shutdown":
            break

        try:
            # 短间隔 + 无远程配置 = recovery 模式纯本地扫描，降为 DEBUG 避免噪声
            if wait_interval < config.cookie_refresh_interval and not config.cookie_remote_url:
                logger.debug("执行定时 Cookie 刷新（本地扫描）...")
            else:
                logger.info("执行定时 Cookie 刷新...")

            changes = await cookie_mgr.refresh()

            if changes.has_changes:
                logger.info(
                    f"Cookie 变更: {len(changes.updated)} 个更新, "
                    f"{len(changes.added)} 个新增"
                )
                await supervisor.apply_cookie_changes(changes)

                # 为新增账号发送通知
                for aid in changes.added:
                    if notifier:
                        await notifier.emit(
                            "COOKIE_ACCOUNT_ADDED", "INFO",
                            account_id=aid,
                            message="新增账号已动态加入",
                        )
            else:
                logger.debug("Cookie 未发生变化")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"定时 Cookie 刷新异常: {e}")
            await asyncio.sleep(5)

    logger.debug("定时 Cookie 刷新任务已停止")


async def background_recovery_check(cookie_mgr, supervisor, config,
                                     shutdown_event, notifier):
    """
    Cookie 失效恢复监控后台任务。

    监听 cookie_invalid_event（Cookie 验证失效时立即触发），
    或按 RECOVERY_CHECK_INTERVAL 定期扫描所有等待恢复的账号。
    发现新版本 Cookie 时通知 BrowserSupervisor 重建对应 Context。

    日志策略：
    - 无变化时使用 DEBUG，避免每分钟产生重复 INFO 日志
    - 失效事件触发、发现可恢复变更、等待账号数变化时使用 INFO
    - 每 30 次检查（约 30 分钟）输出一次 INFO 心跳摘要
    """
    logger = get_logger("recovery")
    check_count = 0
    last_waiting_count = -1  # 初始化为 -1，确保首次检查时输出 INFO
    heartbeat_interval = 30  # 每 30 次检查输出一次心跳

    while not shutdown_event.is_set():
        try:
            # 等待失效事件触发或超时
            try:
                await asyncio.wait_for(
                    supervisor.cookie_invalid_event.wait(),
                    timeout=config.recovery_check_interval,
                )
                supervisor.cookie_invalid_event.clear()
                logger.info("Cookie 失效事件触发，立即检查恢复...")
            except asyncio.TimeoutError:
                pass

            if shutdown_event.is_set():
                break

            waiting = supervisor.get_waiting_accounts()
            if not waiting:
                last_waiting_count = 0
                continue

            check_count += 1
            current_count = len(waiting)

            # 日志级别决策：仅在有意义的状态变化或定期心跳时使用 INFO
            if current_count != last_waiting_count:
                # 等待账号数量变化（含首次检查）
                logger.info(f"检查 {current_count} 个等待恢复的账号...")
            elif check_count % heartbeat_interval == 0:
                # 定期心跳摘要
                logger.info(
                    f"恢复监控仍在运行: {current_count} 个账号等待 Cookie 更新 "
                    f"(已检查 {check_count} 次)"
                )
            else:
                # 常规无变化检查
                logger.debug(f"检查 {current_count} 个等待恢复的账号...")

            last_waiting_count = current_count

            changes = await cookie_mgr.check_recovery(waiting)

            if changes.has_changes:
                await supervisor.apply_cookie_changes(changes)

                for aid in changes.updated:
                    logger.info(f"账号 {aid} Cookie 已更新，触发 Context 重建")

        except asyncio.CancelledError:
            break
        except Exception as e:
            logger.error(f"恢复检查异常: {e}")
            await asyncio.sleep(5)

    logger.debug("Cookie 恢复监控任务已停止")


# =====================================================================
# 监督器运行循环
# =====================================================================

async def _run_supervisor_loop(supervisor, shutdown_event, logger):
    """
    监督器运行循环，支持 Cookie 更新后自动重启。

    当 supervisor.run() 正常返回后（所有实例终态或全部等待 Cookie 更新），
    不立即退出，而是等待 cookie_update_event 信号后重新启动监督器。
    这使得后台 Cookie 刷新任务发现新 Cookie 后能自动恢复服务。

    Args:
        supervisor: BrowserSupervisor 实例
        shutdown_event: 全局关闭事件
        logger: 日志记录器
    """
    while not shutdown_event.is_set():
        # ── 等待至少有一个实例记录可用 ──
        # 首次启动时若无初始 Cookie（远程拉取失败、无本地文件），
        # 后台任务可能随后通过 apply_cookie_changes 注入新记录
        while not supervisor.records and not shutdown_event.is_set():
            logger.debug("等待 Cookie 数据可用...")
            await asyncio.sleep(3)
        if shutdown_event.is_set():
            break

        # ── 运行监督器 ──
        await supervisor.run()

        # ── supervisor.run() 返回后等待 Cookie 更新事件 ──
        # 可能的返回原因：所有实例终态、浏览器重启上限、全部等待 Cookie
        # 后台任务发现新 Cookie 后会设置 cookie_update_event
        if not shutdown_event.is_set():
            logger.info("监督器已停止，等待 Cookie 更新后重新启动...")
            while not shutdown_event.is_set():
                if supervisor.cookie_update_event.is_set():
                    supervisor.cookie_update_event.clear()
                    logger.info("检测到 Cookie 更新，重新启动监督器")
                    break
                await asyncio.sleep(1)


# =====================================================================
# 信号处理
# =====================================================================

def _on_signal(sig, shutdown_event, logger):
    """异步信号处理器回调"""
    sig_name = signal.Signals(sig).name if hasattr(signal, 'Signals') else str(sig)
    logger.info(f"接收到信号 {sig_name}，触发关闭...")
    shutdown_event.set()


# =====================================================================
# 异步主入口
# =====================================================================

async def main_async():
    """异步主入口函数"""
    shutdown_event = asyncio.Event()
    logger = get_logger("main")

    # ── 信号处理器注册 ──
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGTERM, signal.SIGINT):
        try:
            loop.add_signal_handler(sig, _on_signal, sig, shutdown_event, logger)
        except (ValueError, OSError, NotImplementedError):
            pass
    for sig_name in ('SIGQUIT', 'SIGHUP'):
        sig_num = getattr(signal, sig_name, None)
        if sig_num:
            try:
                loop.add_signal_handler(sig_num, _on_signal, sig_num, shutdown_event, logger)
            except (ValueError, OSError, NotImplementedError):
                pass

    # ── 解析配置 ──
    config = build_runtime_config(logger)

    if not config.target_url:
        if not config.hg_mode:
            logger.error("目标URL未配置，独立模式退出")
            return
        logger.warning("目标URL未配置，健康检查服务将以 degraded 状态运行")

    # ── 创建告警管理器 ──
    notifier = AlertManager(config, get_logger("notifier"))
    await notifier.start()

    # ── 创建 Cookie 生命周期管理器并执行初始化 ──
    # bootstrap() 会按顺序执行：
    # 1. 扫描环境变量 Cookie 并写入本地文件
    # 2. 拉取远程 Cookie 并写入本地文件（覆盖同名环境变量文件）
    # 3. 扫描本地文件构建最终有效快照
    # 在首次远程拉取完成（成功或失败）之前，不创建任何 BrowserContext
    cookie_mgr = CookieLifecycleManager(config, get_logger("cookies"), notifier)

    try:
        effective = await cookie_mgr.bootstrap()
    except Exception as e:
        logger.error(f"Cookie 初始化失败: {e}")
        effective = {}

    # ── 检查 Cookie 来源可用性 ──
    if not effective:
        if not config.hg_mode and not config.cookie_remote_url:
            # 独立模式：无有效 Cookie 且无远程 Cookie 配置，无法恢复
            logger.error("未找到任何有效 Cookie 来源且无远程 Cookie 配置")
            await notifier.stop()
            await cookie_mgr.close()
            return
        if config.cookie_remote_url:
            logger.warning(
                "无有效 Cookie，但已配置远程 Cookie 地址，"
                "将等待后台刷新任务获取 Cookie"
            )
        elif config.hg_mode:
            logger.warning("无 Cookie 来源，健康检查服务将以 degraded 状态运行")

    # ── 创建监督器 ──
    # 使用 bootstrap() 返回的有效 Cookie 快照作为初始账号数据
    # 即使 effective 为空，也创建监督器（后台任务可能动态注入账号）
    supervisor = BrowserSupervisor(config, effective, shutdown_event, notifier)

    # ── 启动后台 Cookie 管理任务 ──
    # 仅在 target_url 有效时启动（无 URL 则无需管理 Cookie）
    bg_tasks = []
    if config.target_url:
        bg_tasks.append(asyncio.create_task(
            background_cookie_refresh(
                cookie_mgr, supervisor, config, shutdown_event, notifier
            ),
            name="bg-cookie-refresh",
        ))
        bg_tasks.append(asyncio.create_task(
            background_recovery_check(
                cookie_mgr, supervisor, config, shutdown_event, notifier
            ),
            name="bg-recovery-check",
        ))

    # ── 启动 ──
    if config.hg_mode:
        from aiohttp import web

        app = build_health_app(supervisor, cookie_mgr, notifier)
        runner = web.AppRunner(app, access_log=None)
        await runner.setup()
        site = web.TCPSite(runner, '0.0.0.0', 7860)
        await site.start()
        logger.info("aiohttp 健康检查服务已启动，监听端口 7860")

        # 监督器作为后台任务运行，不阻塞主协程
        # supervisor.run() 结束（无论是正常结束还是全部失效）不会导致 aiohttp 关闭
        # 只有收到关闭信号时才统一退出
        supervisor_task = None

        async def _run_supervisor_safe():
            """安全包装：监督器异常不应中断 aiohttp 服务"""
            try:
                await _run_supervisor_loop(supervisor, shutdown_event, logger)
            except asyncio.CancelledError:
                raise
            except Exception as e:
                logger.exception(f"监督器异常退出: {e}")
            finally:
                logger.info("监督器循环已结束（健康检查服务继续运行）")

        try:
            if config.target_url:
                supervisor_task = asyncio.create_task(
                    _run_supervisor_safe(),
                    name="supervisor-loop",
                )

            # 主协程等待关闭信号（不是等待监督器结束）
            await shutdown_event.wait()

        finally:
            logger.info("正在执行关闭流程...")

            # 确保监督器任务被取消和等待
            if supervisor_task and not supervisor_task.done():
                supervisor_task.cancel()
                try:
                    await supervisor_task
                except asyncio.CancelledError:
                    pass

            # 取消并等待后台任务
            for t in bg_tasks:
                if not t.done():
                    t.cancel()
            if bg_tasks:
                await asyncio.gather(*bg_tasks, return_exceptions=True)

            # 关闭告警服务（尝试发送剩余队列中的邮件）
            await notifier.stop()

            # 关闭 Cookie 管理器的 HTTP 会话
            await cookie_mgr.close()

            # 关闭 aiohttp 服务
            logger.info("正在关闭 aiohttp 服务...")
            await runner.cleanup()

    else:
        # 独立模式：运行监督器循环直到关闭信号
        try:
            if config.target_url:
                await _run_supervisor_loop(supervisor, shutdown_event, logger)
        finally:
            # 取消并等待后台任务
            for t in bg_tasks:
                if not t.done():
                    t.cancel()
            if bg_tasks:
                await asyncio.gather(*bg_tasks, return_exceptions=True)

            # 关闭告警服务
            await notifier.stop()

            # 关闭 Cookie 管理器的 HTTP 会话
            await cookie_mgr.close()

    logger.info("主程序退出")


# =====================================================================
# 同步入口
# =====================================================================

def main():
    """同步入口函数"""
    ensure_dir(logs_dir())
    ensure_dir(cookies_dir())
    setup_root_logger(str(logs_dir() / 'app.log'))

    try:
        asyncio.run(main_async())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
