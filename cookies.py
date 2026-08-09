"""
cookies.py — Cookie 生命周期管理

核心职责：
1. Cookie 数据规范化：将 Cookie-Editor JSON 数组、KV 字符串等多种格式
   统一规范化为标准 Cookie-Editor 风格数组
2. 版本管理：基于规范化内容计算 SHA-256 版本号，版本与来源格式、顺序无关
3. 多来源检测与优先级合并：Remote > Env > Local
4. 远程 Cookie JSON 拉取：支持 ETag 条件请求、大小限制、完整格式校验
5. 环境变量 USER_COOKIE_<name> 扫描与账号 ID 映射
6. 本地 cookies/*.json 文件扫描与原子写入
7. 定时刷新与失效恢复变更检测

数据流：
  远程/环境变量 Cookie
      ↓ 规范化 + 校验
  合并（Remote > Env > Local）
      ↓ 版本比较
  原子写入 cookies/<账号>.json
      ↓
  扫描本地文件构建有效快照
      ↓ CookieChangeSet
  通知 BrowserSupervisor 进行 Context 操作

线程安全：
  - bootstrap() 在启动阶段单独调用，不需要锁保护
  - refresh() 和 check_recovery() 通过 _refresh_lock 互斥
  - get_effective() 返回字典浅拷贝，CookieAccount 创建后不可变
"""

import asyncio
import hashlib
import json
import math
import os
import re
from dataclasses import dataclass, field
from typing import Dict, List, Optional

from utils import clean_env_value, cookies_dir, ensure_dir, atomic_write_json, parse_proxy_url

# =====================================================================
# 常量
# =====================================================================

# 远程响应体最大大小（2 MiB），防止异常响应消耗内存
_MAX_REMOTE_SIZE = 2 * 1024 * 1024

# 流式读取块大小（64 KiB）
_STREAM_CHUNK_SIZE = 64 * 1024

# 单次远程快照允许的最大账号数
_MAX_ACCOUNTS = 100

# 单个账号允许的最大 Cookie 条目数
_MAX_COOKIES_PER_ACCOUNT = 500

# 单个 Cookie value 字段最大长度（16 KiB）
_MAX_COOKIE_VALUE_LEN = 16 * 1024

# Cookie 文件名最大长度
_MAX_FILENAME_LEN = 180

# 环境变量名匹配模式：USER_COOKIE_ 后跟至少一个字符
_USER_COOKIE_PATTERN = re.compile(r'^USER_COOKIE_(.+)$')

# 合法的账号文件名模式：允许字母、数字、点、下划线、连字符、@、+，必须以 .json 结尾
_VALID_FILENAME_PATTERN = re.compile(r'^[a-zA-Z0-9._@+\-]+\.json$')

# Cookie-Editor 规范化字段列表（用于文档参考，实际规范化在 _normalize_cookie_entry 中执行）
_NORMALIZE_FIELDS = (
    'name', 'value', 'domain', 'path', 'hostOnly',
    'httpOnly', 'secure', 'sameSite', 'session',
    'expirationDate', 'storeId',
)

# 合法的 sameSite 值集合
_VALID_SAMESITE = frozenset({'no_restriction', 'lax', 'strict', 'unspecified'})


# =====================================================================
# 数据模型
# =====================================================================

@dataclass
class CookieAccount:
    """
    单个账号的 Cookie 数据快照。

    创建后视为不可变对象——更新时创建新的 CookieAccount 实例，
    而非修改现有实例的字段，确保并发读取安全。

    Attributes:
        account_id: 账号唯一标识（即本地文件名，如 "user1.json"）
        normalized_cookies: Cookie-Editor 规范化数组（用于版本计算和落盘）
        playwright_cookies: Playwright 兼容格式数组（用于 BrowserContext 注入）
        version: 基于规范化内容的 SHA-256 版本号
    """
    account_id: str
    normalized_cookies: list
    playwright_cookies: list
    version: str


@dataclass
class CookieChangeSet:
    """
    Cookie 变更集合，描述一次刷新操作产生的变更。

    Attributes:
        updated: 版本发生变化的已有账号 {account_id: CookieAccount}
        added: 新增的账号 {account_id: CookieAccount}
    """
    updated: Dict[str, CookieAccount] = field(default_factory=dict)
    added: Dict[str, CookieAccount] = field(default_factory=dict)

    @property
    def has_changes(self) -> bool:
        """是否存在任何变更"""
        return bool(self.updated or self.added)


# =====================================================================
# 规范化函数
# =====================================================================

def _normalize_samesite(raw) -> str:
    """
    将各类 sameSite 值规范化为统一枚举。

    映射规则：
    - "no_restriction" / "none" → "no_restriction"
    - "lax" → "lax"
    - "strict" → "strict"
    - 其他（包括 None、空字符串、未知值） → "unspecified"

    Args:
        raw: 原始 sameSite 值（任意类型）

    Returns:
        规范化后的 sameSite 字符串
    """
    s = str(raw).lower().strip() if raw else 'unspecified'
    if s in ('no_restriction', 'none'):
        return 'no_restriction'
    if s in ('lax', 'strict'):
        return s
    return 'unspecified'


def _normalize_bool(val, default=False) -> bool:
    """
    将各类布尔值规范化为 Python bool。

    正确处理字符串 "true"/"false"——
    Python 中 bool("false") == True 是已知陷阱，此函数显式处理该情况。

    处理规则：
    - bool 类型：直接返回
    - str "true"（不区分大小写）→ True
    - str "false"（不区分大小写）→ False
    - str 其他值 → 返回 default
    - int / float → 按 Python 标准布尔语义（0 为 False，非零为 True）
    - 其他类型（None、dict、list 等）→ 返回 default

    Args:
        val: 原始值（任意类型）
        default: 无法识别时的默认值

    Returns:
        规范化后的 bool 值
    """
    if isinstance(val, bool):
        return val
    if isinstance(val, str):
        lower = val.lower().strip()
        if lower == 'true':
            return True
        if lower == 'false':
            return False
        return default
    if isinstance(val, (int, float)):
        return bool(val)
    return default


