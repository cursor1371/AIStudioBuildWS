"""
notifier.py — 异步邮件告警管理器

使用 Gmail SMTP STARTTLS 发送通知邮件。
- 每封邮件同时包含 HTML 和纯文本两种格式正文
- 告警事件按 alert_key 去重，受冷却时间节流
- SMTP 阻塞操作在后台线程中执行，不阻塞 asyncio 事件循环
- 有界队列防止内存溢出，队列满时丢弃最新事件
"""

import asyncio
import datetime
import html
import smtplib
import ssl
import time as _time
from dataclasses import dataclass, field
from email.message import EmailMessage
from typing import Optional

from logger import get_logger
from utils import parse_proxy_url

# =====================================================================
# 告警事件数据模型
# =====================================================================

@dataclass
class AlertEvent:
    """
    告警事件数据。

    Attributes:
        event_type: 事件类型标识（如 "COOKIE_INVALID_WAITING"）
        severity: 严重级别（INFO / WARNING / CRITICAL / ERROR）
        account_id: 关联账号 ID（可选，用于去重和展示）
        message: 事件简要描述
        details: 附加细节信息
        timestamp: 事件产生时间（Unix 时间戳）
        alert_key: 去重键（空时由 emit() 自动生成）
    """
    event_type: str
    severity: str = "WARNING"
    account_id: str = ""
    message: str = ""
    details: str = ""
    timestamp: float = field(default_factory=_time.time)
    alert_key: str = ""

# =====================================================================
# 代理 SMTP 客户端
# =====================================================================

class _ProxiedSMTP(smtplib.SMTP):
    """
    通过 PySocks 代理连接的 SMTP 客户端。

    覆写 _get_socket() 方法，使用 PySocks 的 socksocket 建立代理连接。
    支持 SOCKS5 / SOCKS4 / HTTP CONNECT 三种代理类型。

    继承 smtplib.SMTP 的全部功能，包括 STARTTLS、认证和上下文管理器。
    """

    def __init__(self, host, port, proxy_info, timeout=30):
        """
        Args:
            host: SMTP 服务器地址
            port: SMTP 服务器端口
            proxy_info: ProxyInfo 实例
            timeout: 连接超时（秒）
        """
        self._pinfo = proxy_info
        super().__init__(host, port, timeout=timeout)

    def _get_socket(self, host, port, timeout):
        """覆写：通过代理 socket 建立连接"""
        import socks as pysocks

        proxy_type_map = {
            'socks5': pysocks.SOCKS5,
            'socks4': pysocks.SOCKS4,
            'http': pysocks.HTTP,
        }
        ptype = proxy_type_map.get(self._pinfo.type, pysocks.HTTP)

        sock = pysocks.socksocket()
        sock.set_proxy(
            ptype,
            self._pinfo.host,
            self._pinfo.port,
            username=self._pinfo.username,
            password=self._pinfo.password,
        )
        sock.settimeout(timeout)
        sock.connect((host, port))
        return sock

# =====================================================================
# 告警管理器
# =====================================================================

