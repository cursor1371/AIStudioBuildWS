"""
browser.py — 浏览器生命周期、实例协程、导航验证、保活、WS 监控与 Cookie 验证

核心架构：单共享浏览器 + 多 BrowserContext + asyncio 协程
支持 Cookie 热更新、Browser 空闲模式与失效恢复等待。

BrowserSupervisor
├── 管理唯一的 AsyncCamoufox Browser
├── 每个 Cookie 来源创建独立 BrowserContext + Page
├── Context 级故障：仅重建该账号的 Context（指数退避）
├── Browser 级故障：重建共享 Browser，恢复所有可恢复 Context
├── Cookie 失效：进入 WAITING_COOKIE_UPDATE，等待新 Cookie 后自动恢复
├── Cookie 热更新：运行中检测到新版本时重建对应 Context
└── 全部失效：关闭 Browser 释放资源，保持低资源等待模式
"""

import asyncio
import os
import re
import random
import time as _time
from enum import Enum
from dataclasses import dataclass, field
from typing import Optional, Dict, List, Any
from urllib.parse import urlparse

from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
    Error as PlaywrightError,
)

from logger import get_logger
from utils import (
    RuntimeConfig, ensure_dir, logs_dir,
    mask_url_for_logging, mask_path_for_logging, mask_proxy_for_logging,
    build_provider_label_value, build_provider_label_cookies,
)


# =====================================================================
# 异常类型
# =====================================================================

class RecoverableInstanceError(Exception):
    """Context 级可恢复错误（导航超时、网络故障、保活异常等）"""
    pass


class CookieInvalidError(Exception):
    """Cookie 确认失效，应将该账号置入等待更新状态"""
    pass


class BrowserFaultError(Exception):
    """浏览器级故障（进程崩溃、连接断开）"""
    pass


class _CookieUpdateSignal(Exception):
    """内部信号：Cookie 热更新触发 Context 重建（非错误，不计入重试次数）"""
    pass


# =====================================================================
# 实例状态枚举
# =====================================================================

class InstanceState(Enum):
    PENDING = "pending"
    STARTING = "starting"
    RUNNING = "running"
    RETRY_BACKOFF = "retry_backoff"
    BROWSER_RECOVERING = "browser_recovering"
    WAITING_COOKIE_UPDATE = "waiting_cookie_update"
    CONFIG_ERROR = "config_error"
    RETRY_EXHAUSTED = "retry_exhausted"
    FAILED = "failed"
    STOPPING = "stopping"
    STOPPED = "stopped"


# 终态集合：达到这些状态后，账号不再自动恢复（Cookie 更新除外）
_TERMINAL_STATES = frozenset({
    InstanceState.CONFIG_ERROR,
    InstanceState.RETRY_EXHAUSTED,
    InstanceState.FAILED,
    InstanceState.STOPPED,
})

# Cookie 等待状态集合：等待外部提供新 Cookie 数据后可恢复
_COOKIE_WAITING_STATES = frozenset({
    InstanceState.WAITING_COOKIE_UPDATE,
})

# 存在活跃 Worker 且可通过取消旧 Worker 替换的状态集合
_ACTIVE_WORKER_STATES = frozenset({
    InstanceState.PENDING,
    InstanceState.STARTING,
    InstanceState.RETRY_BACKOFF,
})


# =====================================================================
# 实例记录
# =====================================================================

@dataclass
class InstanceRecord:
    """
    单个 Cookie 账号的运行时记录。

    每个账号拥有独立的 BrowserContext、Page、WebSocket、保活状态和重试计数器。
    通过 browser_generation 防止旧浏览器代际的 Worker 误操作新代际资源，
    通过 revision 防止旧 Cookie 版本的 Worker 覆盖新版本 Worker 的状态。
    """
    instance_id: int                             # 唯一实例编号
    account_id: str                              # 账号唯一标识（即本地文件名，如 "user1.json"）
    display_name: str                            # 日志显示名
    diagnostic_tag: str                          # 截图文件名中的安全标签
    cookies: List[Dict]                          # Playwright 兼容格式 Cookie 列表
    cookie_version: str = ""                     # 当前 Cookie 的 SHA-256 版本号
    state: InstanceState = InstanceState.PENDING # 当前运行状态
    retry_count: int = 0                         # Context 级连续重试次数
    revision: int = 0                            # Cookie 更新修订号（每次更新递增）
    failed_cookie_version: Optional[str] = None  # 导致失效的 Cookie 版本号
    last_ws_status: str = "UNKNOWN"              # 最近一次 WS 连接状态
    browser_generation: int = 0                  # 所属的浏览器代际编号
    context: Any = None                          # BrowserContext 引用（仅所属 Worker 可访问）
    page: Any = None                             # Page 引用（仅所属 Worker 可访问）
    task: Optional[asyncio.Task] = None          # Worker 协程任务引用
    cookie_update_pending: bool = False          # 是否有待处理的 Cookie 热更新
    pending_cookies: Optional[List[Dict]] = None # 待应用的新 Cookie 数据
    pending_cookie_version: Optional[str] = None # 待应用的新 Cookie 版本号
    created_at: float = field(default_factory=_time.time)
    provider_label_value: Optional[str] = None   # 该账号的 Provider Label 值
    provider_label_enabled: bool = False          # 是否启用 Provider Label 注入


# =====================================================================
# 常量与 JS 脚本
# =====================================================================

# 字体资源拦截模式
_FONT_URL_PATTERN = re.compile(r'\.(woff2?|ttf|otf)(\?|#|$)', re.IGNORECASE)

# 浏览器稳定运行判定阈值（秒）
# 只有 Browser 稳定运行超过此时长后崩溃，才重置浏览器重试计数器
_BROWSER_STABLE_SECONDS = 60

# 初始化阶段弹窗按钮
_INIT_POPUP_BUTTONS = ["Got it", "Continue to the app"]

# 运行时弹窗按钮（含恢复类按钮）
_ALL_POPUP_BUTTONS = [
    "Reload", "Retry", "Skip", "Dismiss", "Not now",
    "Got it", "Continue to the app",
]

# JS：检测可见弹窗按钮
_JS_DETECT_POPUP_BUTTONS = """
() => {
    const targets = ['Reload', 'Retry', 'Skip', 'Dismiss', 'Not now',
                     'Got it', 'Continue to the app'];
    const found = [];
    const buttons = document.querySelectorAll('button, [role="button"]');
    for (const btn of buttons) {
        if (btn.offsetParent === null) continue;
        const text = (btn.innerText || '').trim();
        for (const t of targets) {
            if (text === t) { found.push(t); break; }
        }
    }
    const dialogs = document.querySelectorAll('[role="dialog"], mat-dialog-container');
    for (const d of dialogs) {
        if (d.offsetParent === null) continue;
        const dBtns = d.querySelectorAll('button, [role="button"]');
        for (const btn of dBtns) {
            if (btn.offsetParent === null) continue;
            const text = (btn.innerText || '').trim();
            if (text === 'Close') { found.push('Close'); break; }
        }
    }
    return found;
}
"""

# JS：检测页面错误状态
_JS_DETECT_PAGE_ERRORS = """
() => {
    const bodyText = (document.body && document.body.innerText) || "";
    return {
        appletFailed: bodyText.includes("Failed to initialize applet"),
        concurrentUpdates: bodyText.includes("There are concurrent updates"),
        snapshotFailed: bodyText.includes("Failed to create snapshot")
    };
}
"""

# JS：fetch API Cookie 验证
_JS_VALIDATE_COOKIE = """
async () => {
    const controller = new AbortController();
    const timer = setTimeout(() => controller.abort(), 15000);
    try {
        const resp = await fetch('https://aistudio.google.com/apps', {
            method: 'HEAD',
            redirect: 'manual',
            credentials: 'include',
            cache: 'no-store',
            signal: controller.signal
        });
        return { status: resp.status, type: resp.type };
    } catch(e) {
        return { error: e.message };
    } finally {
        clearTimeout(timer);
    }
}
"""

# Cookie 验证失败状态码
_AUTH_FAILURE_STATUSES = frozenset({401, 403})

# 浏览器断开特征关键词
# 仅保留明确的浏览器级错误，移除了 'connection closed' 等可能匹配网络层错误的宽泛关键词
_BROWSER_FAULT_KEYWORDS = (
    'browser has been closed',
    'browser closed',
)

# 保活循环参数
_KEEPALIVE_INTERVAL = 30          # 保活间隔（秒）
_WS_CHECK_EVERY_N = 3            # 每 N 次循环检查 WS（~90 秒）
_WS_IDLE_ASSIST_THRESHOLD = 2    # IDLE/DISCONNECTED 连续 N 次检测后辅助一次重连
_COOKIE_VALIDATE_CLICKS = 120    # 120 * 30s = 1 小时
_MAX_CONSECUTIVE_ERRORS = 3      # 页面错误连续恢复失败上限
_MODAL_CHECK_INTERVAL = 5        # 遮罩层检查间隔（秒）
_WS_UNKNOWN_REBUILD_THRESHOLD = 10  # UNKNOWN 连续 N 次检测后触发 Context 重建（~15 分钟）
_WS_SUMMARY_INTERVAL = 300       # WS 状态汇总日志间隔（秒）

# =====================================================================
# 安全诊断工具（不抛异常）
# =====================================================================

async def _safe_screenshot(page, path, logger=None):
    """安全截图，失败不抛异常"""
    try:
        await page.screenshot(path=path)
        if logger:
            logger.info(f"已截取屏幕快照: {path}")
    except Exception as e:
        if logger:
            logger.debug(f"截图失败: {e}")


