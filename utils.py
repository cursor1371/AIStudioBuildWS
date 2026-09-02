"""
utils.py — 通用工具函数

包含：
- 项目路径定位（project_root / logs_dir / cookies_dir）
- .env 文件加载
- 环境变量清洗与安全读取
- Headless 模式解析
- URL 路径提取与脱敏
- 代理地址脱敏
- 远程 Cookie URL 脱敏
- 原子文件写入
- 运行时配置数据类
- 目录创建等基础工具
"""

import json
import re
import os
import tempfile
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Optional, Any
from urllib.parse import urlparse


# =====================================================================
# 项目路径
# =====================================================================

@lru_cache(maxsize=1)
def project_root() -> Path:
    """
    返回项目根目录。

    检测策略（按优先级）：
    1. 环境变量 CAMOUFOX_PROJECT_ROOT
    2. 本文件 (utils.py) 所在目录即为项目根（平铺结构）
    """
    env_root = os.getenv("CAMOUFOX_PROJECT_ROOT")
    if env_root:
        return Path(env_root).expanduser().resolve()
    # utils.py 位于项目根目录
    return Path(__file__).resolve().parent


def logs_dir() -> Path:
    """日志与截图存放目录"""
    return project_root() / "logs"


def cookies_dir() -> Path:
    """Cookie JSON 文件存放目录"""
    return project_root() / "cookies"


# =====================================================================
# .env 文件加载
# =====================================================================

def load_env_file():
    """加载 .env 文件（仅在非 Docker 环境且文件存在时）"""
    if os.environ.get("DOCKER_ENV") or os.path.exists("/.dockerenv"):
        return
    try:
        from dotenv import load_dotenv
        env_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        if os.path.exists(env_path):
            load_dotenv(env_path, override=False)
    except ImportError:
        pass


# =====================================================================
# 环境变量工具
# =====================================================================

def clean_env_value(value):
    """清理环境变量值，去除首尾空白；为空或 None 时返回 None"""
    if value is None:
        return None
    stripped = value.strip()
    return stripped or None


def safe_int_env(name, default, minimum=0, maximum=None):
    """
    安全读取整数型环境变量。

    缺失/非法/低于最小值时返回默认值；超过最大值时截断。
    """
    raw = os.getenv(name)
    if raw is None:
        return default
    try:
        value = int(raw.strip())
    except (TypeError, ValueError):
        return default
    if value < minimum:
        return default
    if maximum is not None and value > maximum:
        return maximum
    return value


def parse_headless_mode(headless_setting):
    """
    解析 headless 模式配置。

    Returns:
        True / False / 'virtual'
    """
    s = str(headless_setting).lower()
    if s == 'true':
        return True
    elif s == 'false':
        return False
    return 'virtual'


# =====================================================================
# 文件系统工具
# =====================================================================

def ensure_dir(path):
    """确保目录存在，不存在则递归创建"""
    if isinstance(path, str):
        path = Path(path)
    os.makedirs(path, exist_ok=True)


def atomic_write_json(filepath, data, logger=None):
    """
    原子写入 JSON 数据到文件。

    使用临时文件 + fsync + os.replace 的方式，确保：
    - 其他协程/进程读取时，要么读到旧完整文件，要么读到新完整文件
    - 不会读到写入一半的 JSON 数据

    临时文件名格式为 .cookie-<random>.tmp，不会被 Cookie 扫描逻辑误识别。

    Args:
        filepath: 目标文件路径（字符串或 Path）
        data: 可 JSON 序列化的数据
        logger: 可选的日志记录器
    """
    filepath = str(filepath)
    dirpath = os.path.dirname(filepath)
    fd = None
    tmp_path = None
    try:
        fd, tmp_path = tempfile.mkstemp(dir=dirpath, suffix='.tmp', prefix='.cookie-')
        with os.fdopen(fd, 'w', encoding='utf-8') as f:
            fd = None  # os.fdopen 接管 fd 的生命周期
            json.dump(data, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp_path, filepath)
        tmp_path = None  # replace 成功后无需清理临时文件
    except Exception as e:
        if logger:
            logger.error(f"原子写入文件失败 {os.path.basename(filepath)}: {e}")
        raise
    finally:
        # 清理：如果 os.fdopen 未接管 fd（异常发生在 fdopen 之前）
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        # 清理：如果 os.replace 未执行（异常发生在 replace 之前）
        if tmp_path is not None:
            try:
                os.unlink(tmp_path)
            except OSError:
                pass