def _normalize_cookie_entry(cookie: dict) -> Optional[dict]:
    """
    将单个 Cookie 条目规范化为 Cookie-Editor 标准格式。

    类型校验规则：
    - name 必须为 str 类型且非空
    - value 必须为 str 类型（拒绝 dict、list、int、float、bool、None 等非字符串类型），
      长度不超过 _MAX_COOKIE_VALUE_LEN
    - domain 必须为 str 类型且非空
    - 布尔字段（httpOnly、secure、hostOnly、session）通过 _normalize_bool 处理，
      正确转换字符串 "true"/"false"
    - expirationDate 必须是有限数字（拒绝 NaN、Infinity）或 None
    - storeId 统一为 None

    Args:
        cookie: 原始 Cookie 字典

    Returns:
        规范化后的 Cookie 字典，校验失败时返回 None
    """
    if not isinstance(cookie, dict):
        return None

    # ── name 必须是字符串类型 ──
    name = cookie.get('name')
    if not isinstance(name, str):
        return None
    name = name.strip()
    if not name:
        return None

    # ── value 必须是字符串类型（拒绝 dict、list、数字、布尔等非字符串类型） ──
    value = cookie.get('value')
    if not isinstance(value, str):
        return None
    if len(value) > _MAX_COOKIE_VALUE_LEN:
        return None

    # ── domain 必须是字符串类型 ──
    domain = cookie.get('domain')
    if not isinstance(domain, str):
        return None
    domain = domain.strip()
    if not domain:
        return None

    # ── 布尔字段使用严格规范化 ──
    session = _normalize_bool(cookie.get('session'), False)

    # ── 过期时间：必须是有限数字，统一为整数秒 ──
    exp = None
    if not session:
        raw_exp = cookie.get('expirationDate')
        if raw_exp is not None:
            try:
                exp_float = float(raw_exp)
                if not math.isfinite(exp_float):
                    return None  # 拒绝 NaN / Infinity / -Infinity
                exp = int(exp_float)
                if exp < 0:
                    exp = None  # 负数视为会话 Cookie
            except (ValueError, TypeError):
                pass  # 无法解析时视为会话 Cookie

    # ── path 校验 ──
    path_val = cookie.get('path', '/')
    if not isinstance(path_val, str) or not path_val:
        path_val = '/'

    # ── 构造规范化结果 ──
    return {
        'name': name,
        'value': value,
        'domain': domain,
        'path': path_val,
        'hostOnly': _normalize_bool(cookie.get('hostOnly'), not domain.startswith('.')),
        'httpOnly': _normalize_bool(cookie.get('httpOnly'), False),
        'secure': _normalize_bool(cookie.get('secure'), False),
        'sameSite': _normalize_samesite(cookie.get('sameSite')),
        'session': session,
        'expirationDate': exp,
        'storeId': None,
    }


def _normalize_kv_string(kv_string: str, logger=None) -> list:
    """
    将 KV 字符串 ("name=value; name2=value2; ...") 规范化为 Cookie-Editor 数组。

    KV 格式不包含完整 Cookie 属性（如 httpOnly、secure、sameSite 等），
    因此使用以下默认值：
    - domain: .google.com（本项目固定用于 Google 服务）
    - path: /
    - secure: True
    - httpOnly: False
    - sameSite: lax
    - session: True（无过期时间）

    Args:
        kv_string: 分号分隔的 "name=value" 字符串
        logger: 可选日志记录器

    Returns:
        规范化后的 Cookie-Editor 数组
    """
    result = []
    for pair in kv_string.split(';'):
        pair = pair.strip()
        if not pair:
            continue
        if '=' not in pair:
            if logger:
                logger.debug("KV Cookie 跳过不含等号的片段")
            continue

        name, value = pair.split('=', 1)
        name, value = name.strip(), value.strip()
        if not name:
            if logger:
                logger.debug("KV Cookie 跳过空名称的片段")
            continue
        if len(value) > _MAX_COOKIE_VALUE_LEN:
            if logger:
                logger.warning(f"KV Cookie '{name}' value 长度超限，已跳过")
            continue

        result.append({
            'name': name,
            'value': value,
            'domain': '.google.com',
            'path': '/',
            'hostOnly': False,
            'httpOnly': False,
            'secure': True,
            'sameSite': 'lax',
            'session': True,
            'expirationDate': None,
            'storeId': None,
        })
    return result


def normalize_raw_cookies(raw_data, logger=None) -> list:
    """
    自动识别 Cookie 数据格式并规范化为 Cookie-Editor 标准数组。

    支持的输入格式：
    1. list  — Cookie-Editor JSON 数组（逐条规范化）
    2. str   — 先尝试 JSON 解析，若为数组则走 JSON 路径；
               否则按 KV 字符串 ("name=value; ...") 处理

    Args:
        raw_data: 原始 Cookie 数据（list 或 str）
        logger: 可选日志记录器

    Returns:
        规范化后的 Cookie-Editor 数组（可能为空列表）
    """
    # ── 字符串输入：尝试 JSON 解析，失败则按 KV 处理 ──
    if isinstance(raw_data, str):
        data = raw_data.strip()
        if not data:
            if logger:
                logger.debug("收到空的 Cookie 字符串")
            return []
        try:
            parsed = json.loads(data)
            if isinstance(parsed, list):
                raw_data = parsed  # 解析成功且为数组，走下方 list 分支
            else:
                if logger:
                    logger.debug("Cookie JSON 解析结果非数组，尝试 KV 格式")
                return _normalize_kv_string(data, logger)
        except json.JSONDecodeError:
            # JSON 解析失败，按 KV 字符串处理
            if logger:
                logger.debug("Cookie 数据非 JSON 格式，按 KV 字符串解析")
            return _normalize_kv_string(data, logger)

    # ── 列表输入：逐条规范化 ──
    if isinstance(raw_data, list):
        result = []
        skipped = 0
        for entry in raw_data:
            normalized = _normalize_cookie_entry(entry)
            if normalized:
                result.append(normalized)
            else:
                skipped += 1
        if skipped > 0 and logger:
            logger.debug(f"Cookie 规范化: {len(result)} 条有效, {skipped} 条跳过")
        return result

    # ── 不支持的类型 ──
    if logger:
        logger.warning(f"不支持的 Cookie 数据类型: {type(raw_data).__name__}")
    return []


# =====================================================================
# 版本计算
# =====================================================================

