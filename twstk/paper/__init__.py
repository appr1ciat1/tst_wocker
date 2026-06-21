"""
twstk.paper —【套件 3】每日實際資料模擬交易（4/22 起，可抽換策略）

與套件 2（回測）共用策略介面與 twstk.portfolio 成交核心。

CLI：
    python -m twstk.paper.tracker --replay-from 2026-04-22 --strategy momentum_v85
    python -m twstk.paper.tracker            # 從上次狀態續跑到今天
"""

from twstk.paper.tracker import run, SimConfig, load_state, save_state

__all__ = ["run", "SimConfig", "load_state", "save_state"]

# 注意：simulate 舊名已改為 run（依策略型態自動分派）。