# =====================================================================
# URL 工具
# =====================================================================

def extract_url_path(url: str) -> str:
    """提取 URL 的路径 + 查询参数 + 片段部分"""
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        result = parsed.path
        if parsed.query:
            result += '?' + parsed.query
        if parsed.fragment:
            result += '#' + parsed.fragment
        return result
    except Exception:
        return ""


def mask_path_for_logging(path: str) -> str:
    """
    对路径进行脱敏处理。

    脱敏规则（ID 长度 > 8 时，保留头 4 位和尾 4 位，中间用 *** 替换）：
    1. /apps/drive/<ID>  — Google Drive 文件 ID
    2. /apps/<ID>        — AI Studio 应用 ID（UUID 等）
    """
    if not path:
        return ""
    parts = path.split('/')
    # /apps/drive/<ID> — ID 在 parts[3]
    if path.startswith('/apps/drive/') and len(parts) >= 4:
        drive_id = parts[3]
        if len(drive_id) > 8:
            parts[3] = f"{drive_id[:4]}***{drive_id[-4:]}"
            return '/'.join(parts)
    # /apps/<ID> — ID 在 parts[2]，排除 /apps/drive/ 子路径
    elif path.startswith('/apps/') and len(parts) >= 3 and parts[2] != 'drive':
        app_id = parts[2]
        if len(app_id) > 8:
            parts[2] = f"{app_id[:4]}***{app_id[-4:]}"
            return '/'.join(parts)
    return path


