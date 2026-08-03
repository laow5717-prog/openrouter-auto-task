"""平台注册表。

平台标识是一个字符串 slug（'opencode'…），**代码里的这张表是唯一真值源**——
数据库只存 slug 字符串，不建 platforms 表。多一张表就多一处要同步的真值源，而平台
数量是个位数、随代码发布变化，不是运行时数据。

新增一个平台 = 写一个实现 PlatformAdapter 的类 + 在这里注册。编排层不需要改。
"""

from src.platforms.base import (  # noqa: F401  （对外统一从这里取）
    CAP_SUBSCRIBE,
    CAP_TOPUP,
    Credentials,
    PaymentResult,
    PlatformAdapter,
    SessionResult,
    SubscribingAdapter,
)

_REGISTRY = {}


def register(adapter):
    """注册一个适配器实例。同 slug 重复注册直接覆盖（便于测试替身）。"""
    _REGISTRY[adapter.slug] = adapter
    return adapter


def unregister(slug):
    """移除注册（测试收尾用）。不存在时静默。"""
    _REGISTRY.pop(slug, None)


def get(slug):
    """按 slug 取适配器。未知平台抛 KeyError——静默回落到默认平台会写错数据。"""
    if slug not in _REGISTRY:
        known = ', '.join(sorted(_REGISTRY)) or '（空）'
        raise KeyError(f"未知平台 '{slug}'，已注册：{known}")
    return _REGISTRY[slug]


def all_slugs():
    return sorted(_REGISTRY)


def describe_all():
    """供 API 给前端列平台选项。"""
    return [
        {
            'slug': a.slug,
            'display_name': a.display_name,
            'capabilities': sorted(a.capabilities),
        }
        for a in (_REGISTRY[s] for s in all_slugs())
    ]


def _bootstrap():
    """注册内置平台。放在函数里延迟执行，避免 import 期的循环依赖。"""
    from src.platforms.opencode import OpencodeAdapter
    from src.platforms.infron import InfronAdapter
    register(OpencodeAdapter())
    register(InfronAdapter())


_bootstrap()
