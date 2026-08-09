"""
logger.py — 日志管理

单进程单 Handler 架构：
- 全局仅创建一个 FileHandler + 一个 StreamHandler
- 不同模块/实例通过 ScopeAdapter 注入前缀标签
- 支持通过 TZ_OFFSET 环境变量配置日志时区
"""

import logging
import datetime
import os


# =====================================================================
# 时区转换
# =====================================================================

def _custom_timezone_converter(timestamp):
    """将时间戳转换为指定时区的 struct_time（默认 UTC+8 北京时间）"""
    try:
        offset_hours = float(os.getenv('TZ_OFFSET', 8))
    except (ValueError, TypeError):
        offset_hours = 8
    tz = datetime.timezone(datetime.timedelta(hours=offset_hours))
    return datetime.datetime.fromtimestamp(timestamp, tz).timetuple()


# =====================================================================
# 作用域适配器
# =====================================================================

class ScopeAdapter(logging.LoggerAdapter):
    """为日志消息注入 scope 前缀，格式与旧版一致"""

    def process(self, msg, kwargs):
        scope = self.extra.get('scope', '')
        if scope:
            msg = f"{scope} - {msg}"
        return msg, kwargs


# =====================================================================
# 全局 Logger 管理
# =====================================================================

_root_logger = None
_initialized = False


def setup_root_logger(log_file, level=logging.INFO):
    """
    初始化全局根 Logger（仅执行一次）。

    创建唯一的 FileHandler 和 StreamHandler，后续所有模块通过
    get_logger() 获取带作用域前缀的适配器。

    :param log_file: 日志文件路径
    :param level: 日志级别
    """
    global _root_logger, _initialized

    if _initialized:
        return

    logger = logging.getLogger('camoufox')
    logger.setLevel(level)

    # 防御性清理
    if logger.hasHandlers():
        logger.handlers.clear()

    fmt = logging.Formatter(
        '%(asctime)s - %(process)d - %(levelname)s - %(message)s'
    )
    fmt.converter = _custom_timezone_converter

    fh = logging.FileHandler(log_file, encoding='utf-8')
    fh.setLevel(level)
    fh.setFormatter(fmt)

    ch = logging.StreamHandler()
    ch.setLevel(level)
    ch.setFormatter(fmt)

    logger.addHandler(fh)
    logger.addHandler(ch)
    logger.propagate = False

    _root_logger = logger
    _initialized = True


def get_logger(scope="main"):
    """
    获取带作用域前缀的 Logger 适配器。

    :param scope: 日志前缀标签（如 "manager", "USER_COOKIE_1", "server"）
    :return: ScopeAdapter 实例
    """
    global _root_logger

    if _root_logger is None:
        # 未初始化时的临时 Logger（仅输出到控制台）
        temp = logging.getLogger('camoufox._temp')
        if not temp.handlers:
            handler = logging.StreamHandler()
            handler.setFormatter(logging.Formatter(
                '%(asctime)s - %(process)d - %(levelname)s - %(message)s'
            ))
            temp.addHandler(handler)
            temp.setLevel(logging.INFO)
            temp.propagate = False
        return ScopeAdapter(temp, {'scope': scope})

    return ScopeAdapter(_root_logger, {'scope': scope})
