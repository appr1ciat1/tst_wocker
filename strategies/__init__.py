"""
strategies —【策略插件夾】

與三層基礎設施（twstk.data / twstk.backtest / twstk.paper）完全解耦。
未來研究多種策略時，只在此資料夾新增插件並用 @register 註冊即可。

公開介面：
    from strategies import Strategy, MarketData, SignalBundle
    from strategies import get_strategy, list_strategies, register
"""

from strategies.base import (
    Strategy, WeightStrategy, SignalStrategy, EngineStrategy, SignalProducer,
    MarketData, SignalBundle, ExecConfig, signals_to_weights,
)
from strategies.registry import register, get_strategy, list_strategies

__all__ = [
    "Strategy", "WeightStrategy", "SignalStrategy", "EngineStrategy", "SignalProducer",
    "MarketData", "SignalBundle", "ExecConfig", "signals_to_weights",
    "register", "get_strategy", "list_strategies",
]
