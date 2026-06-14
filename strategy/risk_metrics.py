"""
風險指標計算模組 (Risk Metrics Calculator)

計算量化策略常用的風險調整後績效指標：
- Annualized Return / Volatility
- Sharpe / Sortino / Calmar Ratio
- Max Drawdown (金額 & 百分比)
- Win Rate / Profit Factor
- Worst Month / Best Month
- Turnover Rate

v9 Hybrid Tiered 擴充：
- Tiered scaling helpers (core vs satellite)
- Merged book equity + portfolio vol forecast 相關指標
- compute_tired_risk_summary
"""

import pandas as pd
import numpy as np

from typing import Dict, Optional, Tuple, Any


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
    daily_rf = (1 + risk_free_rate) ** (1 / trading_days_per_year) - 1
    excess_daily_returns = daily_returns - daily_rf
    sharpe = (excess_daily_returns.mean() / daily_returns.std()
              * np.sqrt(trading_days_per_year)
              if daily_returns.std() > 0 else 0)
    geometric_sharpe = ((ann_return - risk_free_rate) / ann_volatility
                        if ann_volatility > 0 else 0)

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
        'geometric_sharpe': geometric_sharpe,
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
        f"  Geom. Sharpe:   {metrics['geometric_sharpe']:.3f}",
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


# ============================================================
# v9 Hybrid Tiered Risk Budgeting 擴充函數
# ============================================================

def compute_portfolio_vol_forecast(
    equity_core: Optional[pd.DataFrame | pd.Series] = None,
    equity_sat: Optional[pd.DataFrame | pd.Series] = None,
    merged_equity: Optional[pd.DataFrame | pd.Series] = None,
    lookback: int = 60,
    ewma_lambda: float = 0.94,
) -> Dict[str, float]:
    """
    計算組合層級預測波動率（EWMA 為主）。
    供 Portfolio Volatility Targeting 使用。
    """
    def _to_ret(eq):
        if eq is None:
            return pd.Series(dtype=float)
        if isinstance(eq, pd.DataFrame):
            if "Equity" in eq.columns:
                s = eq["Equity"]
            else:
                s = eq.iloc[:, 0]
        else:
            s = eq
        s = s.sort_index().dropna()
        return s.pct_change().dropna().tail(lookback)

    if merged_equity is not None:
        rets = _to_ret(merged_equity)
    else:
        rc = _to_ret(equity_core)
        rs = _to_ret(equity_sat)
        if len(rc) == 0 and len(rs) == 0:
            return {"ann_vol": 0.12, "method": "neutral"}
        # 合併報酬（權益加總後的 pct）
        idx = rc.index.union(rs.index)
        # 簡化：直接用兩個 book 報酬加權（等權近似）
        combined = (rc.reindex(idx).fillna(0) + rs.reindex(idx).fillna(0)).dropna()
        rets = combined.tail(lookback)

    if len(rets) < 5:
        return {"ann_vol": 0.12, "method": "insufficient_data"}

    # EWMA variance
    var = float(rets.var())
    lam = ewma_lambda
    for r in rets.values:
        var = lam * var + (1 - lam) * (r ** 2)
    ann_vol = float(np.sqrt(max(var, 1e-12)) * np.sqrt(252))
    return {"ann_vol": round(ann_vol, 5), "method": "ewma", "n_obs": len(rets)}


def compute_tiered_scales(
    forecast_ann_vol: float,
    target_ann_vol: float = 0.15,
    core_decay: float = 0.35,
    sat_decay: float = 0.85,
    core_floor: float = 0.55,
    sat_floor: float = 0.15,
    core_base: float = 0.25,
    sat_base: float = 0.75,
) -> Dict[str, float]:
    """
    依 forecast vol 計算 tiered core/satellite scale factors。
    與 portfolio_vol_target.py 邏輯對齊（可獨立呼叫）。
    """
    if forecast_ann_vol <= 0:
        forecast_ann_vol = 0.01
    from strategy.portfolio_vol_target import ROTATION_TRIGGER_VOL, VolTargetConfig, PortfolioVolatilityTarget
    band_high = ROTATION_TRIGGER_VOL
    if forecast_ann_vol <= band_high:
        overall = 1.0
        over = 0.0
    else:
        overall = min(1.0, max(0.10, band_high / forecast_ann_vol))
        over = (forecast_ann_vol - band_high) / max(band_high, 1e-6)

    core_mult = max(core_floor, 1.0 - core_decay * over)
    sat_mult = max(sat_floor, 1.0 - sat_decay * over)
    sat_trade_scale = 1.0

    rotation = PortfolioVolatilityTarget(
        VolTargetConfig(target_ann_vol=target_ann_vol)
    ).capital_rotation(forecast_ann_vol)

    return {
        "overall": round(overall, 4),
        "core_mult": round(core_mult, 4),
        "sat_mult": round(sat_mult, 4),
        "core_trade_scale": 1.0,
        "sat_trade_scale": sat_trade_scale,
        **rotation,
        "core_effective": round(core_base * core_mult * overall, 4),
        "sat_effective": round(sat_base * sat_mult * overall, 4),
        "forecast_ann_vol": round(forecast_ann_vol, 4),
        "target_ann_vol": round(target_ann_vol, 4),
        "over": round(over, 4),
    }


