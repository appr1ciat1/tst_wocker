"""
風險指標計算模組 (Risk Metrics Calculator)

計算量化策略常用的風險調整後績效指標：
- Annualized Return / Volatility
- Sharpe / Sortino / Calmar Ratio
- Max Drawdown (金額 & 百分比)
- Win Rate / Profit Factor
- Worst Month / Best Month
- Turnover Rate
"""

import pandas as pd
import numpy as np


def compute_risk_metrics(equity_df, trades_df, initial_capital=1_000_000, risk_free_rate=0.0):
    """
    計算完整的風險調整後績效指標。

    Parameters
    ----------
    equity_df : pd.DataFrame
        每日資金曲線 (index=Date, columns=['Equity'])
    trades_df : pd.DataFrame
        交易明細
    initial_capital : float
        初始資金
    risk_free_rate : float
        無風險利率 (年化)

    Returns
    -------
    metrics : dict
        所有績效指標的字典
    """
    equity = equity_df['Equity']

    # === 基本收益率 ===
    daily_returns = equity.pct_change().dropna()
    total_days = len(equity)
    trading_days_per_year = 252

    total_return = (equity.iloc[-1] / initial_capital - 1)
    years = total_days / trading_days_per_year
    ann_return = (1 + total_return) ** (1 / years) - 1 if years > 0 else 0
    ann_volatility = daily_returns.std() * np.sqrt(trading_days_per_year)

    # === Sharpe Ratio ===
    sharpe = (ann_return - risk_free_rate) / ann_volatility if ann_volatility > 0 else 0

    # === Sortino Ratio (只用下行波動) ===
    downside_returns = daily_returns[daily_returns < 0]
    downside_vol = downside_returns.std() * np.sqrt(trading_days_per_year) if len(downside_returns) > 0 else 0
    sortino = (ann_return - risk_free_rate) / downside_vol if downside_vol > 0 else 0

    # === Max Drawdown ===
    cummax = equity.cummax()
    drawdown = equity / cummax - 1
    max_drawdown_pct = drawdown.min()
    max_drawdown_idx = drawdown.idxmin()

    # Drawdown 持續期間
    peak_idx = equity[:max_drawdown_idx].idxmax() if max_drawdown_idx is not None else None

    # === Calmar Ratio ===
    calmar = ann_return / abs(max_drawdown_pct) if max_drawdown_pct != 0 else 0

    # === 月度收益 ===
    monthly_returns = equity.resample('ME').last().pct_change().dropna()
    worst_month = monthly_returns.min() if len(monthly_returns) > 0 else 0
    best_month = monthly_returns.max() if len(monthly_returns) > 0 else 0
    positive_months = (monthly_returns > 0).sum()
    total_months = len(monthly_returns)
    monthly_win_rate = positive_months / total_months if total_months > 0 else 0

    # === 交易統計 ===
    if not trades_df.empty:
        total_trades = len(trades_df)
        winning_trades = trades_df[trades_df['Return_Pct'] > 0]
        losing_trades = trades_df[trades_df['Return_Pct'] <= 0]

        win_rate = len(winning_trades) / total_trades
        avg_winner = winning_trades['Return_Pct'].mean() if len(winning_trades) > 0 else 0
        avg_loser = losing_trades['Return_Pct'].mean() if len(losing_trades) > 0 else 0

        # Profit Factor
        gross_profit = winning_trades['Return_Pct'].sum() if len(winning_trades) > 0 else 0
        gross_loss = abs(losing_trades['Return_Pct'].sum()) if len(losing_trades) > 0 else 0
        profit_factor = gross_profit / gross_loss if gross_loss > 0 else float('inf')

        avg_return = trades_df['Return_Pct'].mean()
        avg_days_held = trades_df['Days_Held'].mean()

        # 出場原因分布
        reason_counts = trades_df['Reason'].value_counts().to_dict()
    else:
        total_trades = 0
        win_rate = 0
        avg_winner = 0
        avg_loser = 0
        profit_factor = 0
        avg_return = 0
        avg_days_held = 0
        reason_counts = {}

    metrics = {
        # 收益
        'total_return': total_return,
        'ann_return': ann_return,
        'ann_volatility': ann_volatility,

        # 風險調整
        'sharpe': sharpe,
        'sortino': sortino,
        'calmar': calmar,

        # 回撤
        'max_drawdown_pct': max_drawdown_pct,
        'max_drawdown_date': max_drawdown_idx,
        'max_drawdown_peak': peak_idx,

        # 月度
        'worst_month': worst_month,
        'best_month': best_month,
        'monthly_win_rate': monthly_win_rate,

        # 交易
        'total_trades': total_trades,
        'win_rate': win_rate,
        'avg_winner': avg_winner,
        'avg_loser': avg_loser,
        'profit_factor': profit_factor,
        'avg_return': avg_return,
        'avg_days_held': avg_days_held,
        'reason_counts': reason_counts,

        # 時間
        'total_days': total_days,
        'years': years,
    }

    return metrics


def format_metrics_summary(metrics):
    """
    格式化指標摘要為可讀字串。

    Parameters
    ----------
    metrics : dict
        compute_risk_metrics() 的輸出

    Returns
    -------
    summary : str
    """
    lines = [
        "═" * 50,
        "📊 風險調整後績效報告",
        "═" * 50,
        f"  年化報酬率:     {metrics['ann_return']*100:+.2f}%",
        f"  年化波動率:     {metrics['ann_volatility']*100:.2f}%",
        f"  Sharpe Ratio:   {metrics['sharpe']:.3f}",
        f"  Sortino Ratio:  {metrics['sortino']:.3f}",
        f"  Calmar Ratio:   {metrics['calmar']:.3f}",
        f"  最大回撤:       {metrics['max_drawdown_pct']*100:.1f}%",
        f"  最差月份:       {metrics['worst_month']*100:.1f}%",
        f"  最佳月份:       {metrics['best_month']*100:.1f}%",
        f"  月度勝率:       {metrics['monthly_win_rate']*100:.1f}%",
        "─" * 50,
        f"  總交易數:       {metrics['total_trades']}",
        f"  勝率:           {metrics['win_rate']*100:.1f}%",
        f"  平均贏家:       {metrics['avg_winner']*100:+.2f}%",
        f"  平均輸家:       {metrics['avg_loser']*100:+.2f}%",
        f"  Profit Factor:  {metrics['profit_factor']:.2f}",
        f"  平均持有天數:   {metrics['avg_days_held']:.1f}",
        "═" * 50,
    ]
    return "\n".join(lines)
