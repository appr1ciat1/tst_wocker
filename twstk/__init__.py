"""
twstk — 乾淨拆分後的核心套件

三個互相獨立、與「策略」解耦的層：

    twstk.data       【套件 1】歷史數據   — 純抓資料，零策略邏輯
    twstk.backtest   【套件 2】歷史回測   — 吃 data + 可抽換策略插件
    twstk.paper      【套件 3】每日模擬   — 4/22 起，吃 data + 同一套策略插件

策略本身放在獨立的 `strategies/` 插件夾，透過 `strategies.base.Strategy`
介面接入；未來要研究多種策略時，只新增插件，不動上面三層。
"""

__version__ = "1.0.0"