def merge_book_equities(
    equity_core: pd.DataFrame | pd.Series,
    equity_sat: pd.DataFrame | pd.Series,
    initial_capital: float = 1_000_000.0,
) -> pd.Series:
    """將 Core 與 Satellite 兩本 equity 合併（用於 portfolio vol 與總風險報告）。"""
    def _series(eq):
        if isinstance(eq, pd.DataFrame):
            return eq.get("Equity", eq.iloc[:, 0])
        return eq
    ec = _series(equity_core).sort_index()
    es = _series(equity_sat).sort_index()
    idx = ec.index.union(es.index)
    merged = (ec.reindex(idx).ffill() + es.reindex(idx).ffill()).dropna()
    if len(merged) > 0 and merged.iloc[0] < initial_capital * 0.2:
        merged = merged * (initial_capital / max(merged.iloc[0], 1))
    return merged


def compute_tiered_risk_summary(
    equity_core: Optional[pd.DataFrame | pd.Series],
    equity_sat: Optional[pd.DataFrame | pd.Series],
    trades_core: Optional[pd.DataFrame] = None,
    trades_sat: Optional[pd.DataFrame] = None,
    target_ann_vol: float = 0.15,
    initial_capital: float = 1_000_000.0,
) -> Dict[str, Any]:
    """
    計算分層風險摘要：分別 + 合併 + tiered scale 建議。
    供 paper / report 使用。
    """
    ec = equity_core if equity_core is not None else pd.Series(dtype=float)
    es = equity_sat if equity_sat is not None else pd.Series(dtype=float)
    merged_eq = merge_book_equities(ec, es, initial_capital)
    vol_info = compute_portfolio_vol_forecast(equity_core, equity_sat, merged_eq)
    fvol = vol_info.get("ann_vol", 0.12)
    scales = compute_tiered_scales(fvol, target_ann_vol=target_ann_vol)

    # 各自風險指標（若有 equity）
    def _simple_metrics(eq, name):
        if eq is None or len(eq) < 5:
            return {f"{name}_ann_vol": None, f"{name}_mdd": None}
        s = eq if isinstance(eq, pd.Series) else (eq["Equity"] if "Equity" in eq.columns else eq.iloc[:, 0])
        rets = s.pct_change().dropna()
        ann_vol = rets.std() * np.sqrt(252) if len(rets) > 1 else 0.0
        cummax = s.cummax()
        dd = (s / cummax - 1).min()
        return {f"{name}_ann_vol": round(ann_vol, 4), f"{name}_mdd": round(float(dd), 4)}

    out = {
        "portfolio": {
            "merged_ann_vol": vol_info["ann_vol"],
            "forecast_method": vol_info.get("method"),
            "target_ann_vol": target_ann_vol,
            **scales,
        },
        "core": _simple_metrics(equity_core, "core"),
        "satellite": _simple_metrics(equity_sat, "sat"),
        "merged_mdd": None,
    }

    if len(merged_eq) > 5:
        cummax = merged_eq.cummax()
        out["merged_mdd"] = round(float((merged_eq / cummax - 1).min()), 4)

    # 簡單交易統計
    nc = int(len(trades_core)) if trades_core is not None else 0
    ns = int(len(trades_sat)) if trades_sat is not None else 0
    out["trades"] = {"core_trades": nc, "sat_trades": ns, "total": nc + ns}

    return out


def format_tiered_risk_summary(summary: Dict[str, Any]) -> str:
    """格式化 tiered risk 摘要供 CLI / 報告輸出。"""
    p = summary.get("portfolio", {})
    lines = [
        "═" * 52,
        "🛡️  Hybrid Tiered Risk Budgeting 摘要",
        "═" * 52,
        f"  預測組合年化波動: {p.get('merged_ann_vol', 0)*100:.2f}%",
        f"  目標年化波動:     {p.get('target_ann_vol', 0.1)*100:.1f}%",
        f"  Overall Scale:    {p.get('overall', 1.0):.3f}",
        f"  Core Rotation:    {p.get('core_rotation_boost', 1.0):.3f} (買滿×輪動)",
        f"  Sat  Rotation:     {p.get('sat_rotation_boost', 1.0):.3f} (買滿×輪動)",
        f"  預測超額程度:     {p.get('over', 0):.3f}",
    ]
    if summary.get("merged_mdd") is not None:
        lines.append(f"  合併最大回撤:     {summary['merged_mdd']*100:.1f}%")
    lines.append(f"  Core 交易數:      {summary.get('trades',{}).get('core_trades',0)}")
    lines.append(f"  Satellite 交易數: {summary.get('trades',{}).get('sat_trades',0)}")
    lines.append("═" * 52)
    return "\n".join(lines)