async def _safe_save_html(page, path, logger=None):
    """安全保存页面 HTML，失败不抛异常"""
    try:
        content = await page.content()
        with open(path, 'w', encoding='utf-8') as f:
            f.write(content)
        if logger:
            logger.info(f"已保存页面HTML: {path}")
    except Exception as e:
        if logger:
            logger.debug(f"保存HTML失败: {e}")


# =====================================================================
# 异步路由处理器
# =====================================================================

async def _abort_font_route(route):
    """拦截并终止字体资源请求"""
    await route.abort()


# =====================================================================
# iframe / WS 辅助函数（全部异步）
# =====================================================================

def _get_preview_frame(page):
    """获取 Preview iframe 的 FrameLocator"""
    try:
        return page.frame_locator('iframe[title="Preview"]')
    except Exception:
        return None


async def get_ws_status(page, logger=None) -> str:
    """获取 Preview iframe 内的 WS 连接状态"""
    try:
        frame = _get_preview_frame(page)
        if not frame:
            return "UNKNOWN"
        element = frame.locator(
            'text=/WS:\\s*(CONNECTED|IDLE|CONNECTING|RECONNECTING|DISCONNECTED|ERROR)/i'
        ).first
        if await element.is_visible(timeout=3000):
            text = await element.text_content()
            if text:
                upper = text.upper()
                for status in (
                    "CONNECTED", "IDLE", "CONNECTING",
                    "RECONNECTING", "DISCONNECTED", "ERROR",
                ):
                    if status in upper:
                        return status
        return "UNKNOWN"
    except Exception as e:
        if logger:
            logger.debug(f"获取WS状态出错: {e}")
        return "UNKNOWN"


async def _click_ws_button(page, button_text, logger=None) -> bool:
    """在 Preview iframe 内点击指定的 WS 按钮"""
    try:
        frame = _get_preview_frame(page)
        if not frame:
            return False
        btn = frame.locator(f'button:has-text("{button_text}")')
        if await btn.count() > 0 and await btn.first.is_visible(timeout=3000):
            await btn.first.click(timeout=5000)
            if logger:
                logger.info(f"已点击 {button_text} 按钮")
            await asyncio.sleep(1)
            return True
        if logger:
            logger.warning(f"未找到可见的 {button_text} 按钮")
        return False
    except Exception as e:
        if logger:
            logger.warning(f"点击 {button_text} 按钮失败: {e}")
        return False


async def _wait_for_ws_connected(page, logger=None, timeout=30) -> bool:
    """等待 WS 状态变为 CONNECTED"""
    elapsed = 0
    while elapsed < timeout:
        if await get_ws_status(page, logger) == "CONNECTED":
            return True
        await asyncio.sleep(1)
        elapsed += 1
    return False


async def reconnect_ws(page, logger=None) -> str:
    """
    执行 Disconnect → Connect 重连流程。

    包含页面存活前置检查：如果 Preview iframe 已不存在或页面对象已关闭，
    跳过完整重连流程（避免浪费 ~20 秒和大量无效日志），直接返回 UNKNOWN。
    """
    if logger:
        logger.info("开始执行WS重连流程: Disconnect -> Connect")

    # ── 前置检查：页面/iframe 是否仍可用 ──
    # 如果 iframe 不存在或 page 对象已关闭，后续所有按钮操作和等待都会失败，
    # 提前返回可节省 ~20 秒并避免产生 6~8 条无效日志
    try:
        iframe = page.locator('iframe[title="Preview"]')
        if await iframe.count() == 0:
            if logger:
                logger.warning("Preview iframe 不存在，跳过 WS 重连")
            return "UNKNOWN"
    except Exception as e:
        if logger:
            logger.warning(
                f"WS 重连前置检查失败（页面可能已关闭）: {type(e).__name__}"
            )
        return "UNKNOWN"

    await dismiss_interaction_modal(page, logger)
    await _click_ws_button(page, "Disconnect", logger)
    await asyncio.sleep(2)
    status = await get_ws_status(page, logger)
    if logger:
        logger.info(f"断开后WS状态: {status}")
    await _click_ws_button(page, "Connect", logger)
    await asyncio.sleep(2)
    if await _wait_for_ws_connected(page, logger, timeout=15):
        status = await get_ws_status(page, logger)
        if logger:
            logger.info(f"重连后WS状态: {status}")
        return status
    else:
        status = await get_ws_status(page, logger)
        if logger:
            logger.warning(f"WS重连超时，当前状态: {status}")
        return status


async def dismiss_interaction_modal(page, logger=None) -> bool:
    """检测并通过模拟鼠标移动关闭 interaction-modal 遮罩层"""
    try:
        modal = page.locator('div.interaction-modal')
        if await modal.count() == 0 or not await modal.first.is_visible(timeout=500):
            return False
        if logger:
            logger.info("检测到 interaction-modal 遮罩层，尝试关闭...")
        iframe = page.locator('iframe[title="Preview"]')
        if await iframe.count() > 0:
            box = await iframe.first.bounding_box()
            if box:
                cx = box['x'] + random.randint(50, int(box['width']) - 50)
                cy = box['y'] + random.randint(50, int(box['height']) - 50)
                for _ in range(30):
                    cx = max(box['x'] + 20, min(box['x'] + box['width'] - 20,
                             cx + random.randint(-30, 30)))
                    cy = max(box['y'] + 20, min(box['y'] + box['height'] - 20,
                             cy + random.randint(-20, 20)))
                    await page.mouse.move(cx, cy)
                    await asyncio.sleep(0.05)
                    if await modal.count() == 0 or not await modal.first.is_visible(timeout=100):
                        if logger:
                            logger.info("已成功关闭 interaction-modal 遮罩层")
                        return True
        return False
    except Exception as e:
        if logger:
            logger.debug(f"关闭 interaction-modal 时出错: {e}")
        return False


async def click_in_iframe(page, logger=None) -> bool:
    """在 Preview iframe 安全区域内随机移动并点击一次（保活用）"""
    try:
        iframe = page.locator('iframe[title="Preview"]')
        if await iframe.count() == 0:
            return False
        box = await iframe.first.bounding_box()
        if not box:
            return False
        safe_left = box['x'] + 50
        safe_right = box['x'] + box['width'] - 200
        safe_top = box['y'] + 80
        safe_bottom = box['y'] + box['height'] - 50
        if safe_right <= safe_left or safe_bottom <= safe_top:
            return False
        cx = random.randint(int(safe_left), int(safe_right))
        cy = random.randint(int(safe_top), int(safe_bottom))
        for _ in range(random.randint(1, 2)):
            cx = max(int(safe_left), min(int(safe_right), cx + random.randint(-30, 30)))
            cy = max(int(safe_top), min(int(safe_bottom), cy + random.randint(-20, 20)))
            await page.mouse.move(cx, cy)
            await asyncio.sleep(0.05)
        await page.mouse.click(cx, cy)
        return True
    except Exception as e:
        if logger:
            logger.debug(f"在 iframe 内点击失败: {e}")
        return False


# =====================================================================
# 弹窗处理（异步）
# =====================================================================

async def _click_dialog_close(page, logger=None) -> bool:
    """在对话框容器内查找并点击 Close 按钮"""
    try:
        for selector in ('[role="dialog"]', 'mat-dialog-container'):
            dialog = page.locator(selector)
            if await dialog.count() > 0 and await dialog.first.is_visible():
                close_btn = dialog.first.get_by_role('button', name='Close', exact=True)
                if await close_btn.count() > 0 and await close_btn.first.is_visible():
                    await close_btn.first.click(force=True, timeout=5000)
                    if logger:
                        logger.info("已点击对话框内的 'Close' 按钮")
                    return True
    except Exception:
        pass
    return False


async def handle_popup_dialog(page, logger=None, wait_timeout=8000):
    """初始化阶段弹窗处理（使用 wait_for 主动等待弹窗出现）"""
    if logger:
        logger.info("开始处理弹窗...")
    total_clicks = 0
    try:
        locators = [page.get_by_role('button', name=n) for n in _INIT_POPUP_BUTTONS]
        combined = locators[0]
        for loc in locators[1:]:
            combined = combined.or_(loc)
        try:
            await combined.first.wait_for(state='visible', timeout=wait_timeout)
            if logger:
                logger.info("检测到弹窗")
        except Exception:
            if logger:
                logger.info("未检测到弹窗")
            return
        await asyncio.sleep(1)
        for _ in range(10):
            clicked = False
            for name in _ALL_POPUP_BUTTONS:
                try:
                    btn = page.get_by_role('button', name=name)
                    if await btn.is_visible():
                        await btn.click(force=True, timeout=5000)
                        total_clicks += 1
                        clicked = True
                        if logger:
                            logger.info(f"已点击弹窗按钮: '{name}'")
                        await asyncio.sleep(1)
                except Exception:
                    pass
            if await _click_dialog_close(page, logger):
                total_clicks += 1
                clicked = True
                await asyncio.sleep(1)
            if not clicked:
                break
        if logger:
            if total_clicks > 0:
                logger.info(f"弹窗处理完成, 共点击 {total_clicks} 次")
            else:
                logger.info("弹窗按钮出现但点击失败，将继续执行")
    except Exception as e:
        if logger:
            logger.info(f"处理弹窗时发生意外：{e}，将继续执行...")


