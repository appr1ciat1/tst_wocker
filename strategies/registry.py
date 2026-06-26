"""
strategies.registry — 策略名稱 → 類別 的註冊與查找

用法
----
    from strategies.registry import register, get_strategy, list_strategies

    @register("my_strat")
    class MyStrategy(Strategy):
        ...

    strat = get_strategy("my_strat", ma_period=60)   # 取得實例
"""

from typing import Dict, Type

from strategies.base import Strategy

_REGISTRY: Dict[str, Type[Strategy]] = {}


def register(name: str):
    """類別裝飾器：把策略以 name 註冊。"""
    def _wrap(cls: Type[Strategy]):
        cls.name = name
        _REGISTRY[name] = cls
        return cls
    return _wrap


def get_strategy(name: str, **params) -> Strategy:
    """依名稱建立策略實例（找不到時拋出清楚的錯誤）。"""
    _ensure_loaded()
    if name not in _REGISTRY:
        raise KeyError(
            f"未知策略 '{name}'。可用：{sorted(_REGISTRY)}。"
            f"（新策略請在 strategies/ 下用 @register 註冊）"
        )
    return _REGISTRY[name](**params)


def list_strategies() -> Dict[str, str]:
    """回傳 {name: description}。"""
    _ensure_loaded()
    return {n: getattr(c, "description", "") for n, c in sorted(_REGISTRY.items())}


_LOADED = False


def _ensure_loaded():
    """延遲匯入內建策略插件，避免 import 期循環相依。"""
    global _LOADED
    if _LOADED:
        return
    _LOADED = True
    # 在此 import 內建插件即會觸發其 @register
    from strategies import momentum_v85       # noqa: F401  v8.5（忠實引擎）
    from strategies import optimized_v85       # noqa: F401  mom_guard / mom_surge（v8.5 約束優化）
    from strategies import sector_rotation_v2  # noqa: F401  SR v2（忠實引擎）
    from strategies import hybrid_tiered_v9    # noqa: F401  v9 overlay
    from strategies import momentum_v9_sbl     # noqa: F401  v9 + 借券(SBL) tilt
    from strategies import reversal            # noqa: F401  均值回歸 sleeve
    from strategies import ew_momentum         # noqa: F401  範例（目標權重型）