class AlertManager:
    """
    异步告警管理器。

    通过有界队列和单后台 Worker 实现异步、去重、节流的邮件发送。
    SMTP 阻塞操作通过 asyncio.to_thread 在独立线程中执行。

    生命周期：
        manager = AlertManager(config)
        await manager.start()     # 启动后台 Worker
        await manager.emit(...)   # 发送告警事件（自动去重节流）
        await manager.stop()      # 关闭 Worker，尝试发送剩余队列

    安全说明：
        邮件内容中不包含 Cookie value、SMTP 密码、代理凭据、远程 URL 等敏感信息。
        所有动态文本经 html.escape() 转义，防止 HTML 注入。
    """

    # 队列最大容量
    _MAX_QUEUE_SIZE = 100

    # 关闭时等待剩余邮件发送的最大时长（秒）
    _DRAIN_TIMEOUT = 5

    def __init__(self, config, logger=None):
        """
        初始化告警管理器。

        Args:
            config: RuntimeConfig，包含 notification_* 和 smtp_* 配置
            logger: 可选日志记录器
        """
        self.config = config
        self.logger = logger or get_logger("notifier")
        self._enabled = config.notification_enabled

        # 有界异步队列
        self._queue: asyncio.Queue = asyncio.Queue(maxsize=self._MAX_QUEUE_SIZE)
        # 去重冷却时间记录：alert_key → 上次发送的时间戳
        self._cooldowns: dict = {}
        # 后台 Worker 任务引用
        self._worker_task: Optional[asyncio.Task] = None
        # 关闭标志
        self._shutdown = False

        # 代理配置（用于 SMTP 出站，复用 CAMOUFOX_PROXY）
        self._proxy_info = None
        if config.proxy:
            proxy_info = parse_proxy_url(config.proxy, self.logger)
            if proxy_info:
                try:
                    import socks  # noqa: F401 — 验证 PySocks 可用
                    self._proxy_info = proxy_info
                except ImportError:
                    self.logger.warning(
                        "SMTP 代理需要 PySocks 库，未安装，SMTP 邮件发送将使用直连"
                    )

        # 统计计数器（用于日志和健康检查）
        self._sent_count = 0
        self._throttled_count = 0
        self._dropped_count = 0

    # =================================================================
    # 生命周期管理
    # =================================================================

    async def start(self):
        """启动邮件发送后台 Worker"""
        if not self._enabled:
            self.logger.info("邮件告警未启用 (NOTIFICATION_EMAIL_ENABLED != true)")
            return

        # 启动前校验必要配置，缺失时记录警告但不阻止启动
        missing = []
        if not self.config.smtp_user:
            missing.append("EMAIL_SMTP_USER")
        if not self.config.smtp_password:
            missing.append("EMAIL_SMTP_APP_PASSWORD")
        if not self.config.notification_from:
            missing.append("NOTIFICATION_EMAIL_FROM")
        if not self.config.notification_to:
            missing.append("NOTIFICATION_EMAIL_TO")

        if missing:
            self.logger.warning(
                f"邮件告警已启用但缺少必要配置: {', '.join(missing)}，"
                f"邮件发送将会失败"
            )

        self._worker_task = asyncio.create_task(
            self._worker(), name="notifier-worker"
        )
        self.logger.info(
            f"邮件告警服务已启动 "
            f"(SMTP: {self.config.smtp_host}:{self.config.smtp_port}, "
            f"冷却时间: {self.config.notification_cooldown}s)"
        )

    async def stop(self):
        """
        停止后台 Worker。

        先标记关闭，然后等待队列中剩余邮件发送完毕（最多 _DRAIN_TIMEOUT 秒），
        最后取消 Worker 任务。
        """
        if not self._worker_task:
            return

        self._shutdown = True
        remaining = self._queue.qsize()
        self.logger.info(
            f"正在关闭邮件告警服务 "
            f"(队列剩余: {remaining}, "
            f"累计发送: {self._sent_count}, "
            f"累计节流: {self._throttled_count}, "
            f"累计丢弃: {self._dropped_count})"
        )

        # 等待队列排空或超时
        if remaining > 0:
            try:
                await asyncio.wait_for(self._drain(), timeout=self._DRAIN_TIMEOUT)
                self.logger.info("邮件队列已排空")
            except asyncio.TimeoutError:
                still_remaining = self._queue.qsize()
                if still_remaining > 0:
                    self.logger.warning(
                        f"邮件队列排空超时，丢弃剩余 {still_remaining} 封邮件"
                    )

        # 取消 Worker 任务
        self._worker_task.cancel()
        try:
            await self._worker_task
        except asyncio.CancelledError:
            pass

        self.logger.info("邮件告警服务已关闭")

    async def _drain(self):
        """等待队列为空"""
        while not self._queue.empty():
            await asyncio.sleep(0.2)

    # =================================================================
    # 告警发射接口
    # =================================================================

    async def emit(self, event_type, severity="WARNING", account_id="",
                   message="", details="", alert_key=None):
        """
        发送告警事件。

        自动按 alert_key 去重并受冷却时间节流。
        冷却期内的重复事件会被静默丢弃（仅记录 debug 日志）。
        告警未启用时直接返回，无任何开销。

        Args:
            event_type: 事件类型标识（如 "REMOTE_FETCH_CONSECUTIVE_FAILURE"）
            severity: 严重级别（INFO / WARNING / CRITICAL / ERROR）
            account_id: 关联账号 ID（可选）
            message: 事件简要描述
            details: 附加细节信息
            alert_key: 自定义去重键（默认按 "event_type:account_id" 生成）
        """
        if not self._enabled:
            return

        # 生成去重键
        key = alert_key or f"{event_type}:{account_id}"
        now = _time.time()
        cooldown = self.config.notification_cooldown

        # 冷却期检查：同一 key 在冷却时间内不重复发送
        last_sent = self._cooldowns.get(key)
        if last_sent is not None and (now - last_sent) < cooldown:
            self._throttled_count += 1
            remaining = cooldown - (now - last_sent)
            self.logger.debug(
                f"告警被节流: [{severity}] {event_type} "
                f"(key={key}, 冷却剩余: {remaining:.0f}s)"
            )
            return

        event = AlertEvent(
            event_type=event_type,
            severity=severity,
            account_id=account_id,
            message=message,
            details=details,
            alert_key=key,
        )

        # 入队（非阻塞），入队成功后才记录冷却时间戳
        # 避免队列满导致入队失败时误设置 cooldown，造成后续同类告警被错误节流
        try:
            self._queue.put_nowait(event)
            self._cooldowns[key] = now
            self.logger.debug(
                f"告警已入队: [{severity}] {event_type}"
                + (f" (账号: {account_id})" if account_id else "")
            )
        except asyncio.QueueFull:
            self._dropped_count += 1
            self.logger.warning(
                f"告警队列已满 (容量: {self._MAX_QUEUE_SIZE})，"
                f"丢弃事件: [{severity}] {event_type}"
            )

    @property
    def queue_depth(self) -> int:
        """当前队列中待发送的事件数量（用于健康检查）"""
        return self._queue.qsize()

    # =================================================================
    # 后台 Worker
    # =================================================================

    async def _worker(self):
        """
        后台邮件发送 Worker 协程。

        从队列中逐个取出事件，在后台线程中执行 SMTP 发送。
        持续运行直到 _shutdown 标志被设置，然后排空剩余队列。
        """
        self.logger.debug("邮件发送 Worker 已启动")

        # ── 正常运行阶段：带超时等待，定期检查 shutdown 标志 ──
        while not self._shutdown:
            try:
                event = await asyncio.wait_for(
                    self._queue.get(), timeout=5.0
                )
            except asyncio.TimeoutError:
                # 队列为空，继续等待
                continue
            except asyncio.CancelledError:
                break

            await self._send_one(event)

        # ── 关闭阶段：排空队列中剩余的事件 ──
        drained = 0
        while not self._queue.empty():
            try:
                event = self._queue.get_nowait()
                await self._send_one(event)
                drained += 1
            except asyncio.QueueEmpty:
                break
            except asyncio.CancelledError:
                break

        if drained > 0:
            self.logger.info(f"关闭阶段额外发送了 {drained} 封邮件")

        self.logger.debug("邮件发送 Worker 已停止")

    async def _send_one(self, event: AlertEvent):
        """
        发送单封告警邮件。

        在后台线程中执行 SMTP 操作，捕获并分类记录各类发送错误。
        发送失败不影响后续事件处理。
        """
        log_suffix = (
            f"[{event.severity}] {event.event_type}"
            + (f" (账号: {event.account_id})" if event.account_id else "")
        )

        try:
            await asyncio.to_thread(self._do_smtp_send, event)
            self._sent_count += 1
            self.logger.info(f"告警邮件已发送: {log_suffix}")

        except smtplib.SMTPAuthenticationError as e:
            # SMTP 认证失败：通常是 App Password 错误或未启用
            error_detail = ""
            if hasattr(e, 'smtp_error'):
                error_detail = str(e.smtp_error, 'utf-8', errors='replace') if isinstance(e.smtp_error, bytes) else str(e.smtp_error)
            self.logger.error(
                f"SMTP 认证失败，请检查 EMAIL_SMTP_USER 和 "
                f"EMAIL_SMTP_APP_PASSWORD 配置: {e.smtp_code} {error_detail}"
            )

        except smtplib.SMTPRecipientsRefused as e:
            # 收件人被拒绝
            self.logger.error(
                f"SMTP 收件人被拒绝，请检查 NOTIFICATION_EMAIL_TO 配置: "
                f"{type(e).__name__}"
            )

        except smtplib.SMTPException as e:
            # 其他 SMTP 协议错误
            self.logger.error(f"SMTP 发送失败: {type(e).__name__}: {e}")

        except ConnectionError as e:
            # 网络连接失败（DNS 解析、连接拒绝、连接超时等）
            self.logger.error(
                f"SMTP 连接失败，请检查网络和 SMTP 服务器配置 "
                f"({self.config.smtp_host}:{self.config.smtp_port}): {e}"
            )

        except TimeoutError:
            # 连接或操作超时
            self.logger.error(
                f"SMTP 操作超时 "
                f"({self.config.smtp_host}:{self.config.smtp_port})"
            )

        except OSError as e:
            # PySocks 代理连接错误等其他网络层异常（非 ConnectionError / TimeoutError 子类）
            self.logger.error(
                f"SMTP 网络错误（可能是代理连接问题）: {type(e).__name__}: {e}"
            )

        except Exception as e:
            # 未预期异常
            self.logger.error(f"邮件发送异常: {type(e).__name__}: {e}")

    # =================================================================
    # SMTP 发送（在后台线程中执行，不阻塞事件循环）
    # =================================================================

    def _do_smtp_send(self, event: AlertEvent):
        """
        构造 MIME 邮件并通过 SMTP STARTTLS 发送。

        此方法在后台线程中执行（通过 asyncio.to_thread 调用），
        因此可以安全使用 smtplib 的阻塞 API。

        邮件同时包含纯文本和 HTML 两种正文格式，
        确保在各类邮件客户端中都能正常显示。
        """
        prefix = self.config.notification_prefix or "AIStudioBuildWS"

        # ── 构造邮件 ──
        subject = (
            f"[AIStudioBuildWS][{prefix}][{event.severity}] "
            f"{event.event_type}"
        )

        msg = EmailMessage()
        msg['Subject'] = subject
        msg['From'] = self.config.notification_from
        msg['To'] = self.config.notification_to

        # 纯文本正文（主格式）
        text_body = self._build_text(event, prefix)
        msg.set_content(text_body)

        # HTML 正文（备选格式，支持富文本展示的邮件客户端优先使用）
        html_body = self._build_html(event, prefix)
        msg.add_alternative(html_body, subtype='html')

        # ── SMTP STARTTLS 发送 ──
        ctx = ssl.create_default_context()
        # 根据代理配置选择 SMTP 连接方式
        if self._proxy_info:
            server = _ProxiedSMTP(
                self.config.smtp_host, self.config.smtp_port,
                self._proxy_info, timeout=30
            )
        else:
            server = smtplib.SMTP(
                self.config.smtp_host, self.config.smtp_port, timeout=30
            )
        with server:
            server.ehlo()
            server.starttls(context=ctx)
            server.ehlo()
            server.login(self.config.smtp_user, self.config.smtp_password)
            server.send_message(msg)

    # =================================================================
    # 邮件模板
    # =================================================================

    @staticmethod
    def _format_time(ts: float) -> str:
        """将 Unix 时间戳格式化为 UTC ISO 8601 字符串"""
        return datetime.datetime.fromtimestamp(
            ts, tz=datetime.timezone.utc
        ).strftime('%Y-%m-%dT%H:%M:%SZ')

    @staticmethod
    def _build_text(event: AlertEvent, prefix: str) -> str:
        """
        构造纯文本邮件正文。

        格式为键值对列表，每行一个字段，简洁易读。
        """
        lines = [
            f"实例: {prefix}",
            f"时间: {AlertManager._format_time(event.timestamp)}",
            f"事件: {event.event_type}",
            f"级别: {event.severity}",
        ]
        if event.account_id:
            lines.append(f"账号: {event.account_id}")
        if event.message:
            lines.append(f"说明: {event.message}")
        if event.details:
            lines.append(f"详情: {event.details}")
        lines.append("")
        lines.append("此邮件由 AIStudioBuildWS 自动发送，请勿回复。")
        return '\n'.join(lines)

    @staticmethod
    def _build_html(event: AlertEvent, prefix: str) -> str:
        """
        构造 HTML 邮件正文。

        使用简洁表格布局展示事件信息。
        所有动态内容经 html.escape() 转义，防止 XSS 或 HTML 结构破坏。
        根据严重级别使用不同颜色的标题。
        """
        # 构建表格行数据
        rows = [
            ("实例", html.escape(prefix)),
            ("时间", html.escape(AlertManager._format_time(event.timestamp))),
            ("事件", html.escape(event.event_type)),
            ("级别", html.escape(event.severity)),
        ]
        if event.account_id:
            rows.append(("账号", html.escape(event.account_id)))
        if event.message:
            rows.append(("说明", html.escape(event.message)))
        if event.details:
            rows.append(("详情", html.escape(event.details)))

        # 根据严重级别设置标题颜色
        color_map = {
            "CRITICAL": "#d32f2f",
            "ERROR": "#e53935",
            "WARNING": "#f57c00",
            "INFO": "#1976d2",
        }
        header_color = color_map.get(event.severity, "#616161")

        # 表格样式
        th_style = (
            "padding:8px 14px;border:1px solid #e0e0e0;"
            "background:#f5f5f5;text-align:left;white-space:nowrap;"
        )
        td_style = "padding:8px 14px;border:1px solid #e0e0e0;"

        # 构建表格 HTML
        tr_html = ''.join(
            f'<tr>'
            f'<td style="{th_style}">{label}</td>'
            f'<td style="{td_style}">{value}</td>'
            f'</tr>'
            for label, value in rows
        )

        return (
            '<html><body style="font-family:sans-serif;color:#333;">'
            f'<h3 style="color:{header_color};margin-bottom:12px;">'
            f'[{html.escape(event.severity)}] '
            f'{html.escape(event.event_type)}</h3>'
            f'<table style="border-collapse:collapse;width:100%;max-width:600px;">'
            f'{tr_html}'
            f'</table>'
            '<p style="color:#999;font-size:12px;margin-top:16px;">'
            '此邮件由 AIStudioBuildWS 自动发送，请勿回复。</p>'
            '</body></html>'
        )