async def dismiss_popups_if_visible(page, logger=None) -> list:
    """运行时弹窗扫描：通过 page.evaluate() 一次性检测"""
    clicked = []
    try:
        visible_names = await page.evaluate(_JS_DETECT_POPUP_BUTTONS)
        if not visible_names:
            return []
        if logger:
            logger.info(f"检测到运行时弹窗按钮: {visible_names}")
        for name in _ALL_POPUP_BUTTONS:
            if name not in visible_names:
                continue
            try:
                btn = page.get_by_role('button', name=name)
                if await btn.is_visible():
                    await btn.click(force=True, timeout=5000)
                    clicked.append(name)
                    if logger:
                        logger.info(f"已点击运行时弹窗按钮: '{name}'")
                    await asyncio.sleep(1)
            except Exception:
                pass
        if 'Close' in visible_names:
            if await _click_dialog_close(page, logger):
                clicked.append('Close')
                await asyncio.sleep(1)
    except Exception as e:
        if logger:
            logger.debug(f"运行时弹窗扫描出错: {e}")
    return clicked


# =====================================================================
# 页面错误检测与恢复
# =====================================================================

async def detect_page_errors(page, logger=None):
    """通过 page.evaluate() 检测页面已知错误状态文本"""
    try:
        result = await page.evaluate(_JS_DETECT_PAGE_ERRORS)
        if result.get('appletFailed') or result.get('concurrentUpdates') or result.get('snapshotFailed'):
            return result
        return None
    except Exception as e:
        if logger:
            logger.debug(f"页面错误检测出错: {e}")
        return None


async def _verify_page_after_recovery(page, logger) -> bool:
    """恢复后验证页面状态"""
    try:
        if "accounts.google.com" in page.url:
            logger.error("恢复后页面跳转到Google认证页面")
            return False
        iframe = page.locator('iframe[title="Preview"]')
        try:
            await iframe.first.wait_for(state='visible', timeout=5000)
        except Exception:
            logger.warning("恢复后 Preview iframe 不可见")
            return False
        return True
    except Exception as e:
        logger.warning(f"恢复后状态验证异常: {e}")
        return False


async def attempt_error_recovery(page, logger, error_info) -> bool:
    """根据检测到的错误类型尝试恢复页面"""
    descs = []
    if error_info.get('appletFailed'):
        descs.append("Applet初始化失败")
    if error_info.get('concurrentUpdates'):
        descs.append("并发更新冲突")
    if error_info.get('snapshotFailed'):
        descs.append("快照创建失败")
    logger.warning(f"开始尝试恢复页面错误: {', '.join(descs)}")
    try:
        recovery_clicked = False
        for btn_name in ("Reload", "Retry"):
            try:
                btn = page.get_by_role('button', name=btn_name)
                if await btn.is_visible():
                    await btn.click(force=True, timeout=5000)
                    logger.info(f"已点击恢复按钮: '{btn_name}'")
                    recovery_clicked = True
                    break
            except Exception:
                pass
        if not recovery_clicked:
            logger.info("未找到恢复按钮，执行页面刷新...")
            try:
                await page.reload(wait_until='domcontentloaded', timeout=30000)
            except Exception as e:
                logger.error(f"页面刷新失败: {e}")
                return False
        await asyncio.sleep(5)
        try:
            spinner = page.locator('mat-spinner')
            await spinner.wait_for(state='hidden', timeout=15000)
        except Exception:
            pass
        await dismiss_popups_if_visible(page, logger)
        remaining = await detect_page_errors(page, logger)
        if remaining is None:
            if not await _verify_page_after_recovery(page, logger):
                logger.warning("页面错误已消除，但页面状态异常")
                return False
            logger.info("页面错误已成功恢复")
            return True
        else:
            logger.warning("恢复后仍存在页面错误")
            return False
    except Exception as e:
        logger.error(f"页面错误恢复过程中发生异常: {e}")
        return False


# =====================================================================
# Cookie 两阶段验证（异步）
# =====================================================================

async def validate_cookies(page, logger) -> bool:
    """
    两阶段 Cookie 验证。

    阶段 1（快速路径）：fetch API，零额外内存开销
    阶段 2（确认路径）：仅在认证相关不确定时，新建标签页导航确认

    Returns:
        True=有效或无法确认失效, False=确认失效
    """
    logger.debug("开始Cookie验证...")

    # ── 阶段 1: fetch 快速验证 ──
    is_auth_issue = False
    try:
        result = await page.evaluate(_JS_VALIDATE_COOKIE)
        if result.get('error'):
            err = result['error']
            if 'abort' in err.lower():
                logger.warning("Cookie验证: fetch 请求超时 (15s)")
            else:
                logger.warning(f"Cookie验证: 请求异常 - {err}")
            logger.warning("Cookie验证遇到非认证异常，暂时视为有效")
            return True

        resp_type = result.get('type', '')
        status = result.get('status', 0)

        if 200 <= status < 300:
            logger.info("Cookie验证成功")
            return True

        if resp_type == 'opaqueredirect':
            logger.warning("Cookie验证: 检测到重定向（待确认是否为认证跳转）")
            is_auth_issue = True
        elif status in _AUTH_FAILURE_STATUSES:
            logger.warning(f"Cookie验证: HTTP {status}（待确认是否为Cookie失效）")
            is_auth_issue = True
        else:
            logger.warning(f"Cookie验证: HTTP {status}，服务端异常，暂时视为有效")
            return True

    except Exception as e:
        logger.warning(f"Cookie验证执行异常: {e}，暂时视为有效")
        return True

    # ── 阶段 2: 页面导航确认（仅认证相关） ──
    if is_auth_issue:
        logger.info("fetch 返回认证相关异常，通过页面导航进行二次确认...")
        validation_page = None
        try:
            validation_page = await page.context.new_page()
            await validation_page.goto(
                "https://aistudio.google.com/apps",
                wait_until='domcontentloaded',
                timeout=30000,
            )
            await asyncio.sleep(2)
            final_host = urlparse(validation_page.url).hostname
            if final_host == "accounts.google.com":
                logger.error("Cookie确认失效: 页面已跳转到Google认证页面")
                return False
            logger.info("Cookie确认有效: 未跳转到认证页面")
            return True
        except PlaywrightTimeoutError:
            logger.warning("Cookie导航验证: 页面加载超时，暂时视为有效")
            return True
        except PlaywrightError as e:
            logger.warning(f"Cookie导航验证: 网络错误 - {e}，暂时视为有效")
            return True
        except Exception as e:
            logger.warning(f"Cookie导航验证: 异常 - {e}，暂时视为有效")
            return True
        finally:
            if validation_page:
                try:
                    await validation_page.close()
                except Exception:
                    pass
    return True


# =====================================================================
# BrowserSupervisor — 浏览器监督器
# =====================================================================