def compute_cookie_version(normalized_cookies: list) -> str:
    """
    基于规范化 Cookie 内容计算 SHA-256 版本号。

    计算流程：
    1. 将 Cookie 按 (domain, path, name) 三元组稳定排序
    2. 使用紧凑 JSON（无空格、key 排序）序列化
    3. UTF-8 编码后计算 SHA-256

    这保证了：
    - 相同 Cookie 内容无论原始格式（JSON/KV）、列表顺序、JSON 缩进
      都会产生相同的版本号
    - 任何 Cookie name/value/属性变化都会导致不同的版本号

    Args:
        normalized_cookies: 规范化后的 Cookie-Editor 数组

    Returns:
        64 位十六进制 SHA-256 字符串
    """
    sorted_cookies = sorted(
        normalized_cookies,
        key=lambda c: (c.get('domain', ''), c.get('path', ''), c.get('name', ''))
    )
    canonical = json.dumps(
        sorted_cookies,
        sort_keys=True,
        separators=(',', ':'),
        ensure_ascii=False,
    )
    return hashlib.sha256(canonical.encode('utf-8')).hexdigest()


# =====================================================================
# Playwright 格式转换
# =====================================================================

def to_playwright_cookies(normalized_cookies: list) -> list:
    """
    将规范化 Cookie-Editor 数组转换为 Playwright BrowserContext.add_cookies() 兼容格式。

    转换规则：
    - sameSite: no_restriction → "None", strict → "Strict", 其他 → "Lax"
    - expires: 会话 Cookie → -1, 否则取 expirationDate 的整数部分
    - httpOnly/secure: 仅在为 True 时包含（Playwright 默认为 False）

    Args:
        normalized_cookies: 规范化后的 Cookie-Editor 数组

    Returns:
        Playwright 兼容的 Cookie 字典列表
    """
    result = []
    for c in normalized_cookies:
        pw = {
            'name': c['name'],
            'value': c['value'],
            'domain': c['domain'],
            'path': c['path'],
        }

        if c.get('httpOnly'):
            pw['httpOnly'] = True
        if c.get('secure'):
            pw['secure'] = True

        # 过期时间
        if c.get('session') or c.get('expirationDate') is None:
            pw['expires'] = -1
        else:
            pw['expires'] = int(c['expirationDate'])

        # sameSite 映射
        ss = c.get('sameSite', 'unspecified')
        if ss == 'no_restriction':
            pw['sameSite'] = 'None'
        elif ss == 'strict':
            pw['sameSite'] = 'Strict'
        else:
            pw['sameSite'] = 'Lax'

        result.append(pw)
    return result


# =====================================================================
# 账号 ID 工具
# =====================================================================

def _suffix_to_account_id(suffix: str) -> str:
    """
    将环境变量后缀或远程 key 转换为规范化的账号文件名。

    映射规则：
    - "user1"       → "user1.json"
    - "user1.json"  → "user1.json"
    - "user1.JSON"  → "user1.json"（扩展名统一为小写）
    - "1"           → "1.json"（兼容旧格式 USER_COOKIE_1）

    Args:
        suffix: 环境变量去掉 USER_COOKIE_ 前缀后的部分，或远程 JSON 的 key

    Returns:
        规范化的账号文件名（以 .json 结尾）
    """
    s = suffix.strip()
    if s.lower().endswith('.json'):
        # 已有 .json 后缀，统一扩展名为小写
        return s[:-5] + '.json'
    return f"{s}.json"


def _is_valid_account_id(account_id: str) -> bool:
    """
    校验账号文件名是否合法。

    合法条件：
    - 非空且长度不超过 _MAX_FILENAME_LEN
    - 不以 . 开头（排除隐藏文件和 . / ..）
    - 仅包含字母、数字、点、下划线、连字符、@、+
    - 以 .json 结尾

    Args:
        account_id: 待校验的账号文件名

    Returns:
        是否合法
    """
    if not account_id or len(account_id) > _MAX_FILENAME_LEN:
        return False
    if account_id.startswith('.'):
        return False
    return bool(_VALID_FILENAME_PATTERN.match(account_id))


def _build_account(account_id: str, normalized: list) -> Optional[CookieAccount]:
    """
    从规范化 Cookie 数据构建 CookieAccount 对象。

    Args:
        account_id: 账号文件名
        normalized: 规范化后的 Cookie-Editor 数组

    Returns:
        CookieAccount 实例，数据为空时返回 None
    """
    if not normalized:
        return None
    return CookieAccount(
        account_id=account_id,
        normalized_cookies=normalized,
        playwright_cookies=to_playwright_cookies(normalized),
        version=compute_cookie_version(normalized),
    )


# =====================================================================
# Cookie 生命周期管理器
# =====================================================================