def mask_url_for_logging(url: str) -> str:
    """
    对 URL 进行脱敏处理。

    脱敏规则（ID 长度 > 8 时，保留头 4 位和尾 4 位，中间用 *** 替换）：
    1. /apps/drive/<ID>  — Google Drive 文件 ID
    2. /apps/<ID>        — AI Studio 应用 ID（UUID 等）
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        parts = parsed.path.split('/')
        masked_index = -1

        # /apps/drive/<ID> — ID 在 parts[3]
        if parsed.path.startswith('/apps/drive/') and len(parts) >= 4:
            if len(parts[3]) > 8:
                masked_index = 3
        # /apps/<ID> — ID 在 parts[2]
        elif parsed.path.startswith('/apps/') and len(parts) >= 3 and parts[2] != 'drive':
            if len(parts[2]) > 8:
                masked_index = 2

        if masked_index >= 0:
            original = parts[masked_index]
            parts[masked_index] = f"{original[:4]}***{original[-4:]}"
            masked_path = '/'.join(parts)
            result = f"{parsed.scheme}://{parsed.netloc}{masked_path}"
            if parsed.query:
                result += '?' + parsed.query
            if parsed.fragment:
                result += '#' + parsed.fragment
            return result

        return url
    except Exception:
        return url


def mask_proxy_for_logging(proxy_url: str) -> str:
    """
    对代理 URL 进行脱敏处理。

    隐藏用户名和密码，仅保留 scheme://host:port 部分。
    示例：http://user:pass@host:8080 → http://***@host:8080
    """
    if not proxy_url:
        return ""
    try:
        parsed = urlparse(proxy_url)
        if parsed.username or parsed.password:
            # 存在凭据，进行脱敏
            masked = f"{parsed.scheme}://***@{parsed.hostname}"
            if parsed.port:
                masked += f":{parsed.port}"
            return masked
        return proxy_url
    except Exception:
        return "***"


def mask_remote_url_for_logging(url: str) -> str:
    """
    对远程 Cookie URL 进行脱敏处理。

    隐藏路径细节和 query 参数，仅保留 scheme://hostname 和路径摘要。
    避免在日志中泄露 Gist ID、Token 参数等敏感信息。

    示例：
        https://gist.githubusercontent.com/user/abc123def456/raw/cookies.json
        → https://gist.githubusercontent.com/user/abc1***s.json
    """
    if not url:
        return ""
    try:
        parsed = urlparse(url)
        path = parsed.path
        # 对过长路径进行截断脱敏
        if len(path) > 20:
            path = path[:10] + "***" + path[-6:]
        return f"{parsed.scheme}://{parsed.hostname}{path}"
    except Exception:
        return "***"

# =====================================================================
# 代理 URL 解析
# =====================================================================

@dataclass(frozen=True)
class ProxyInfo:
    """
    解析后的代理配置信息（不可变）。

    统一表示 HTTP / SOCKS5 / SOCKS4 代理，
    供远程 Cookie 拉取（aiohttp）和 SMTP 邮件发送（smtplib + PySocks）使用。
    """
    type: str                        # "http" / "socks5" / "socks4"
    host: str                        # 代理主机地址
    port: int                        # 代理端口
    username: Optional[str] = None   # 认证用户名（可选）
    password: Optional[str] = None   # 认证密码（可选）
    raw_url: str = ""                # 原始 URL（供 aiohttp-socks 等库直接使用）


def parse_proxy_url(proxy_url: str, logger=None) -> Optional['ProxyInfo']:
    """
    解析代理 URL 为结构化 ProxyInfo。

    支持的 URL 格式：
    - http://host:port
    - http://user:pass@host:port
    - socks5://host:port
    - socks5://user:pass@host:port
    - socks5h://host:port（DNS 在代理端解析）
    - socks4://host:port

    Args:
        proxy_url: 代理 URL 字符串
        logger: 可选日志记录器

    Returns:
        解析成功返回 ProxyInfo，失败返回 None
    """
    if not proxy_url:
        return None

    try:
        parsed = urlparse(proxy_url)
        scheme = (parsed.scheme or "").lower()

        # 映射 scheme 到代理类型
        type_map = {
            'http': 'http',
            'https': 'http',       # HTTP CONNECT 隧道
            'socks5': 'socks5',
            'socks5h': 'socks5',   # DNS 在代理端解析
            'socks4': 'socks4',
            'socks4a': 'socks4',
        }
        proxy_type = type_map.get(scheme)
        if not proxy_type:
            if logger:
                logger.warning(f"不支持的代理协议: {scheme}，已忽略代理配置")
            return None

        host = parsed.hostname
        port = parsed.port
        if not host or not port:
            if logger:
                logger.warning("代理 URL 缺少主机地址或端口号，已忽略代理配置")
            return None

        return ProxyInfo(
            type=proxy_type,
            host=host,
            port=port,
            username=parsed.username or None,
            password=parsed.password or None,
            raw_url=proxy_url,
        )

    except Exception as e:
        if logger:
            logger.warning(f"代理 URL 解析失败: {e}，已忽略代理配置")
        return None

# =====================================================================
# Provider Label Cookie 注入
# =====================================================================

# Provider Label 常量
_MAX_LABEL_COOKIE_NAMES = 8       # Cookie 名称最大数量
_MAX_LABEL_GATEWAY_DOMAINS = 16   # 网关域名最大数量
_MAX_LABEL_COOKIES_PER_CTX = 64   # 单 Context 注入的 Label Cookie 总数上限
_MAX_LABEL_PREFIX_LEN = 64        # 前缀最大长度
_MAX_LABEL_VALUE_LEN = 256        # 最终 Label 值最大长度

# Cookie 名称校验：仅允许字母、数字、下划线、连字符
_VALID_COOKIE_NAME_RE = re.compile(r'^[A-Za-z0-9_\-]+$')

# 网关域名校验：仅允许字母、数字、点、连字符
_VALID_DOMAIN_RE = re.compile(r'^[a-zA-Z0-9.\-]+$')


@dataclass(frozen=True)
class ProviderLabelConfig:
    """
    Provider Label Cookie 注入配置（不可变）。

    启动时解析一次，后续只读使用。
    支持两层独立注入：
    - Node 层：Cookie 键名可配，键值从账号文件名自动派生
    - Channel 层：固定 Key=Value 对，所有账号共享

    Attributes:
        requested: 用户是否明确设置了 WS_LABEL_COOKIE_INJECTION=true
        enabled: 是否实际启用（所有校验通过且至少一层有配置）
        node_cookie_names: Node 层 Cookie 名称元组
        channel_cookies: Channel 层固定 KV 对元组 ((name, value), ...)
        gateway_domains: 网关域名元组
        cookies_per_context: 每个 BrowserContext 注入的标签 Cookie 总数
        disabled_reason: 禁用原因（仅 requested=True 且 enabled=False 时有值）
    """
    requested: bool = False
    enabled: bool = False
    node_cookie_names: tuple = ()
    channel_cookies: tuple = ()
    gateway_domains: tuple = ()
    cookies_per_context: int = 0
    disabled_reason: str = ""


def normalize_label_segment(raw: str) -> str:
    """
    将字符串规范化为安全的 Label 片段。

    规则：
    1. 去除首尾空白
    2. 连续的非字母数字字符替换为单个下划线
    3. 去除首尾下划线

    示例：
        "account7.json"             → "account7_json"
        "eeeuser51@aa.wuzm.cc.json" → "eeeuser51_aa_wuzm_cc_json"
        "ms 01"                     → "ms_01"
        "hf---prod"                 → "hf_prod"

    Args:
        raw: 原始字符串

    Returns:
        规范化后的安全字符串，可能为空
    """
    s = raw.strip()
    if not s:
        return ""
    s = re.sub(r'[^A-Za-z0-9]+', '_', s)
    s = s.strip('_')
    return s


def build_provider_label_value(account_id: str) -> str:
    """
    从账号文件名派生 Node Cookie 值。

    直接对 account_id 进行规范化，不再拼接前缀。

    Args:
        account_id: 账号文件名（如 "account7.json"）

    Returns:
        规范化后的标识值（如 "account7_json"），
        长度不超过 256 字符；规范化为空时返回空字符串
    """
    norm = normalize_label_segment(account_id)
    if not norm:
        return ""
    if len(norm) > _MAX_LABEL_VALUE_LEN:
        norm = norm[:_MAX_LABEL_VALUE_LEN]
    return norm


def build_provider_label_cookies(
    node_label_value: str,
    node_cookie_names: tuple,
    channel_cookies: tuple,
    gateway_domains: tuple,
) -> list:
    """
    为单个 BrowserContext 构建标识 Cookie 列表。

    Node 层：node_cookie_names × gateway_domains，值统一为 node_label_value。
    Channel 层：channel_cookies × gateway_domains，值各自独立。

    Args:
        node_label_value: Node 层 Cookie 值（如 "account7_json"）；
                          为空时跳过 Node 层
        node_cookie_names: Node 层 Cookie 名称元组
        channel_cookies: Channel 层固定 KV 对元组 ((name, value), ...)
        gateway_domains: 网关域名元组

    Returns:
        Playwright context.add_cookies() 兼容的 Cookie 字典列表
    """
    if not gateway_domains:
        return []
    cookies = []
    # ── Node 层 ──
    if node_label_value:
        for name in node_cookie_names:
            for domain in gateway_domains:
                cookies.append({
                    "name": name,
                    "value": node_label_value,
                    "url": f"https://{domain}/",
                    "sameSite": "None",
                    "secure": True,
                    "httpOnly": True,
                })
    # ── Channel 层 ──
    for ch_name, ch_value in channel_cookies:
        for domain in gateway_domains:
            cookies.append({
                "name": ch_name,
                "value": ch_value,
                "url": f"https://{domain}/",
                "sameSite": "None",
                "secure": True,
                "httpOnly": True,
            })
    return cookies


def parse_provider_label_config(logger=None) -> ProviderLabelConfig:
    """
    从环境变量解析 Provider Label Cookie 注入配置。

    解析并校验以下环境变量：
    - WS_LABEL_COOKIE_INJECTION: 总开关
    - WS_GATEWAY_DOMAINS: 网关域名（逗号分隔）
    - WS_NODE_COOKIE_NAME: Node 层 Cookie 键名（逗号分隔，值从账号文件名派生）
    - WS_CHANNEL_COOKIE: Channel 层固定 KV 对（逗号分隔的 key=value）

    两层独立配置，至少配置一层才启用。

    Args:
        logger: 可选日志记录器

    Returns:
        ProviderLabelConfig 不可变配置对象
    """
    raw_injection = clean_env_value(os.getenv("WS_LABEL_COOKIE_INJECTION")) or ""
    requested = raw_injection.lower() == "true"

    if not requested:
        return ProviderLabelConfig()

    # ── 迁移提示：检测已废弃的环境变量 ──
    if os.getenv("WS_LABEL_PREFIX") and logger:
        logger.warning(
            "环境变量 WS_LABEL_PREFIX 已废弃且不再生效，"
            "如需跨节点统一标识请使用 WS_CHANNEL_COOKIE 替代"
        )

    # ── 解析网关域名（逻辑不变） ──
    raw_domains = clean_env_value(os.getenv("WS_GATEWAY_DOMAINS")) or ""
    gateway_domains = []
    seen_domains = set()
    for domain in raw_domains.split(","):
        domain = domain.strip().lower()
        if not domain:
            continue
        # 逐项校验：拒绝包含协议、路径、端口、认证信息等非法内容
        invalid_reason = None
        if "://" in domain:
            invalid_reason = "包含协议（只需填写域名）"
        elif "/" in domain:
            invalid_reason = "包含路径"
        elif ":" in domain:
            invalid_reason = "包含端口"
        elif "?" in domain or "#" in domain:
            invalid_reason = "包含查询参数或片段"
        elif "@" in domain:
            invalid_reason = "包含认证信息"
        elif not _VALID_DOMAIN_RE.match(domain):
            invalid_reason = "格式不合法"

        if invalid_reason:
            if logger:
                logger.warning(
                    f"Provider Label: 网关域名 '{domain}' {invalid_reason}，已跳过"
                )
            continue
        if domain in seen_domains:
            continue
        seen_domains.add(domain)
        gateway_domains.append(domain)

    if len(gateway_domains) > _MAX_LABEL_GATEWAY_DOMAINS:
        if logger:
            logger.warning(
                f"Provider Label: 网关域名数量 {len(gateway_domains)} 超过上限 "
                f"{_MAX_LABEL_GATEWAY_DOMAINS}，仅使用前 {_MAX_LABEL_GATEWAY_DOMAINS} 个"
            )
        gateway_domains = gateway_domains[:_MAX_LABEL_GATEWAY_DOMAINS]

    if not gateway_domains:
        return ProviderLabelConfig(
            requested=True, disabled_reason="无有效 WS_GATEWAY_DOMAINS"
        )

    # ── Node 层: WS_NODE_COOKIE_NAME（兼容旧版 WS_LABEL_COOKIE_NAME） ──
    raw_node_names = clean_env_value(os.getenv("WS_NODE_COOKIE_NAME")) or ""
    if not raw_node_names:
        old_names = clean_env_value(os.getenv("WS_LABEL_COOKIE_NAME")) or ""
        if old_names:
            raw_node_names = old_names
            if logger:
                logger.warning(
                    "环境变量 WS_LABEL_COOKIE_NAME 已更名为 WS_NODE_COOKIE_NAME，"
                    "请更新配置；本次已自动兼容读取"
                )
    node_cookie_names = []
    if raw_node_names:
        seen_names = set()
        for name in raw_node_names.split(","):
            name = name.strip()
            if not name:
                continue
            if not _VALID_COOKIE_NAME_RE.match(name):
                if logger:
                    logger.warning(
                        f"Provider Label: Node Cookie 名称 '{name}' 包含非法字符，已跳过"
                    )
                continue
            if name in seen_names:
                continue
            seen_names.add(name)
            node_cookie_names.append(name)

        if len(node_cookie_names) > _MAX_LABEL_COOKIE_NAMES:
            if logger:
                logger.warning(
                    f"Provider Label: Node Cookie 名称数量 {len(node_cookie_names)} "
                    f"超过上限 {_MAX_LABEL_COOKIE_NAMES}，"
                    f"仅使用前 {_MAX_LABEL_COOKIE_NAMES} 个"
                )
            node_cookie_names = node_cookie_names[:_MAX_LABEL_COOKIE_NAMES]

    # ── Channel 层: WS_CHANNEL_COOKIE ──
    raw_channel = clean_env_value(os.getenv("WS_CHANNEL_COOKIE")) or ""
    channel_cookies = []
    if raw_channel:
        seen_ch_names = set()
        for pair in raw_channel.split(","):
            pair = pair.strip()
            if not pair:
                continue
            if "=" not in pair:
                if logger:
                    logger.warning(
                        f"Provider Label: Channel Cookie 片段 '{pair}' 缺少等号，已跳过"
                    )
                continue
            name, value = pair.split("=", 1)
            name, value = name.strip(), value.strip()
            if not name:
                continue
            if not _VALID_COOKIE_NAME_RE.match(name):
                if logger:
                    logger.warning(
                        f"Provider Label: Channel Cookie 名称 '{name}' 包含非法字符，已跳过"
                    )
                continue
            if not value:
                if logger:
                    logger.warning(
                        f"Provider Label: Channel Cookie '{name}' 值为空，已跳过"
                    )
                continue
            if len(value) > _MAX_LABEL_VALUE_LEN:
                if logger:
                    logger.warning(
                        f"Provider Label: Channel Cookie '{name}' 值过长，已跳过"
                    )
                continue
            if name in seen_ch_names:
                if logger:
                    logger.warning(
                        f"Provider Label: Channel Cookie 名称 '{name}' 重复，"
                        f"已跳过后续同名条目"
                    )
                continue
            seen_ch_names.add(name)
            channel_cookies.append((name, value))

        if len(channel_cookies) > _MAX_LABEL_COOKIE_NAMES:
            if logger:
                logger.warning(
                    f"Provider Label: Channel Cookie 数量 {len(channel_cookies)} "
                    f"超过上限 {_MAX_LABEL_COOKIE_NAMES}，"
                    f"仅使用前 {_MAX_LABEL_COOKIE_NAMES} 个"
                )
            channel_cookies = channel_cookies[:_MAX_LABEL_COOKIE_NAMES]

    # ── 校验：至少有一层配置 ──
    if not node_cookie_names and not channel_cookies:
        return ProviderLabelConfig(
            requested=True,
            disabled_reason="WS_NODE_COOKIE_NAME 和 WS_CHANNEL_COOKIE 均未配置",
        )

    # ── 总数检查 ──
    total = (len(node_cookie_names) + len(channel_cookies)) * len(gateway_domains)
    if total > _MAX_LABEL_COOKIES_PER_CTX:
        return ProviderLabelConfig(
            requested=True,
            disabled_reason=(
                f"Cookie 总数({len(node_cookie_names)} + {len(channel_cookies)}) × "
                f"网关域名({len(gateway_domains)}) = {total} "
                f"超过上限 {_MAX_LABEL_COOKIES_PER_CTX}"
            ),
        )

    return ProviderLabelConfig(
        requested=True,
        enabled=True,
        node_cookie_names=tuple(node_cookie_names),
        channel_cookies=tuple(channel_cookies),
        gateway_domains=tuple(gateway_domains),
        cookies_per_context=total,
    )

# =====================================================================
# 运行时配置数据类
# =====================================================================

@dataclass
class RuntimeConfig:
    """从环境变量解析出的全局运行时配置"""

    # ── 核心配置 ──
    target_url: str = ""
    headless_mode: Any = 'virtual'       # True / False / 'virtual'
    proxy: Optional[str] = None
    instance_start_delay: int = 30       # BrowserContext 启动错峰间隔（秒）
    max_instance_retries: int = 5        # 单账号 Context 级最大重试
    max_browser_retries: int = 5         # 共享浏览器级最大重启
    hg_mode: bool = False                # 是否启用 aiohttp 健康检查服务
    shutdown_timeout: int = 15           # 关闭等待超时（秒）

    # ── Cookie 远程集中管理 ──
    cookie_remote_url: str = ""          # 远程 Cookie JSON 地址
    cookie_remote_token: str = ""        # 远程访问 Bearer Token
    cookie_remote_timeout: int = 20      # 单次远程拉取超时（秒）
    cookie_refresh_interval: int = 3600  # 常规远程刷新间隔（秒）
    recovery_check_interval: int = 60    # 失效恢复检查间隔（秒）
    cookie_context_update_delay: int = 3 # 多 Context 更新错峰间隔（秒）
    cookie_remote_failure_alert_threshold: int = 3  # 连续拉取失败触发告警阈值

    # ── 邮件通知 ──
    notification_enabled: bool = False   # 是否启用邮件通知
    notification_prefix: str = ""        # 实例前缀标识（区分不同容器）
    notification_from: str = ""          # 发件人地址
    notification_to: str = ""            # 收件人地址（逗号分隔多个）
    notification_cooldown: int = 1800    # 同类告警最小间隔（秒）
    smtp_host: str = "smtp.gmail.com"    # SMTP 服务器地址
    smtp_port: int = 587                 # SMTP 端口
    smtp_user: str = ""                  # SMTP 认证用户名
    smtp_password: str = ""              # SMTP 认证密码（Gmail App Password）

    # ── Provider Label Cookie 注入 ──
    provider_label: Optional[ProviderLabelConfig] = None