class BrowserSupervisor:
    """
    共享浏览器监督器。

    管理唯一的 AsyncCamoufox 浏览器实例和 N 个 BrowserContext 实例协程。
    负责浏览器代际重建、实例错峰启动、Cookie 热更新、Browser 空闲模式、
    关闭协调和健康检查快照。
    """

    def __init__(self, config: RuntimeConfig, initial_accounts: dict,
                 shutdown_event: asyncio.Event, notifier=None):
        """
        初始化浏览器监督器。

        Args:
            config: 运行时配置
            initial_accounts: 初始 Cookie 账号字典 {account_id: CookieAccount}
            shutdown_event: 全局关闭事件
            notifier: 可选的 AlertManager 告警管理器
        """
        self.config = config
        self.shutdown_event = shutdown_event
        self._notifier = notifier

        self.logger = get_logger("supervisor")

        # 浏览器代际与状态
        self.generation = 0
        self._browser = None
        self._intentional_close = False
        self.browser_fault_event = asyncio.Event()

        # Cookie 生命周期事件
        self.cookie_invalid_event = asyncio.Event()   # 某账号 Cookie 确认失效时触发
        self.cookie_update_event = asyncio.Event()     # Cookie 更新已应用时触发（唤醒空闲等待）

        # Cookie 更新操作互斥锁，防止多个后台任务并发修改实例状态
        self._cookie_apply_lock = asyncio.Lock()

        # 实例记录
        self.records: List[InstanceRecord] = []
        self._tasks: Dict[int, asyncio.Task] = {}
        self._next_instance_id = 0

        # Provider Label Cookie 注入状态
        self._provider_label_config = config.provider_label
        self._provider_label_registry: Dict[str, str] = {}  # label_value → account_id
        self._provider_label_injection_failures = 0
        
        # 初始化实例记录
        self._init_records(initial_accounts)

    def _alloc_id(self) -> int:
        """分配唯一的实例编号"""
        iid = self._next_instance_id
        self._next_instance_id += 1
        return iid

    def _find_record(self, account_id: str) -> Optional[InstanceRecord]:
        """按账号 ID 查找实例记录"""
        for r in self.records:
            if r.account_id == account_id:
                return r
        return None

    def _init_records(self, accounts: dict):
        """为每个初始 Cookie 账号创建 InstanceRecord 并注册 Provider Label"""
        for account_id, acct in accounts.items():
            # 生成诊断标签：将非字母数字字符替换为下划线，用于截图文件名
            tag = re.sub(r'[^\w\-]', '_', account_id)
            record = InstanceRecord(
                instance_id=self._alloc_id(),
                account_id=account_id,
                display_name=account_id,
                diagnostic_tag=tag,
                cookies=acct.playwright_cookies,
                cookie_version=acct.version,
            )
            self._register_provider_label(record)
            self.records.append(record)

    def _build_launch_options(self) -> dict:
        """构建 AsyncCamoufox 启动参数"""
        opts = {
            "headless": self.config.headless_mode,
            "block_images": True,
            "block_webrtc": True,
            "i_know_what_im_doing": True,
        }
        if self.config.proxy:
            from utils import parse_proxy_url
            proxy_info = parse_proxy_url(self.config.proxy, self.logger)
            if proxy_info:
                # Playwright 要求凭据使用独立字段，不支持内联 user:pass@ 格式
                proxy_server = f"{proxy_info.type}://{proxy_info.host}:{proxy_info.port}"
                proxy_dict = {
                    "server": proxy_server,
                    "bypass": "localhost, 127.0.0.1",
                }
                if proxy_info.username:
                    proxy_dict["username"] = proxy_info.username
                if proxy_info.password:
                    proxy_dict["password"] = proxy_info.password
                opts["proxy"] = proxy_dict
                self.logger.info(
                    f"使用代理: {mask_proxy_for_logging(self.config.proxy)} "
                    f"(类型: {proxy_info.type.upper()}, "
                    f"认证: {'是' if proxy_info.username else '否'})"
                )
            else:
                self.logger.warning(
                    f"代理 URL 解析失败，将不使用代理: "
                    f"{mask_proxy_for_logging(self.config.proxy)}"
                )

        opts["firefox_user_prefs"] = {
            # ── 1. 禁用 GPU 合成 / WebRender ──
            "layers.acceleration.disabled": True,
            "gfx.webrender.all": False,
            "gfx.webrender.enabled": False,
            # ── 2. 合并渲染进程 ──
            "fission.autostart": False,
            "fission.bfcacheInParent": False,
            "dom.ipc.processCount": 1,
            "dom.ipc.processCount.webIsolated": 1,
            # ── 3. 限制缓存 ──
            "browser.cache.disk.enable": False,
            "browser.cache.memory.capacity": 8192,
            # ── 4. 禁用无用媒体子系统 ──
            "media.navigator.enabled": False,
            "media.autoplay.default": 5,
            "media.eme.enabled": False,
            # ── 5. 减少渲染引擎开销 ──
            "gfx.font_rendering.graphite.enabled": False,
            "image.mem.surfacecache.max_size_kb": 8192,
            # ── 6. 会话/历史瘦身 ──
            "browser.sessionhistory.max_entries": 2,
            "browser.sessionhistory.max_total_viewers": 0,
            "browser.sessionstore.max_tabs_undo": 0,
            "browser.sessionstore.max_windows_undo": 0,
            "browser.sessionstore.resume_from_crash": False,
            # ── 7. 网络层瘦身 ──
            "network.prefetch-next": False,
            "network.dns.disablePrefetch": True,
            "network.http.speculative-parallel-limit": 0,
            "network.buffer.cache.size": 4096,
            "network.buffer.cache.count": 12,
            # ── 8. 禁用无用子系统 ──
            "dom.push.enabled": False,
            "dom.serviceWorkers.enabled": False,
            "dom.webnotifications.enabled": False,
            "accessibility.force_disabled": 1,
            "toolkit.telemetry.enabled": False,
            # ── 9. 降低 UI 开销 ──
            "ui.prefersReducedMotion": 1,
        }
        return opts

    # ── 浏览器断开回调 ──

    def _on_browser_disconnected(self):
        """浏览器连接断开回调"""
        if not self._intentional_close:
            self.logger.warning("浏览器连接意外断开")
            self.browser_fault_event.set()

    # ── 判断是否浏览器级故障 ──

    def _is_browser_fault(self, error: Exception) -> bool:
        """
        检查异常是否表明浏览器级故障。

        判断优先级：
        1. browser_fault_event 已被设置（来自 disconnected 回调或其他 Worker）
        2. 浏览器引用丢失或 is_connected() 返回 False
        3. 错误消息包含明确的浏览器关闭关键词（兜底）
        """
        if self.browser_fault_event.is_set():
            return True
        if self._browser is None:
            return True
        try:
            if not self._browser.is_connected():
                return True
        except Exception:
            return True
        msg = str(error).lower()
        return any(kw in msg for kw in _BROWSER_FAULT_KEYWORDS)

    # ── 检测 Browser 是否可用 ──

    def _is_browser_available(self) -> bool:
        """检查当前共享 Browser 是否存在且已连接"""
        if self._browser is None or self.browser_fault_event.is_set():
            return False
        try:
            return self._browser.is_connected()
        except Exception:
            return False

    # ── 可中断睡眠 ──

    async def _interruptible_sleep(self, seconds, interval=1.0):
        """
        可中断的异步睡眠。

        Returns:
            "shutdown" / "browser_fault" / "completed"
        """
        elapsed = 0.0
        while elapsed < seconds:
            if self.shutdown_event.is_set():
                return "shutdown"
            if self.browser_fault_event.is_set():
                return "browser_fault"
            step = min(interval, seconds - elapsed)
            await asyncio.sleep(step)
            elapsed += step
        return "completed"
    
    @staticmethod
    def _apply_cookie_to_record(record, cookies, version):
        """
        统一更新 record 的 Cookie 数据并清理 pending 状态。

        所有直接更新 Cookie 数据的代码路径（非 RUNNING 状态）必须通过此方法执行，
        确保 pending 状态被正确清理，防止新 Worker 应用过期的 pending Cookie。

        RUNNING 状态的 Worker 通过 cookie_update_pending 标志自行在保活循环中处理，
        不走此方法。
        """
        record.cookies = cookies
        record.cookie_version = version
        record.cookie_update_pending = False
        record.pending_cookies = None
        record.pending_cookie_version = None

    # =================================================================
    # Provider Label Cookie 注入
    # =================================================================

    def _register_provider_label(self, record: InstanceRecord):
        """
        为账号注册 Provider Label 并检测冲突。

        Node 层：从账号文件名派生 Cookie 值，冲突时仅跳过 Node 注入。
        Channel 层：所有账号共享固定 KV，无需注册和冲突检测。
        至少有一层可注入时启用 provider_label_enabled。
        """
        config = self._provider_label_config
        if not config or not config.enabled:
            return

        # ── Node 层注册（需冲突检测） ──
        node_value = None
        if config.node_cookie_names:
            label = build_provider_label_value(record.account_id)
            if label:
                existing = self._provider_label_registry.get(label)
                if existing is not None and existing != record.account_id:
                    self.logger.warning(
                        f"Provider Label Node 冲突：账号 {record.account_id} 与 "
                        f"{existing} 均映射为相同标签；"
                        f"已跳过 {record.account_id} 的 Node 层 Cookie 注入"
                    )
                else:
                    self._provider_label_registry[label] = record.account_id
                    node_value = label

        # 至少有一层可注入才启用
        record.provider_label_value = node_value
        record.provider_label_enabled = (
            node_value is not None or bool(config.channel_cookies)
        )

    async def _inject_provider_label_cookies(self, context, record, log):
        """
        向 BrowserContext 注入 Provider Label Cookie。

        在 Google Cookie 注入之后、Page 创建之前调用。
        注入失败仅记录警告日志，不影响账号的正常运行。
        """
        if not record.provider_label_enabled:
            return

        config = self._provider_label_config
        if not config or not config.enabled:
            return

        cookies = build_provider_label_cookies(
            node_label_value=record.provider_label_value or "",
            node_cookie_names=config.node_cookie_names,
            channel_cookies=config.channel_cookies,
            gateway_domains=config.gateway_domains,
        )
        if not cookies:
            return

        try:
            await context.add_cookies(cookies)
            log.debug(f"已注入 {len(cookies)} 条标识 Cookie")
        except Exception as e:
            self._provider_label_injection_failures += 1
            log.warning(
                f"标识 Cookie 注入失败，已跳过该可选功能，"
                f"不影响账号启动: {type(e).__name__}"
            )

    # =================================================================
    # Cookie 热更新接口
    # =================================================================

    async def apply_cookie_changes(self, changes):
        """
        由 main.py 在检测到 Cookie 变更后调用，将变更应用到实例记录。

        通过 _cookie_apply_lock 互斥锁保证同一时刻只有一个后台任务
        在执行实例状态修改，防止并发创建重复 Worker 或覆盖 Task 引用。

        处理策略（按账号状态分类）：
        - RUNNING：设置 cookie_update_pending 标志，由运行中的 Worker 在保活循环中检测并重建
        - WAITING_COOKIE_UPDATE：直接更新 Cookie，重置状态为 PENDING 并启动新 Worker
        - RETRY_EXHAUSTED：同 WAITING，允许用新 Cookie 重新尝试
        - STARTING / RETRY_BACKOFF / PENDING：取消旧 Worker，更新 Cookie，启动新 Worker
        - 其他状态（BROWSER_RECOVERING 等）：仅更新 Cookie 数据和版本号

        新增账号先检查是否已存在同名记录：
        - 已存在：转为更新处理，避免创建重复 Context
        - 不存在：创建新 InstanceRecord 并启动 Worker

        幂等保护：版本已是最新的账号会被跳过，避免重复重建 Context。
        多个账号的更新按 cookie_context_update_delay 错峰执行，避免 CPU 峰值。

        Args:
            changes: CookieChangeSet，包含 updated 和 added 两个字典
        """
        async with self._cookie_apply_lock:
            delay = self.config.cookie_context_update_delay
            browser = self._browser
            gen = self.generation
            browser_ok = self._is_browser_available()
            count = 0

            # ── 处理新增账号（含去重保护） ──
            for account_id, acct in changes.added.items():
                existing = self._find_record(account_id)
                if existing is not None:
                    # 已有同名记录，转为更新处理，避免创建重复 Context
                    if account_id not in changes.updated:
                        changes.updated[account_id] = acct
                        self.logger.info(
                            f"账号 {account_id} 已存在记录，转为更新处理"
                        )
                    continue

                if count > 0 and delay > 0:
                    await asyncio.sleep(delay)

                tag = re.sub(r'[^\w\-]', '_', account_id)
                record = InstanceRecord(
                    instance_id=self._alloc_id(),
                    account_id=account_id,
                    display_name=account_id,
                    diagnostic_tag=tag,
                    cookies=acct.playwright_cookies,
                    cookie_version=acct.version,
                )
                self._register_provider_label(record)
                self.records.append(record)

                # 如果 Browser 可用，立即启动 Worker
                if browser_ok:
                    record.browser_generation = gen
                    task = asyncio.create_task(
                        self._run_instance_worker(record, browser, gen),
                        name=f"instance-{record.display_name}",
                    )
                    self._tasks[record.instance_id] = task
                    record.task = task
                    self.logger.info(f"新增账号 {account_id} 已启动 Context")

                count += 1

            # ── 处理已有账号更新 ──
            for account_id, acct in changes.updated.items():
                record = self._find_record(account_id)
                if not record:
                    continue

                # ── 幂等判断：版本已是最新时跳过，避免重复重建 ──
                if record.cookie_version == acct.version:
                    continue
                # RUNNING 状态下如果 pending 版本已与要应用的版本相同，也跳过
                if (record.state == InstanceState.RUNNING
                        and record.cookie_update_pending
                        and record.pending_cookie_version == acct.version):
                    continue

                if count > 0 and delay > 0:
                    await asyncio.sleep(delay)

                if record.state == InstanceState.RUNNING:
                    # 正在运行的实例：设置热更新标志，由 Worker 在保活循环中处理
                    record.cookie_update_pending = True
                    record.pending_cookies = acct.playwright_cookies
                    record.pending_cookie_version = acct.version
                    self.logger.info(
                        f"账号 {account_id} Cookie 已标记热更新，"
                        f"等待 Worker 在下次保活循环中重建 Context"
                    )

                elif record.state in _COOKIE_WAITING_STATES:
                    # 等待 Cookie 更新的实例：直接更新并重新启动
                    self._apply_cookie_to_record(
                        record, acct.playwright_cookies, acct.version
                    )
                    record.state = InstanceState.PENDING
                    record.retry_count = 0
                    record.revision += 1

                    if browser_ok:
                        record.browser_generation = gen
                        task = asyncio.create_task(
                            self._run_instance_worker(record, browser, gen),
                            name=f"instance-{record.display_name}",
                        )
                        self._tasks[record.instance_id] = task
                        record.task = task
                        self.logger.info(
                            f"恢复账号 {account_id}: Cookie 已更新，重建 Context"
                        )

                elif record.state == InstanceState.RETRY_EXHAUSTED:
                    # 重试耗尽的实例：新 Cookie 允许重新尝试
                    self._apply_cookie_to_record(
                        record, acct.playwright_cookies, acct.version
                    )
                    record.state = InstanceState.PENDING
                    record.retry_count = 0
                    record.revision += 1

                    if browser_ok:
                        record.browser_generation = gen
                        task = asyncio.create_task(
                            self._run_instance_worker(record, browser, gen),
                            name=f"instance-{record.display_name}",
                        )
                        self._tasks[record.instance_id] = task
                        record.task = task
                        self.logger.info(
                            f"账号 {account_id}: 新 Cookie 到达，从重试耗尽中恢复"
                        )

                elif record.state in _ACTIVE_WORKER_STATES:
                    # 存在活跃 Worker：取消旧 Worker 后使用新 Cookie 启动新 Worker
                    await self._cancel_instance_task(record)
                    self._apply_cookie_to_record(
                        record, acct.playwright_cookies, acct.version
                    )
                    record.state = InstanceState.PENDING
                    record.retry_count = 0
                    record.revision += 1

                    if browser_ok:
                        record.browser_generation = gen
                        task = asyncio.create_task(
                            self._run_instance_worker(record, browser, gen),
                            name=f"instance-{record.display_name}",
                        )
                        self._tasks[record.instance_id] = task
                        record.task = task
                        self.logger.info(
                            f"账号 {account_id}: 已替换活跃 Worker，"
                            f"使用新 Cookie 重建 Context"
                        )

                else:
                    # 其他状态（BROWSER_RECOVERING, STOPPING 等）：仅更新数据
                    self._apply_cookie_to_record(
                        record, acct.playwright_cookies, acct.version
                    )
                    record.revision += 1

                count += 1

            # 通知空闲等待模式可能有新任务
            self.cookie_update_event.set()

    def get_waiting_accounts(self) -> Dict[str, str]:
        """
        返回所有处于等待 Cookie 更新状态的账号及其失效版本号。

        Returns:
            {account_id: failed_cookie_version} 字典
        """
        return {
            r.account_id: r.failed_cookie_version or ""
            for r in self.records
            if r.state == InstanceState.WAITING_COOKIE_UPDATE
        }

    # =================================================================
    # 主运行循环
    # =================================================================

    async def run(self):
        """
        监督器主循环：管理浏览器代际和实例协程任务。

        在所有非终态账号都处于 WAITING_COOKIE_UPDATE 状态时，
        主动关闭 Browser 进入低资源空闲等待模式，
        直到 Cookie 更新事件或关闭信号唤醒。
        """
        from camoufox.async_api import AsyncCamoufox

        self.logger.info("=" * 20 + " Camoufox 实例管理器开始启动 " + "=" * 20)
        self.logger.info(
            f"运行模式: {'server' if self.config.hg_mode else 'standalone'}; "
            f"实例启动间隔: {self.config.instance_start_delay} 秒; "
            f"已注册 {len(self.records)} 个账号"
        )

        launch_options = self._build_launch_options()
        browser_retry = 0
        ensure_dir(logs_dir())

        while not self.shutdown_event.is_set():
            # 筛选可启动的候选实例（排除终态和等待 Cookie 更新的）
            candidates = [
                r for r in self.records
                if r.state not in _TERMINAL_STATES
                and r.state not in _COOKIE_WAITING_STATES
            ]
            waiting = [r for r in self.records if r.state in _COOKIE_WAITING_STATES]

            if not candidates:
                if waiting:
                    # 所有非终态账号都在等待 Cookie 更新
                    # 进入低资源等待模式（不启动 Browser）
                    self.logger.info(
                        f"所有 {len(waiting)} 个账号均在等待 Cookie 更新，"
                        f"进入低资源等待模式（Browser 已释放）"
                    )
                    if self._notifier:
                        await self._notifier.emit(
                            "ALL_ACCOUNTS_WAITING_COOKIE_UPDATE", "CRITICAL",
                            message=f"{len(waiting)} 个账号 Cookie 均已失效，等待更新",
                        )
                    await self._idle_wait()
                    continue  # 唤醒后回到循环顶部重新检查
                else:
                    # 所有实例均已达终态
                    self.logger.info("所有实例均已达终态，监督器退出")
                    break

            self.browser_fault_event.clear()
            self._intentional_close = False
            self.generation += 1
            gen = self.generation

            self.logger.info(
                f"浏览器代际 #{gen} 启动中，"
                f"{len(candidates)} 个实例待启动..."
            )

            try:
                async with AsyncCamoufox(**launch_options) as browser:
                    browser_start_time = _time.monotonic()
                    self._browser = browser
                    browser.on("disconnected", lambda *_: self._on_browser_disconnected())
                    self.logger.info(f"浏览器代际 #{gen} 已启动")

                    try:
                        self._tasks.clear()
                        for i, record in enumerate(candidates):
                            if self.shutdown_event.is_set() or self.browser_fault_event.is_set():
                                break
                            # 防重复：如果该账号已有活跃 Worker
                            # （可能由 apply_cookie_changes 在错峰间隔期间创建），跳过
                            if record.task is not None and not record.task.done():
                                self.logger.debug(
                                    f"账号 {record.display_name} 已有活跃 Worker，"
                                    f"跳过重复创建"
                                )
                                # 将已有任务纳入跟踪，确保 _wait_for_completion 能等待它
                                self._tasks[record.instance_id] = record.task
                                continue
                            record.state = InstanceState.PENDING
                            record.browser_generation = gen
                            task = asyncio.create_task(
                                self._run_instance_worker(record, browser, gen),
                                name=f"instance-{record.display_name}",
                            )
                            self._tasks[record.instance_id] = task
                            record.task = task
                            self.logger.info(
                                f"正在启动第 {i + 1}/{len(candidates)} 个实例 "
                                f"({record.display_name})..."
                            )
                            if i < len(candidates) - 1:
                                result = await self._interruptible_sleep(
                                    self.config.instance_start_delay
                                )
                                if result != "completed":
                                    break

                        done_reason = await self._wait_for_completion()

                        if done_reason == "shutdown":
                            self.logger.info("收到关闭信号，结束监督器主循环")
                            break
                        elif done_reason == "browser_fault":
                            stable_duration = _time.monotonic() - browser_start_time
                            if stable_duration >= _BROWSER_STABLE_SECONDS:
                                browser_retry = 0
                            browser_retry += 1

                            if browser_retry > self.config.max_browser_retries:
                                self.logger.error(
                                    f"浏览器重启次数已达上限 ({self.config.max_browser_retries})，"
                                    f"监督器退出"
                                )
                                if self._notifier:
                                    await self._notifier.emit(
                                        "BROWSER_RESTART_LIMIT_EXCEEDED", "CRITICAL",
                                        message="浏览器重启次数已达上限",
                                    )
                                for r in self.records:
                                    if r.state not in _TERMINAL_STATES and r.state not in _COOKIE_WAITING_STATES:
                                        r.state = InstanceState.FAILED
                                break

                            delay = min(3 * (2 ** (browser_retry - 1)), 60)
                            self.logger.warning(
                                f"浏览器代际 #{gen} 故障 "
                                f"(运行 {stable_duration:.0f}s)，"
                                f"将在 {delay}s 后重建 "
                                f"(重试 {browser_retry}/{self.config.max_browser_retries})"
                            )
                            for r in self.records:
                                if r.state not in _TERMINAL_STATES and r.state not in _COOKIE_WAITING_STATES:
                                    r.state = InstanceState.BROWSER_RECOVERING
                                    r.retry_count = 0

                            # 清除已确认的故障事件，防止打断退避睡眠
                            self.browser_fault_event.clear()

                            wait_result = await self._interruptible_sleep(delay)
                            if wait_result == "shutdown":
                                break
                            continue
                        else:
                            # all_done：检查是否仍有账号等待 Cookie 更新
                            if any(r.state in _COOKIE_WAITING_STATES for r in self.records):
                                self.logger.info(
                                    f"浏览器代际 #{gen} 所有任务已完成，"
                                    f"仍有账号等待 Cookie 更新"
                                )
                                continue  # 回到循环顶部，将进入空闲等待
                            self.logger.info(f"浏览器代际 #{gen} 所有实例任务已完成")
                            break

                    finally:
                        self._intentional_close = True
                        await self._cancel_all_tasks()

            except asyncio.CancelledError:
                raise

            except Exception as e:
                browser_retry += 1
                self.logger.error(f"浏览器代际 #{gen} 启动异常: {e}")
                if browser_retry > self.config.max_browser_retries:
                    self.logger.error("浏览器重启次数已达上限，监督器退出")
                    if self._notifier:
                        await self._notifier.emit(
                            "BROWSER_RESTART_LIMIT_EXCEEDED", "CRITICAL",
                            message="浏览器重启次数已达上限",
                        )
                    for r in self.records:
                        if r.state not in _TERMINAL_STATES and r.state not in _COOKIE_WAITING_STATES:
                            r.state = InstanceState.FAILED
                    break
                delay = min(3 * (2 ** (browser_retry - 1)), 60)
                self.logger.warning(f"将在 {delay}s 后重试浏览器启动...")

                # 清除可能残留的故障事件
                self.browser_fault_event.clear()

                wait_result = await self._interruptible_sleep(delay)
                if wait_result == "shutdown":
                    break
                continue

            finally:
                self._browser = None

        self.logger.info("浏览器实例管理器运行结束")

    # ── 空闲等待模式 ──

    async def _idle_wait(self):
        """
        低资源等待模式：无 Browser 运行，等待 Cookie 更新事件或关闭信号。

        此模式下不占用浏览器内存，仅保留 Python 进程和后台任务。
        每 600 秒输出一次等待状态日志，便于运维观察。
        """
        last_log = _time.time()
        while not self.shutdown_event.is_set():
            # 检查 Cookie 更新事件
            if self.cookie_update_event.is_set():
                self.cookie_update_event.clear()
                self.logger.info("检测到 Cookie 更新，退出等待模式")
                break
            # 定期日志
            now = _time.time()
            if now - last_log >= 600:
                w = sum(1 for r in self.records if r.state in _COOKIE_WAITING_STATES)
                self.logger.info(f"仍在等待 Cookie 更新... ({w} 个账号)")
                last_log = now
            await asyncio.sleep(1)

    # ── 等待任务完成（支持动态新增 Task） ──

    async def _wait_for_completion(self) -> str:
        """
        等待所有实例任务完成，或浏览器故障/关闭信号。

        每轮重新扫描 self._tasks，确保 apply_cookie_changes 动态新增的
        Task 也能被正确等待。每 600 秒输出一次全局状态汇总，
        每 300 秒输出一次 WS 连接状态汇总。

        Returns:
            "shutdown" / "browser_fault" / "all_done"
        """
        last_summary = _time.time()
        last_ws_summary = _time.time()

        while True:
            pending = {tid: t for tid, t in self._tasks.items() if not t.done()}
            if not pending:
                break

            if self.shutdown_event.is_set():
                await self._cancel_all_tasks()
                return "shutdown"
            if self.browser_fault_event.is_set():
                self.logger.warning("检测到浏览器故障，取消剩余实例任务...")
                await self._cancel_all_tasks()
                return "browser_fault"

            # 定期状态汇总（每 600 秒）
            now = _time.time()
            if now - last_summary >= 600:
                snap = self.snapshot()
                self.logger.info(
                    f"状态汇总: "
                    f"{snap['ready_instances']} 运行中, "
                    f"{snap['waiting_cookie_update_instances']} 等待Cookie更新, "
                    f"{snap['terminal_instances']} 终态, "
                    f"Browser: 代际#{self.generation}"
                )
                last_summary = now

            # WS 状态汇总（每 300 秒）
            if now - last_ws_summary >= _WS_SUMMARY_INTERVAL:
                self._log_ws_summary()
                last_ws_summary = now

            done, _ = await asyncio.wait(
                pending.values(), timeout=1.0,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in done:
                for tid, t in list(pending.items()):
                    if t is task:
                        del pending[tid]
                        break
                if task.done() and not task.cancelled():
                    exc = task.exception()
                    if exc:
                        self.logger.error(f"实例任务异常退出: {exc}")

        # 所有任务已结束，最终检查是否有 Worker 在退出前设置了浏览器故障事件
        if self.browser_fault_event.is_set():
            return "browser_fault"

        return "all_done"

    # ── 取消所有任务 ──

    async def _cancel_all_tasks(self):
        """取消并等待所有实例任务完成"""
        for task in self._tasks.values():
            if not task.done():
                task.cancel()
        if self._tasks:
            await asyncio.gather(*self._tasks.values(), return_exceptions=True)
        self._tasks.clear()

    # ── 取消单个实例任务 ──

    async def _cancel_instance_task(self, record: InstanceRecord):
        """
        取消并等待指定实例的当前 Worker 任务，清理任务引用。

        用于 Cookie 热更新时替换活跃 Worker：先取消旧 Worker（等待其 finally
        块完成 Context 清理），然后才启动新 Worker。

        若任务已完成或不存在，此方法为无操作。
        """
        task = self._tasks.get(record.instance_id)
        if task is not None and not task.done():
            task.cancel()
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.pop(record.instance_id, None)
        record.task = None

    # =================================================================
    # 单实例 Worker 协程
    # =================================================================

    async def _run_instance_worker(self, record: InstanceRecord, browser, generation: int):
        """
        单个 Cookie 账号的完整生命周期协程（含 Context 级重试循环）。

        包含 revision 保护：如果 apply_cookie_changes 已经更新了 record 并启动了
        新的 Worker，当前 Worker 检测到 revision 变化后会安全退出，
        避免覆盖新 Worker 的状态或关闭新 Worker 的 Context。
        """
        log = get_logger(record.display_name)
        screenshot_dir = str(logs_dir())
        worker_revision = record.revision

        while not self.shutdown_event.is_set():
            # ── revision 保护：检查是否有更新的 Worker 取代了当前 Worker ──
            if record.revision != worker_revision:
                log.info("检测到更新的 Cookie revision，当前 Worker 退出")
                return

            context = None
            page = None

            try:
                record.state = InstanceState.STARTING
                log.info("正在创建 BrowserContext...")

                context = await browser.new_context()
                await context.route(_FONT_URL_PATTERN, _abort_font_route)
                await context.add_cookies(record.cookies)

                # Provider Label Cookie 注入（可选功能，失败不影响后续流程）
                await self._inject_provider_label_cookies(context, record, log)

                page = await context.new_page()

                record.context = context
                record.page = page
                record.browser_generation = generation

                # 导航与验证
                await self._navigate_and_validate(page, record, log, screenshot_dir)

                # 验证通过，进入保活
                record.state = InstanceState.RUNNING
                log.info("所有验证通过，确认已成功登录")

                # 如果是从 Cookie 失效状态恢复成功，发送恢复通知
                if record.failed_cookie_version:
                    log.info(
                        f"账号已从 Cookie 失效中恢复 "
                        f"(失效版本: {record.failed_cookie_version[:12]}...)"
                    )
                    if self._notifier:
                        await self._notifier.emit(
                            "COOKIE_ACCOUNT_RECOVERED", "INFO",
                            account_id=record.account_id,
                            message="账号 Cookie 已更新，Context 重建成功",
                            alert_key=f"cookie_recovered:{record.account_id}:{record.cookie_version[:12]}",
                        )
                    record.failed_cookie_version = None

                await self._keepalive_loop(page, record, log, screenshot_dir)

                record.retry_count = 0
                return

            except _CookieUpdateSignal:
                # Cookie 热更新信号：关闭旧 Context，立即在下一轮循环重建
                log.info("Cookie 版本已更新，正在重建 Context...")
                await self._safely_close_context(record, context, generation)
                context = None
                page = None
                worker_revision = record.revision  # 同步到新 revision
                record.retry_count = 0
                continue

            except CookieInvalidError:
                # Cookie 确认失效：进入等待更新状态
                if record.revision != worker_revision:
                    return  # 已被新 Worker 取代
                record.failed_cookie_version = record.cookie_version
                record.state = InstanceState.WAITING_COOKIE_UPDATE
                log.error("Cookie 已确认失效，进入等待更新状态")
                # 触发失效事件，通知 main.py 中的恢复检查任务立即执行
                self.cookie_invalid_event.set()
                if self._notifier:
                    await self._notifier.emit(
                        "COOKIE_INVALID_WAITING", "WARNING",
                        account_id=record.account_id,
                        message="Cookie 已确认失效，等待新 Cookie",
                        alert_key=f"cookie_invalid:{record.account_id}:{record.cookie_version[:12]}",
                    )
                return

            except RecoverableInstanceError as e:
                if record.revision != worker_revision:
                    return  # 已被新 Worker 取代
                record.retry_count += 1
                if record.retry_count > self.config.max_instance_retries:
                    record.state = InstanceState.RETRY_EXHAUSTED
                    log.error(
                        f"Context 重试次数已达上限 ({self.config.max_instance_retries})，实例终止"
                    )
                    return

                record.state = InstanceState.RETRY_BACKOFF
                await self._safely_close_context(record, context, generation)
                context = None
                page = None

                delay = min(3 * (2 ** (record.retry_count - 1)), 60)
                log.warning(
                    f"实例可恢复错误 (重试 {record.retry_count}/{self.config.max_instance_retries})，"
                    f"将在 {delay}s 后重建 Context: {e}"
                )

                result = await self._interruptible_sleep(delay)
                if result == "shutdown":
                    record.state = InstanceState.STOPPED
                    return
                if result == "browser_fault":
                    record.state = InstanceState.BROWSER_RECOVERING
                    return
                continue

            except BrowserFaultError:
                if record.revision != worker_revision:
                    return
                record.state = InstanceState.BROWSER_RECOVERING
                # 必须通知监督器触发浏览器重建
                self.browser_fault_event.set()
                log.warning("检测到浏览器级故障，等待监督器重建")
                return

            except asyncio.CancelledError:
                log.info("任务已取消")
                record.state = InstanceState.STOPPING
                raise  # 必须继续传播

            except Exception as e:
                if record.revision != worker_revision:
                    return
                if self._is_browser_fault(e):
                    record.state = InstanceState.BROWSER_RECOVERING
                    # 必须通知监督器触发浏览器重建
                    self.browser_fault_event.set()
                    log.warning(f"检测到浏览器级故障: {e}")
                else:
                    record.state = InstanceState.FAILED
                    log.exception(f"发生未预料的严重错误: {e}")
                return

            finally:
                await self._safely_close_context(record, context, generation)

    # ── Context 安全关闭（带代际保护） ──

    async def _safely_close_context(self, record, context, generation):
        """
        安全关闭 Context。

        始终尝试关闭传入的 Context 对象以防资源泄漏，
        然后在 await 返回后重新验证引用身份——仅当 record 中的引用
        仍然指向本 Worker 持有的 Context 时，才清空记录中的引用。
        """
        if context is None:
            return
        # 始终尝试关闭我们持有的 Context，防止资源泄漏
        try:
            await context.close()
            self.logger.debug(
                f"已关闭 Context: {record.display_name} (代际 #{generation})"
            )
        except Exception as e:
            self.logger.debug(
                f"关闭 Context 时出错: {record.display_name}: {e}"
            )
        # await 返回后重新验证：仅当引用仍属于当前 Worker 时才清空记录
        if record.browser_generation == generation and record.context is context:
            record.context = None
            record.page = None
            record.last_ws_status = "UNKNOWN"

    # =================================================================
    # 导航与页面验证
    # =================================================================

    async def _navigate_and_validate(self, page, record, log, screenshot_dir):
        """
        导航到目标 URL 并验证身份认证状态。

        Raises:
            CookieInvalidError: Cookie 确认失效
            RecoverableInstanceError: 导航超时/网络错误等可重试故障
            BrowserFaultError: 浏览器已断开
        """
        expected_url = self.config.target_url
        tag = record.diagnostic_tag

        # ── 1. 导航 ──
        try:
            log.info(f"正在导航到: {mask_url_for_logging(expected_url)} (超时 90 秒)")
            response = await page.goto(expected_url, wait_until='domcontentloaded', timeout=90000)
            if response:
                log.info(f"导航初步成功，响应状态码: {response.status}")
                if not response.ok:
                    log.warning(f"HTTP 状态码异常: {response.status}")
                    await _safe_screenshot(
                        page, os.path.join(screenshot_dir, f"WARN_http_{response.status}_{tag}.png"), log
                    )
            else:
                log.debug("page.goto 未返回响应对象")

        except PlaywrightTimeoutError:
            log.error(f"导航超时 (>90s): {mask_url_for_logging(expected_url)}")
            await _safe_screenshot(page, os.path.join(screenshot_dir, f"FAIL_timeout_{tag}.png"), log)
            await _safe_save_html(page, os.path.join(screenshot_dir, f"FAIL_timeout_{tag}.html"), log)
            raise RecoverableInstanceError(f"导航超时: {mask_url_for_logging(expected_url)}")

        except PlaywrightError as e:
            error_msg = str(e)
            log.error(f"导航网络错误: {error_msg}")
            if self._is_browser_fault(e):
                raise BrowserFaultError(f"导航时浏览器断开: {error_msg[:100]}")
            await _safe_screenshot(page, os.path.join(screenshot_dir, f"FAIL_network_{tag}.png"), log)
            raise RecoverableInstanceError(f"导航网络错误: {error_msg[:100]}")

        # ── 2. 等待页面初步加载 ──
        log.info("页面初步加载完成，检查认证状态...")
        await asyncio.sleep(2)

        final_url = page.url
        log.info(f"最终URL: {mask_url_for_logging(final_url)}")
        final_parsed = urlparse(final_url)

        # ── 3. 优先检测 Google 认证页面 ──
        if final_parsed.hostname == 'accounts.google.com':
            if 'signin/identifier' in final_parsed.path:
                log.error("检测到Google登录页面，Cookie已完全失效")
            elif 'signin/accountchooser' in final_parsed.path:
                log.error("检测到Google账户选择页面，Cookie已过期")
            else:
                log.error(f"检测到Google认证页面: {final_parsed.path}")
            await _safe_screenshot(page, os.path.join(screenshot_dir, f"FAIL_google_auth_{tag}.png"), log)
            raise CookieInvalidError("重定向到Google认证页面")

        # ── 4. 路径匹配 ──
        expected_path = urlparse(expected_url).path
        final_path = final_parsed.path

        if not (expected_path and expected_path in final_path):
            log.error(
                f"导航到意外URL: 预期 {mask_path_for_logging(expected_path)}, "
                f"实际 {mask_path_for_logging(final_path)}"
            )
            await _safe_screenshot(page, os.path.join(screenshot_dir, f"FAIL_unexpected_url_{tag}.png"), log)
            raise RecoverableInstanceError("导航到意外URL")

        log.info(f"URL验证通过。预期路径: {mask_path_for_logging(expected_path)}")

        # ── 5. 等待 Spinner 消失 ──
        spinner = page.locator('mat-spinner')
        try:
            log.info("正在等待加载指示器消失... (最长 30 秒)")
            await spinner.wait_for(state='hidden', timeout=30000)
            log.info("加载指示器已消失")
        except PlaywrightTimeoutError:
            log.error("加载指示器超时未消失")
            await _safe_screenshot(page, os.path.join(screenshot_dir, f"FAIL_spinner_{tag}.png"), log)
            raise RecoverableInstanceError("加载指示器超时")

        # ── 6. 检查认证错误横幅 ──
        auth_error = page.get_by_text("authentication error", exact=False)
        if await auth_error.is_visible(timeout=2000):
            log.error("检测到认证失败错误横幅")
            await _safe_screenshot(page, os.path.join(screenshot_dir, f"FAIL_auth_error_{tag}.png"), log)
            raise CookieInvalidError("认证错误横幅")

        # ── 7. 检查登录按钮 ──
        log.info("未检测到认证错误，进行最终确认...")
        login_cn = page.get_by_role('button', name='登录')
        login_en = page.get_by_role('button', name='Login')
        if await login_cn.is_visible(timeout=1000) or await login_en.is_visible(timeout=1000):
            log.error("页面仍显示登录按钮，Cookie无效")
            await _safe_screenshot(page, os.path.join(screenshot_dir, f"FAIL_login_btn_{tag}.png"), log)
            raise CookieInvalidError("登录按钮可见")

    # =================================================================
    # 保活主循环
    # =================================================================

    async def _keepalive_loop(self, page, record, log, screenshot_dir):
        """
        保活主循环：弹窗处理 → 页面点击 → WS 监控 → Cookie 验证 → Cookie 热更新检测。

        Raises:
            CookieInvalidError: Cookie 定期验证确认失效
            RecoverableInstanceError: 保活过程中可恢复的异常
            BrowserFaultError: 浏览器级故障
            _CookieUpdateSignal: Cookie 热更新，需要重建 Context
        """
        # ── 保活初始化阶段（纳入可恢复异常边界） ──
        try:
            log.info("已成功到达目标页面")
            await page.click('body')
            await handle_popup_dialog(page, logger=log)
            log.info(f"实例将保持运行状态，每 {_KEEPALIVE_INTERVAL} 秒保活一次")
            await asyncio.sleep(15)
            await handle_popup_dialog(page, logger=log, wait_timeout=3000)
        except (CookieInvalidError, RecoverableInstanceError,
                BrowserFaultError, asyncio.CancelledError):
            raise
        except Exception as e:
            if self._is_browser_fault(e):
                raise BrowserFaultError(f"保活初始化中浏览器断开: {e}")
            raise RecoverableInstanceError(f"保活初始化失败: {e}")

        # 初始 WS 状态
        last_ws_status = await get_ws_status(page, log)
        record.last_ws_status = last_ws_status
        log.info(f"初始WS状态: {last_ws_status}")

        click_counter = 0
        consecutive_error_count = 0
        ws_idle_count = 0
        ws_unknown_count = 0
        ws_assist_done = False

        while True:
            # ── 检查关闭信号 ──
            if self.shutdown_event.is_set():
                log.info("收到关闭信号，退出保活循环")
                record.state = InstanceState.STOPPING
                break

            # ── 检查浏览器故障 ──
            if self.browser_fault_event.is_set():
                raise BrowserFaultError("保活循环中检测到浏览器故障")

            # ── Cookie 热更新检测 ──
            if record.cookie_update_pending:
                log.info("检测到 Cookie 版本更新，准备重建 Context")
                record.cookies = record.pending_cookies
                record.cookie_version = record.pending_cookie_version
                record.cookie_update_pending = False
                record.pending_cookies = None
                record.pending_cookie_version = None
                record.revision += 1
                raise _CookieUpdateSignal()

            try:
                # 1. 遮罩层检测
                await dismiss_interaction_modal(page, log)

                # 2. 运行时弹窗扫描
                clicked_buttons = await dismiss_popups_if_visible(page, log)
                if any(btn in ("Reload", "Retry") for btn in clicked_buttons):
                    log.info("已通过弹窗扫描点击恢复按钮，等待页面重新加载...")
                    await asyncio.sleep(5)
                    try:
                        await page.locator('mat-spinner').wait_for(state='hidden', timeout=15000)
                    except Exception:
                        pass
                    await dismiss_popups_if_visible(page, log)
                    last_ws_status = await get_ws_status(page, log)
                    record.last_ws_status = last_ws_status
                    ws_idle_count = 0
                    ws_unknown_count = 0
                    ws_assist_done = False
                    log.info(f"页面重新加载后WS状态: {last_ws_status}")

                # 3. iframe 保活点击
                await click_in_iframe(page, log)
                click_counter += 1

                # 4. 定期 WS 与错误检查
                if click_counter % _WS_CHECK_EVERY_N == 0:
                    # 4a. WS 状态检查
                    current_ws = await get_ws_status(page, log)
                    if current_ws != last_ws_status:
                        log.info(f"WS状态变更: {last_ws_status} -> {current_ws}")

                    if current_ws == "CONNECTED":
                        # 一切正常，重置所有计数
                        ws_idle_count = 0
                        ws_unknown_count = 0
                        ws_assist_done = False

                    elif current_ws in ("CONNECTING", "RECONNECTING"):
                        # 前端正在自动重连，Python 不介入
                        ws_idle_count = 0
                        ws_unknown_count = 0
                        log.debug(f"前端正在自动重连 (状态: {current_ws})")

                    elif current_ws in ("IDLE", "DISCONNECTED"):
                        # 持续超阈值后辅助一次 Disconnect→Connect，之后不再主动干预
                        ws_unknown_count = 0
                        ws_idle_count += 1
                        if ws_idle_count >= _WS_IDLE_ASSIST_THRESHOLD and not ws_assist_done:
                            log.info(
                                f"WS 持续 {current_ws} 状态 "
                                f"{ws_idle_count} 次检测，"
                                f"辅助执行一次 Disconnect→Connect"
                            )
                            await reconnect_ws(page, log)
                            ws_assist_done = True
                            ws_idle_count = 0

                    elif current_ws == "ERROR":
                        # 前端 WS 出错，scheduleReconnect 会自动接管
                        ws_unknown_count = 0
                        ws_idle_count = 0
                        log.warning("前端 WS 状态为 ERROR，等待前端自动恢复")

                    elif current_ws == "UNKNOWN":
                        # iframe 不可达 — 唯一可能触发 Context 重建的 WS 状态
                        ws_idle_count = 0
                        ws_unknown_count += 1
                        if ws_unknown_count >= _WS_UNKNOWN_REBUILD_THRESHOLD:
                            raise RecoverableInstanceError(
                                f"WS 持续 UNKNOWN 状态 {ws_unknown_count} 次检测 "
                                f"(约 {ws_unknown_count * _WS_CHECK_EVERY_N * _KEEPALIVE_INTERVAL // 60} 分钟)，"
                                f"iframe 可能已损坏，触发 Context 重建"
                            )
                        else:
                            log.warning(
                                f"WS 状态 UNKNOWN "
                                f"({ws_unknown_count}/{_WS_UNKNOWN_REBUILD_THRESHOLD})"
                            )

                    last_ws_status = current_ws
                    record.last_ws_status = current_ws

                    # 4b. 页面错误检测与恢复
                    error_info = await detect_page_errors(page, log)
                    if error_info:
                        recovered = await attempt_error_recovery(page, log, error_info)
                        if recovered:
                            consecutive_error_count = 0
                            last_ws_status = await get_ws_status(page, log)
                            record.last_ws_status = last_ws_status
                            ws_idle_count = 0
                            ws_unknown_count = 0
                            ws_assist_done = False
                        else:
                            consecutive_error_count += 1
                            if consecutive_error_count >= _MAX_CONSECUTIVE_ERRORS:
                                raise RecoverableInstanceError(
                                    f"页面错误连续恢复失败 {_MAX_CONSECUTIVE_ERRORS} 次"
                                )
                            log.warning(
                                f"页面错误恢复失败 "
                                f"({consecutive_error_count}/{_MAX_CONSECUTIVE_ERRORS})"
                            )
                    else:
                        consecutive_error_count = 0

                # 5. Cookie 定期验证
                if click_counter >= _COOKIE_VALIDATE_CLICKS:
                    is_valid = await validate_cookies(page, log)
                    if not is_valid:
                        raise CookieInvalidError("Cookie 定期验证确认失效")
                    click_counter = 0

                # 6. 可中断睡眠（含高频遮罩层检测和 Cookie 更新快速响应）
                for tick in range(_KEEPALIVE_INTERVAL):
                    if self.shutdown_event.is_set():
                        log.info("收到关闭信号，退出保活循环")
                        record.state = InstanceState.STOPPING
                        return
                    if self.browser_fault_event.is_set():
                        raise BrowserFaultError("保活睡眠中检测到浏览器故障")
                    if record.cookie_update_pending:
                        break  # 跳出睡眠，下一轮主循环将处理热更新
                    if tick > 0 and tick % _MODAL_CHECK_INTERVAL == 0:
                        await dismiss_interaction_modal(page, log)
                    await asyncio.sleep(1)

            except (CookieInvalidError, RecoverableInstanceError,
                    BrowserFaultError, _CookieUpdateSignal, asyncio.CancelledError):
                raise

            except Exception as e:
                if self._is_browser_fault(e):
                    raise BrowserFaultError(f"保活循环中浏览器断开: {e}")
                log.error(f"保活循环出错: {e}")
                await _safe_screenshot(
                    page,
                    os.path.join(screenshot_dir, f"FAIL_keepalive_{record.diagnostic_tag}.png"),
                    log,
                )
                raise RecoverableInstanceError(f"保活循环异常: {e}")

    # =================================================================
    # WS 状态汇总日志
    # =================================================================

    def _log_ws_summary(self):
        """
        输出所有 RUNNING 实例的 WS 连接状态汇总。

        格式示例：
        WS 状态汇总: 运行中 3 实例 (2/3 连接) | CONNECTED: 2, RECONNECTING: 1 |
        明细: user1.json[CONNECTED], user2.json[RECONNECTING], user3.json[CONNECTED]
        """
        running = [r for r in self.records if r.state == InstanceState.RUNNING]
        if not running:
            return

        total = len(running)
        connected = sum(1 for r in running if r.last_ws_status == "CONNECTED")

        # 按状态分组计数
        status_counts = {}
        for r in running:
            s = r.last_ws_status
            status_counts[s] = status_counts.get(s, 0) + 1
        status_parts = [f"{s}: {c}" for s, c in sorted(status_counts.items())]

        # 明细列表
        details = [f"{r.display_name}[{r.last_ws_status}]" for r in running]

        self.logger.info(
            f"WS 状态汇总: 运行中 {total} 实例 "
            f"({connected}/{total} 连接) | "
            f"{', '.join(status_parts)} | "
            f"明细: {', '.join(details)}"
        )

    # =================================================================
    # 健康检查快照
    # =================================================================

    def snapshot(self) -> dict:
        """生成当前状态快照，供健康检查端点使用"""
        total = len(self.records)
        ready = sum(1 for r in self.records if r.state == InstanceState.RUNNING)
        terminal = sum(1 for r in self.records if r.state in _TERMINAL_STATES)
        waiting = sum(1 for r in self.records if r.state in _COOKIE_WAITING_STATES)
        # WS 连接状态仅统计 RUNNING 的实例
        connected = sum(
            1 for r in self.records
            if r.state == InstanceState.RUNNING and r.last_ws_status == "CONNECTED"
        )

        return {
            'browser_generation': self.generation,
            'configured_instances': total,
            'browser_instances': total,                        # 向后兼容
            'running_instances': total - terminal - waiting,   # 向后兼容：非终态非等待
            'ready_instances': ready,
            'connected_instances': connected,
            'waiting_cookie_update_instances': waiting,
            'terminal_instances': terminal,
            'provider_label': {
                'enabled': bool(
                    self._provider_label_config
                    and self._provider_label_config.enabled
                ),
                'injection_failures': self._provider_label_injection_failures,
                'labeled_instances': sum(
                    1 for r in self.records if r.provider_label_enabled
                ),
            },            
        }

    # =================================================================
    # 关闭接口
    # =================================================================

    async def shutdown(self):
        """由外部调用的显式关闭方法"""
        self.logger.info("收到关闭请求...")
        self.shutdown_event.set()
        await self._cancel_all_tasks()
        for record in self.records:
            if record.state not in _TERMINAL_STATES:
                record.state = InstanceState.STOPPED