class CookieLifecycleManager:
    """
    Cookie 全生命周期管理器。

    职责范围：
    - 检测并扫描所有 Cookie 来源（远程、环境变量、本地文件）
    - 执行优先级合并（Remote > Env > Local），以账号为粒度整体替换
    - 规范化 Cookie 数据并计算版本号
    - 原子写入本地 Cookie 文件
    - 定时远程刷新与 ETag 条件请求
    - 失效恢复变更检测

    不在职责范围内：
    - 不操作 Browser / BrowserContext / Page
    - 不发送邮件（通过 notifier 接口委托）
    - 不管理 WebSocket 连接
    """

    def __init__(self, config, logger, notifier=None):
        """
        初始化 Cookie 生命周期管理器。

        Args:
            config: RuntimeConfig 运行时配置
            logger: 日志记录器（建议使用 get_logger("cookies")）
            notifier: 可选的 AlertManager 告警管理器
        """
        self.config = config
        self.logger = logger
        self._notifier = notifier

        # 有效 Cookie 快照：account_id → CookieAccount
        self._effective: Dict[str, CookieAccount] = {}

        # 最后一次成功校验的远程快照：account_id → normalized_cookies
        # 远程拉取失败时保留此快照，避免降级
        self._remote_snapshot: Dict[str, list] = {}

        # HTTP 条件请求头缓存（仅在远程 JSON 完整校验成功后更新）
        self._remote_etag: Optional[str] = None
        self._remote_last_modified: Optional[str] = None

        # 远程拉取连续失败计数与状态
        self._consecutive_failures = 0
        self._was_failing = False

        # 本轮刷新远程是否可达（用于失效恢复时判断是否允许环境变量绕过远程优先级）
        self._remote_reachable = False

        # 刷新互斥锁，保护 refresh() 和 check_recovery() 不并发执行
        self._refresh_lock = asyncio.Lock()

        # aiohttp 会话（懒初始化，在首次远程请求时创建）
        self._session = None

        # 代理配置（用于远程 Cookie 拉取，复用 CAMOUFOX_PROXY）
        self._proxy_info = parse_proxy_url(config.proxy, logger) if config.proxy else None

        # 环境变量扫描结果签名缓存（用于判断扫描结果是否变化，避免重复 INFO 日志）
        self._last_env_scan_key = None

    # =================================================================
    # 公开 API
    # =================================================================

    async def bootstrap(self) -> Dict[str, CookieAccount]:
        """
        启动时初始化 Cookie 数据。

        执行顺序：
        1. 扫描环境变量并写入本地文件（低优先级）
        2. 拉取远程 Cookie 并写入本地文件（高优先级，会覆盖环境变量写入的同名文件）
        3. 扫描本地文件构建最终有效快照

        保证 BrowserContext 使用最新 Cookie 启动，避免先用旧 Cookie 启动再被远程覆盖。

        Returns:
            有效 Cookie 快照字典 {account_id: CookieAccount}
        """
        ensure_dir(cookies_dir())

        # ── 阶段 1：环境变量 Cookie 写入本地 ──
        env_accounts = self._scan_env()
        if env_accounts:
            self._sync_to_local(env_accounts, source="env")
            self.logger.info(
                f"环境变量 Cookie 同步完成: {len(env_accounts)} 个账号"
            )

        # ── 阶段 2：远程 Cookie 拉取与写入本地（覆盖同名环境变量文件） ──
        if self.config.cookie_remote_url:
            self.logger.info("正在执行首次远程 Cookie 拉取...")
            remote_accounts = await self._fetch_remote(is_initial=True)
            if remote_accounts is not None:
                self._remote_snapshot = remote_accounts
                self._sync_to_local(remote_accounts, source="remote")
                self.logger.info(
                    f"远程 Cookie 同步完成: {len(remote_accounts)} 个账号"
                )
            else:
                self.logger.warning(
                    "首次远程 Cookie 拉取失败，将使用环境变量和本地文件兜底"
                )
        else:
            self.logger.info("未配置远程 Cookie 地址，跳过远程拉取")

        # ── 阶段 3：扫描本地文件构建有效快照 ──
        self._effective = self._build_effective_from_local()

        if self._effective:
            account_list = ', '.join(sorted(self._effective.keys()))
            self.logger.info(
                f"Cookie 初始化完成: {len(self._effective)} 个有效账号 [{account_list}]"
            )
        else:
            self.logger.warning(
                "Cookie 初始化完成: 无有效账号（无远程、环境变量或本地 Cookie 可用）"
            )

        return dict(self._effective)

    async def refresh(self) -> CookieChangeSet:
        """
        执行一次完整的 Cookie 刷新。

        包含远程拉取、环境变量扫描、本地文件重建和变更计算。
        通过 _refresh_lock 保证与 check_recovery() 互斥。

        Returns:
            CookieChangeSet 描述本次刷新产生的变更
        """
        async with self._refresh_lock:
            return await self._do_refresh()

    async def check_recovery(self, waiting_versions: Dict[str, str]) -> CookieChangeSet:
        """
        检查等待恢复的账号是否有新的 Cookie 版本可用。

        执行完整刷新以确保来源数据最新，然后基于当前有效快照直接比较版本，
        而非依赖本轮 ChangeSet。这确保了即使上一轮刷新已更新 effective 但
        对应 Worker 在更新生效前就失效的场景也能被正确捕获。

        对于常规刷新未能恢复的账号，在远程不可达或远程已移除该账号的前提下，
        尝试通过环境变量直接恢复——这实现了设计中的"失效恢复例外"：
        当远程不可达时，环境变量可绕过远程优先级保护参与恢复，
        但不影响正常运行账号的优先级体系。

        Args:
            waiting_versions: {account_id: failed_cookie_version}
                其中 failed_cookie_version 是导致该账号失效的 Cookie 版本号

        Returns:
            CookieChangeSet 仅包含可用于恢复的变更
        """
        async with self._refresh_lock:
            # 执行完整刷新，确保 _effective 反映所有来源的最新状态
            all_changes = await self._do_refresh()

            recovery = CookieChangeSet()

            # ── 阶段 1：基于当前有效快照检查版本变化 ──
            # 只要 effective 中该账号的版本 != 导致失效的版本，即可用于恢复
            for aid, failed_version in waiting_versions.items():
                current = self._effective.get(aid)
                if current is not None and current.version != failed_version:
                    recovery.updated[aid] = current
                    self.logger.info(
                        f"账号 {aid} 发现可用于恢复的新 Cookie 版本 "
                        f"(失效版本: {failed_version[:12]}... → "
                        f"新版本: {current.version[:12]}...)"
                    )

            # ── 阶段 2：对未恢复账号尝试环境变量直接恢复 ──
            # 仅在远程不可达或远程已移除该账号时生效，
            # 防止环境变量在远程可达时越权覆盖
            unrecovered = {
                aid: fv for aid, fv in waiting_versions.items()
                if aid not in recovery.updated
            }
            if unrecovered:
                env_recovered = self._try_env_recovery(unrecovered)
                for aid, acct in env_recovered.items():
                    recovery.updated[aid] = acct

            # 新增账号直接作为恢复变更（来自本轮刷新的 added）
            for aid, acct in all_changes.added.items():
                recovery.added[aid] = acct
                self.logger.info(f"发现新增账号 {aid}，加入恢复变更")

            return recovery

    def get_effective(self) -> Dict[str, CookieAccount]:
        """
        获取当前有效 Cookie 快照的浅拷贝。

        CookieAccount 对象创建后不可变，因此浅拷贝足以保证调用方安全使用。

        Returns:
            {account_id: CookieAccount} 字典
        """
        return dict(self._effective)

    async def close(self):
        """关闭 HTTP 会话，释放网络资源"""
        if self._session and not self._session.closed:
            await self._session.close()
            self._session = None
            self.logger.debug("HTTP 会话已关闭")

    # =================================================================
    # 内部：刷新核心流程
    # =================================================================

    async def _do_refresh(self) -> CookieChangeSet:
        """
        执行一次完整的来源扫描和优先级合并。

        执行顺序（先远程后环境变量，确保优先级正确）：
        1. 拉取远程 Cookie 并同步到本地（高优先级，覆盖同名文件）
        2. 扫描环境变量，跳过远程快照中已有的账号后同步到本地（保护远程优先级）
        3. 从本地文件重建有效快照
        4. 与旧快照比较，计算变更集合

        这一顺序保证了：
        - 远程成功时：远程版本始终写入本地，环境变量被过滤
        - 远程移除账号时：_remote_snapshot 中该账号已不存在，环境变量可立即生效
        - 远程失败时：_remote_snapshot 保持不变，保护正常运行账号不被环境变量覆盖

        Returns:
            CookieChangeSet
        """
        # ── 步骤 1：远程拉取（如已配置） ──
        self._remote_reachable = False
        if self.config.cookie_remote_url:
            remote_accounts = await self._fetch_remote(is_initial=False)
            if remote_accounts is not None:
                self._remote_reachable = True
                self._remote_snapshot = remote_accounts
                self._sync_to_local(remote_accounts, source="remote")

        # ── 步骤 2：环境变量同步 ──
        # 过滤条件使用步骤 1 之后的最新 _remote_snapshot，
        # 确保远程移除的账号能立即被环境变量接管
        env_accounts = self._scan_env()
        if env_accounts:
            env_to_write = {
                aid: cookies for aid, cookies in env_accounts.items()
                if aid not in self._remote_snapshot
            }
            if env_to_write:
                self._sync_to_local(env_to_write, source="env")

        # ── 步骤 3：重建有效快照 ──
        new_effective = self._build_effective_from_local()

        # ── 步骤 4：计算变更 ──
        changes = self._compute_changes(self._effective, new_effective)
        self._effective = new_effective

        if changes.has_changes:
            updated_list = ', '.join(sorted(changes.updated.keys())) or '无'
            added_list = ', '.join(sorted(changes.added.keys())) or '无'
            self.logger.info(
                f"Cookie 变更检测: "
                f"{len(changes.updated)} 个更新 [{updated_list}], "
                f"{len(changes.added)} 个新增 [{added_list}]"
            )

        return changes

    # =================================================================
    # 内部：本地文件操作
    # =================================================================

    def _build_effective_from_local(self) -> Dict[str, CookieAccount]:
        """
        扫描 cookies/ 目录下的 *.json 文件，构建有效 Cookie 快照。

        跳过条件：
        - 非 .json 文件
        - 以 . 开头的隐藏文件或临时文件
        - 文件名不符合合法账号 ID 格式
        - 文件内容为空、JSON 损坏或 Cookie 数据为空

        Returns:
            {account_id: CookieAccount} 字典
        """
        result = {}
        cookie_path = cookies_dir()

        if not os.path.isdir(cookie_path):
            self.logger.warning(f"Cookie 目录不存在: {cookie_path}")
            return result

        for fname in sorted(os.listdir(cookie_path)):
            # 跳过非 .json 文件和隐藏/临时文件
            if not fname.endswith('.json') or fname.startswith('.'):
                continue
            if not _is_valid_account_id(fname):
                self.logger.debug(f"跳过不合法的 Cookie 文件名: {fname}")
                continue

            filepath = cookie_path / fname
            try:
                with open(filepath, 'r', encoding='utf-8') as f:
                    content = f.read().strip()
                if not content:
                    self.logger.debug(f"Cookie 文件 {fname} 为空，跳过")
                    continue

                # JSON 解析失败时，将原始文本作为 KV 字符串传递给 normalize_raw_cookies
                try:
                    data = json.loads(content)
                except json.JSONDecodeError:
                    data = content

                normalized = normalize_raw_cookies(data, self.logger)
                if not normalized:
                    self.logger.warning(
                        f"Cookie 文件 {fname} 规范化后无有效数据，跳过"
                    )
                    continue

                acct = _build_account(fname, normalized)
                if acct:
                    result[fname] = acct
            except Exception as e:
                self.logger.warning(f"加载 Cookie 文件 {fname} 时发生异常: {e}")

        self.logger.debug(f"本地 Cookie 扫描完成: {len(result)} 个有效文件")
        return result

    def _sync_to_local(self, accounts: Dict[str, list], source: str):
        """
        将规范化 Cookie 数据原子写入本地文件。

        仅在以下情况写入：
        - 本地文件不存在（新账号）
        - 本地文件存在但版本不同（数据有更新）

        版本相同时跳过写入，避免不必要的磁盘 I/O。

        Args:
            accounts: {account_id: normalized_cookies_list} 待写入的账号数据
            source: 数据来源标识（"env" 或 "remote"），仅用于日志
        """
        cookie_path = cookies_dir()
        ensure_dir(cookie_path)

        written = 0
        skipped = 0

        for account_id, normalized in accounts.items():
            if not normalized:
                self.logger.debug(
                    f"来源 {source}: 账号 {account_id} 数据为空，跳过写入"
                )
                continue
            if not _is_valid_account_id(account_id):
                self.logger.warning(
                    f"来源 {source}: 账号 ID 不合法 '{account_id}'，跳过写入"
                )
                continue

            new_version = compute_cookie_version(normalized)
            filepath = cookie_path / account_id

            # ── 版本比较：仅在版本不同时写入 ──
            if filepath.exists():
                try:
                    with open(filepath, 'r', encoding='utf-8') as f:
                        existing_content = f.read().strip()
                    # 本地文件也支持 KV 格式读取
                    try:
                        existing_data = json.loads(existing_content)
                    except json.JSONDecodeError:
                        existing_data = existing_content
                    existing_norm = normalize_raw_cookies(existing_data)
                    if existing_norm:
                        existing_version = compute_cookie_version(existing_norm)
                        if existing_version == new_version:
                            skipped += 1
                            continue  # 版本相同，跳过写入
                except Exception:
                    # 文件损坏或不可读，继续覆盖
                    self.logger.debug(
                        f"本地文件 {account_id} 读取异常，将被覆盖"
                    )

            # ── 原子写入 ──
            atomic_write_json(str(filepath), normalized, self.logger)
            written += 1
            self.logger.info(
                f"已更新本地 Cookie 文件: {account_id} "
                f"(来源: {source}, 版本: {new_version[:12]}...)"
            )

        if written > 0 or skipped > 0:
            self.logger.debug(
                f"本地文件同步完成 (来源: {source}): "
                f"{written} 个写入, {skipped} 个版本相同跳过"
            )

    @staticmethod
    def _compute_changes(old: Dict[str, CookieAccount],
                         new: Dict[str, CookieAccount]) -> CookieChangeSet:
        """
        比较新旧快照，计算变更集合。

        - 新快照中存在但旧快照不存在的账号 → added
        - 新快照中版本与旧快照不同的账号 → updated
        - 旧快照中存在但新快照不存在的账号 → 不处理（不主动移除）

        Args:
            old: 旧的有效快照
            new: 新的有效快照

        Returns:
            CookieChangeSet
        """
        changes = CookieChangeSet()
        for aid, acct in new.items():
            if aid not in old:
                changes.added[aid] = acct
            elif acct.version != old[aid].version:
                changes.updated[aid] = acct
        return changes

    # =================================================================
    # 内部：环境变量扫描
    # =================================================================

    def _scan_env(self) -> Dict[str, list]:
        """
        扫描所有 USER_COOKIE_<suffix> 环境变量。

        映射规则：
        - USER_COOKIE_1       → 账号 ID "1.json"
        - USER_COOKIE_user1   → 账号 ID "user1.json"
        - USER_COOKIE_team_a  → 账号 ID "team_a.json"

        去重策略：
        - 同一个规范化后的账号 ID 仅接受首个匹配的环境变量
        - 重复映射时记录警告日志

        Returns:
            {account_id: normalized_cookies_list} 字典
        """
        result: Dict[str, list] = {}
        seen_ids: Dict[str, str] = {}  # account_id → env_var_name（去重追踪）

        for key, value in sorted(os.environ.items()):
            m = _USER_COOKIE_PATTERN.match(key)
            if not m:
                continue

            suffix = m.group(1)
            account_id = _suffix_to_account_id(suffix)

            if not _is_valid_account_id(account_id):
                self.logger.warning(
                    f"环境变量 {key} 映射的账号 ID 不合法: '{account_id}'，已跳过"
                )
                continue

            cleaned = clean_env_value(value)
            if not cleaned:
                self.logger.debug(
                    f"环境变量 {key} 值为空，已跳过"
                )
                continue

            # 去重：同一账号 ID 仅接受首个环境变量
            if account_id in seen_ids:
                self.logger.warning(
                    f"环境变量 {key} 与 {seen_ids[account_id]} "
                    f"均映射到账号 '{account_id}'，后者已被跳过"
                )
                continue
            seen_ids[account_id] = key

            try:
                normalized = normalize_raw_cookies(cleaned, self.logger)
                if normalized:
                    result[account_id] = normalized
                    self.logger.debug(
                        f"环境变量 {key} → {account_id}: "
                        f"{len(normalized)} 条 Cookie"
                    )
                else:
                    self.logger.warning(
                        f"环境变量 {key} Cookie 数据规范化后为空，已跳过"
                    )
            except Exception as e:
                self.logger.warning(
                    f"环境变量 {key} Cookie 解析失败: {e}"
                )

        # 日志优化：仅在扫描结果变化时使用 INFO，避免 recovery 循环中每分钟重复输出
        current_key = tuple(sorted(result.keys())) if result else ()
        if current_key != self._last_env_scan_key:
            # 结果有变化（含首次调用）
            self._last_env_scan_key = current_key
            if result:
                self.logger.info(
                    f"环境变量扫描完成: {len(result)} 个有效 Cookie 来源 "
                    f"[{', '.join(sorted(result.keys()))}]"
                )
            else:
                self.logger.info("环境变量扫描完成: 未发现 USER_COOKIE_* 变量")
        else:
            # 结果与上次相同
            self.logger.debug(
                f"环境变量扫描完成: {len(result)} 个来源（与上次相同）"
                if result else "环境变量扫描完成: 无 USER_COOKIE_* 变量"
            )

        return result

    # =================================================================
    # 内部：失效恢复环境变量直接恢复
    # =================================================================

    def _try_env_recovery(self, unrecovered: Dict[str, str]) -> Dict[str, CookieAccount]:
        """
        对常规刷新未能恢复的失效账号，尝试通过环境变量直接恢复。

        该方法实现了设计中的"失效恢复例外"：

        正常运行时，远程优先级最高——环境变量中即使存在同名账号，也不会覆盖
        远程版本（由 _do_refresh 中的 _remote_snapshot 过滤保证）。

        但当某账号已被浏览器确认 Cookie 失效时，若远程不可达（无法证明远程版本
        仍是有效权威），则允许环境变量绕过远程优先级保护参与恢复。

        绕过条件（必须同时满足）：
        1. 远程本轮不可达（_remote_reachable == False），
           或远程可达但已移除该账号（aid not in _remote_snapshot）
        2. 环境变量存在该账号的 Cookie 数据
        3. 环境变量版本不同于导致失效的版本（避免用失效数据恢复）
        4. 环境变量版本不同于当前 effective 版本（避免无效重建）

        注意：当远程可达且仍持有该账号时（即使版本与失效版本相同），
        不允许环境变量覆盖——应等待远程管理员更新 Cookie。

        Args:
            unrecovered: {account_id: failed_cookie_version}
                常规刷新后仍未恢复的账号及其失效版本号

        Returns:
            {account_id: CookieAccount} 可用于恢复的账号字典
        """
        result: Dict[str, CookieAccount] = {}

        # 重新扫描环境变量（开销极小，仅遍历 os.environ）
        env_accounts = self._scan_env()
        if not env_accounts:
            return result

        for aid, failed_version in unrecovered.items():
            # ── 条件 1：远程不可达或远程已移除该账号 ──
            if self._remote_reachable and aid in self._remote_snapshot:
                # 远程可达且持有该账号 → 等待远程管理员更新，不允许环境变量越权
                self.logger.debug(
                    f"账号 {aid}: 远程可达且持有该账号，"
                    f"跳过环境变量恢复（等待远程更新）"
                )
                continue

            # ── 条件 2：环境变量存在该账号 ──
            if aid not in env_accounts:
                continue

            normalized = env_accounts[aid]
            version = compute_cookie_version(normalized)

            # ── 条件 3：版本不同于失效版本 ──
            if version == failed_version:
                self.logger.debug(
                    f"账号 {aid}: 环境变量版本与失效版本相同，跳过"
                )
                continue

            # ── 条件 4：版本不同于当前 effective ──
            current = self._effective.get(aid)
            if current and current.version == version:
                continue

            # ── 绕过远程优先级，直接写入本地并恢复 ──
            cookie_path = cookies_dir() / aid
            atomic_write_json(str(cookie_path), normalized, self.logger)
            self.logger.info(
                f"失效账号 {aid} 通过环境变量恢复: "
                f"失效版本 {failed_version[:12]}... → "
                f"新版本 {version[:12]}..."
            )

            acct = _build_account(aid, normalized)
            if acct:
                result[aid] = acct
                # 同步更新 effective 快照
                self._effective[aid] = acct

        return result

    # =================================================================
    # 内部：远程拉取
    # =================================================================

    def _get_session(self):
        """
        获取或创建 aiohttp HTTP 会话。

        使用懒初始化策略，仅在首次远程请求时创建会话。
        会话被 close() 关闭后，下次调用会重新创建。
        代理策略：
        - HTTP 代理：使用 aiohttp 原生 proxy 参数（per-request，在 _fetch_remote 中传递）
        - SOCKS 代理：使用 aiohttp-socks ProxyConnector（per-session）
        - aiohttp-socks 未安装时：降级为直连并输出警告
        Returns:
            aiohttp.ClientSession 实例
        """
        if self._session is None or self._session.closed:
            import aiohttp
            connector = None
            # SOCKS 代理需要通过 aiohttp-socks 的 ProxyConnector 处理
            if self._proxy_info and self._proxy_info.type in ('socks5', 'socks4'):
                try:
                    from aiohttp_socks import ProxyConnector
                    connector = ProxyConnector.from_url(self._proxy_info.raw_url)
                    self.logger.debug(
                        f"远程 Cookie 拉取使用 {self._proxy_info.type.upper()} 代理"
                    )
                except ImportError:
                    self.logger.warning(
                        "SOCKS 代理需要 aiohttp-socks 库，未安装，远程 Cookie 拉取回退为直连"
                    )
            self._session = aiohttp.ClientSession(connector=connector)
        return self._session

    async def _fetch_remote(self, is_initial=False) -> Optional[Dict[str, list]]:
        """
        拉取远程 Cookie JSON 并进行完整校验。

        请求特性：
        - 支持 ETag / Last-Modified 条件请求（HTTP 304 表示内容未变化）
        - 支持 Bearer Token 认证
        - 禁用 HTTP 重定向，要求配置最终 Raw URL
        - 先检查 Content-Length 头，再流式读取，响应体大小限制为 _MAX_REMOTE_SIZE
        - JSON 格式、账号数量、Cookie 数量、字段合法性全面校验
        - ETag / Last-Modified 仅在完整校验成功后才更新缓存

        校验失败策略：
        - 任何一个账号的 Cookie 数据格式非法 → 整个远程快照被拒绝
        - 保留上一次成功的远程快照，不降级

        Args:
            is_initial: 是否为首次启动时的拉取（影响告警类型）

        Returns:
            成功时返回 {account_id: normalized_cookies_list}
            失败时返回 None（保留旧远程快照不变）
        """
        import aiohttp
        from utils import mask_remote_url_for_logging

        url = self.config.cookie_remote_url
        if not url:
            return None

        # ── 构造请求头 ──
        headers = {'Accept': 'application/json'}
        if self.config.cookie_remote_token:
            headers['Authorization'] = f'Bearer {self.config.cookie_remote_token}'
        if self._remote_etag:
            headers['If-None-Match'] = self._remote_etag
        if self._remote_last_modified:
            headers['If-Modified-Since'] = self._remote_last_modified

        masked_url = mask_remote_url_for_logging(url)

        try:
            timeout = aiohttp.ClientTimeout(total=self.config.cookie_remote_timeout)
            # HTTP 代理通过 aiohttp 原生 proxy 参数传递；SOCKS 代理已在 Session Connector 层处理
            http_proxy = (
                self._proxy_info.raw_url
                if self._proxy_info and self._proxy_info.type == 'http'
                else None
            )
            async with self._get_session().get(
                url, headers=headers, timeout=timeout,
                allow_redirects=False, proxy=http_proxy
            ) as resp:

                # ── HTTP 304: 内容未变化 ──
                if resp.status == 304:
                    await self._on_remote_success()
                    self.logger.debug(
                        f"远程 Cookie 未变化 (HTTP 304) [{masked_url}]"
                    )
                    return self._remote_snapshot

                # ── 非 200 状态码 ──
                if resp.status != 200:
                    await self._on_remote_failure(
                        f"HTTP 状态码 {resp.status}", is_initial, masked_url
                    )
                    return None

                # ── Content-Length 预检查 ──
                content_length = resp.content_length
                if content_length is not None and content_length > _MAX_REMOTE_SIZE:
                    await self._on_remote_failure(
                        f"Content-Length {content_length} 字节超过限制 "
                        f"({_MAX_REMOTE_SIZE} 字节)",
                        is_initial, masked_url,
                    )
                    return None

                # ── 流式读取响应体（限制内存峰值） ──
                chunks = []
                total_size = 0
                while True:
                    chunk = await resp.content.read(_STREAM_CHUNK_SIZE)
                    if not chunk:
                        break
                    total_size += len(chunk)
                    if total_size > _MAX_REMOTE_SIZE:
                        await self._on_remote_failure(
                            f"响应体大小超过限制 ({_MAX_REMOTE_SIZE} 字节)",
                            is_initial, masked_url,
                        )
                        return None
                    chunks.append(chunk)
                body = b''.join(chunks)

                # 暂存响应头，校验成功后才写入缓存
                pending_etag = resp.headers.get('ETag')
                pending_last_modified = resp.headers.get('Last-Modified')

                # ── JSON 解析 ──
                try:
                    data = json.loads(body.decode('utf-8'))
                except (json.JSONDecodeError, UnicodeDecodeError) as e:
                    await self._on_remote_failure(
                        f"JSON 解析失败: {e}", is_initial, masked_url
                    )
                    return None

                # ── 结构与内容校验 ──
                validated = self._validate_remote_payload(data)
                if validated is None:
                    # 远程数据结构非法：发送独立的 CRITICAL 告警
                    if self._notifier:
                        await self._notifier.emit(
                            "REMOTE_PAYLOAD_INVALID", "CRITICAL",
                            message="远程 Cookie JSON 格式校验失败",
                        )
                    await self._on_remote_failure(
                        "远程 JSON 格式校验失败（详见上方日志）",
                        is_initial, masked_url,
                    )
                    return None

                # ── 校验成功：更新 ETag 缓存 ──
                self._remote_etag = pending_etag
                self._remote_last_modified = pending_last_modified

                await self._on_remote_success()
                self.logger.info(
                    f"远程 Cookie 拉取成功: {len(validated)} 个账号 "
                    f"[{', '.join(sorted(validated.keys()))}] [{masked_url}]"
                )
                return validated

        except asyncio.CancelledError:
            raise
        except aiohttp.ClientError as e:
            await self._on_remote_failure(
                f"HTTP 客户端错误: {type(e).__name__}", is_initial, masked_url
            )
            return None
        except asyncio.TimeoutError:
            await self._on_remote_failure(
                f"请求超时 ({self.config.cookie_remote_timeout}s)",
                is_initial, masked_url,
            )
            return None
        except Exception as e:
            await self._on_remote_failure(
                f"未预期异常: {type(e).__name__}",
                is_initial, masked_url,
            )
            return None

    def _validate_remote_payload(self, data) -> Optional[Dict[str, list]]:
        """
        校验并规范化远程 JSON 负载。

        采用两级校验策略：

        结构级校验（整体拒绝）：
        - 必须是非空 dict
        - 账号数量不超过 _MAX_ACCOUNTS
        - 不同 key 规范化后不得产生相同的账号 ID（防歧义）

        账号级校验（跳过单个账号，保留其他正常账号）：
        - 单个 key 规范化后的账号 ID 不合法 → 跳过
        - 单个账号的 Cookie 数据规范化后为空 → 跳过
        - 单个账号 Cookie 数量超过 _MAX_COOKIES_PER_ACCOUNT → 跳过
        - 单个账号 Cookie 规范化过程抛异常 → 跳过

        Args:
            data: 远程响应解析后的 JSON 数据

        Returns:
            校验通过时返回 {account_id: normalized_cookies_list}
            （可能比原始数据少部分异常账号）
            结构级校验失败时返回 None
        """
        # ── 结构级校验：整体拒绝 ──

        if not isinstance(data, dict):
            self.logger.error(
                f"远程 JSON 格式错误: 期望对象(dict)，"
                f"实际为 {type(data).__name__}"
            )
            return None

        if not data:
            self.logger.error("远程 JSON 为空对象，已拒绝")
            return None

        if len(data) > _MAX_ACCOUNTS:
            self.logger.error(
                f"远程账号数量 {len(data)} 超过上限 {_MAX_ACCOUNTS}，已拒绝"
            )
            return None

        # ── 重复检测：先扫描所有 key 的规范化结果，发现歧义则整体拒绝 ──
        id_to_keys: Dict[str, List[str]] = {}
        for key in data:
            account_id = _suffix_to_account_id(key)
            id_to_keys.setdefault(account_id, []).append(key)

        for account_id, keys in id_to_keys.items():
            if len(keys) > 1:
                self.logger.error(
                    f"远程 JSON 中多个 key {keys} 规范化为同一账号 ID "
                    f"'{account_id}'（存在歧义），整体拒绝"
                )
                return None

        # ── 账号级校验：逐个处理，异常账号跳过 ──

        result = {}
        skipped = []

        for key, value in data.items():
            account_id = _suffix_to_account_id(key)

            # 文件名合法性
            if not _is_valid_account_id(account_id):
                skipped.append(key)
                self.logger.warning(
                    f"远程账号 key '{key}' 规范化后的 ID "
                    f"'{account_id}' 不合法，已跳过该账号"
                )
                continue

            # Cookie 数据规范化
            try:
                normalized = normalize_raw_cookies(value, self.logger)
            except Exception as e:
                skipped.append(key)
                self.logger.warning(
                    f"远程账号 {account_id} Cookie 规范化异常: {e}，"
                    f"已跳过该账号"
                )
                continue

            if not normalized:
                skipped.append(key)
                self.logger.warning(
                    f"远程账号 {account_id} Cookie 数据规范化后为空，"
                    f"已跳过该账号"
                )
                continue

            # Cookie 数量上限
            if len(normalized) > _MAX_COOKIES_PER_ACCOUNT:
                skipped.append(key)
                self.logger.warning(
                    f"远程账号 {account_id} Cookie 数量 {len(normalized)} "
                    f"超过上限 {_MAX_COOKIES_PER_ACCOUNT}，已跳过该账号"
                )
                continue

            result[account_id] = normalized

        # ── 汇总日志 ──
        if skipped:
            self.logger.warning(
                f"远程 JSON 校验: {len(result)} 个账号有效, "
                f"{len(skipped)} 个账号被跳过 [{', '.join(skipped)}]"
            )

        # 全部账号都被跳过时，视为无有效数据
        if not result:
            self.logger.error(
                "远程 JSON 中所有账号均校验失败，无有效数据"
            )
            return None

        return result

    # ── 远程拉取结果处理 ──

    async def _on_remote_success(self):
        """
        远程拉取成功后的状态更新。

        如果此前处于连续失败状态，发送恢复通知。
        """
        if self._was_failing:
            self._was_failing = False
            self.logger.info(
                f"远程 Cookie 拉取已恢复 "
                f"(此前连续失败 {self._consecutive_failures} 次)"
            )
            if self._notifier:
                await self._notifier.emit(
                    "REMOTE_FETCH_RECOVERED", "INFO",
                    message=(
                        f"远程 Cookie 拉取已恢复"
                        f"（此前连续失败 {self._consecutive_failures} 次）"
                    ),
                )
        self._consecutive_failures = 0

    async def _on_remote_failure(self, reason: str, is_initial: bool,
                                  masked_url: str):
        """
        远程拉取失败后的计数与告警处理。

        告警策略：
        - 首次启动拉取失败：立即通知一次
        - 定时刷新连续失败达到阈值：通知一次（受冷却时间限制）
        - 连续失败次数作为日志上下文输出

        Args:
            reason: 失败原因描述
            is_initial: 是否为首次启动时的拉取
            masked_url: 脱敏后的远程 URL（仅用于日志）
        """
        self._consecutive_failures += 1
        self.logger.warning(
            f"远程 Cookie 拉取失败 "
            f"(连续第 {self._consecutive_failures} 次): "
            f"{reason} [{masked_url}]"
        )

        threshold = self.config.cookie_remote_failure_alert_threshold

        if is_initial:
            # 首次启动失败：单独告警类型
            if self._notifier:
                await self._notifier.emit(
                    "REMOTE_INITIAL_FETCH_FAILED", "WARNING",
                    message=f"首次远程 Cookie 拉取失败: {reason}",
                )
        elif self._consecutive_failures >= threshold:
            # 连续失败达到阈值：标记失败状态（独立于 notifier 是否存在）
            self._was_failing = True
            if self._notifier:
                await self._notifier.emit(
                    "REMOTE_FETCH_CONSECUTIVE_FAILURE", "WARNING",
                    message=(
                        f"远程 Cookie 拉取连续失败 "
                        f"{self._consecutive_failures} 次 "
                        f"(阈值: {threshold})"
                    ),
                )
