"""
AI 多維度 Rank 橫向排名策略 (AI Ensemble Cross-Sectional Ranking) — v8.3

Production scoring: rank_momentum × 3 + rank_trend × 1
(可選: + rank_liq × 0.3 when liq_stability=True)

已驗證無效 (Phase 1-4, 43 configs):
- ml_weights, residual_momentum, trend_quality: 有害
- breakeven, trailing, confidence-k, mid-hold-review: 有害或零效果
"""

STRATEGY_VERSION = "v8.5"

import yfinance as yf
import pandas as pd
import numpy as np
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings('ignore')


def fetch_panel_data(tickers, days=800, start_date=None, end_date=None):
    """
    批次下載多檔台股的 OHLCV 日線資料。

    Parameters
    ----------
    tickers : list[str]
        台股代號列表，例如 ['2330', '2317', '2454']
    days : int
        回溯天數，預設 800 天（約 3 年交易日）
    start_date : str or datetime, optional
        明確指定起始日期（優先於 days）
    end_date : str or datetime, optional
        明確指定結束日期（預設為今天）

    Returns
    -------
    close_df, open_df, high_df, low_df, vol_df : tuple[pd.DataFrame]
        各為 (日期 x 股票代號) 的 DataFrame，已做 forward fill
    """
    if end_date is not None:
        end_dt = pd.Timestamp(end_date)
    else:
        end_dt = pd.Timestamp(datetime.today())

    if start_date is not None:
        start_dt = pd.Timestamp(start_date)
        actual_days = (end_dt - start_dt).days
    else:
        start_dt = end_dt - timedelta(days=days)
        actual_days = days

    print(f"📥 正在批次下載 {len(tickers)} 檔股票的歷史資料 "
          f"({start_dt.strftime('%Y-%m-%d')} → {end_dt.strftime('%Y-%m-%d')}, "
          f"~{actual_days} 天)...")

    tw_tickers = [f"{t}.TW" for t in tickers]

    # yfinance 批次下載有大小限制，分批處理
    batch_size = 50
    all_dfs = []
    for batch_start in range(0, len(tw_tickers), batch_size):
        batch = tw_tickers[batch_start:batch_start + batch_size]
        batch_num = batch_start // batch_size + 1
        total_batches = (len(tw_tickers) + batch_size - 1) // batch_size
        print(f"   📦 下載批次 {batch_num}/{total_batches} ({len(batch)} 檔)...")
        df = yf.download(batch, start=start_dt, end=end_dt, progress=False)
        if not df.empty:
            all_dfs.append(df)

    if not all_dfs:
        raise RuntimeError("無法下載任何資料")

    # 合併所有批次
    if len(all_dfs) == 1:
        df = all_dfs[0]
    else:
        df = pd.concat(all_dfs, axis=1)

    data = {}
    for col in ['Close', 'Open', 'High', 'Low', 'Volume']:
        if isinstance(df.columns, pd.MultiIndex):
            try:
                temp_df = df.xs(col, level=0, axis=1)
            except KeyError:
                print(f"   ⚠️ 欄位 {col} 不存在，跳過")
                continue
        else:
            temp_df = df[[col]]

        temp_df.columns = [str(c).replace('.TW', '') for c in temp_df.columns]
        data[col] = temp_df.ffill()

    print(f"   ✅ 下載完成，資料範圍：{data['Close'].index[0].strftime('%Y-%m-%d')}"
          f" → {data['Close'].index[-1].strftime('%Y-%m-%d')}"
          f"，共 {len(data['Close'].columns)} 檔")
    return data['Close'], data['Open'], data['High'], data['Low'], data['Volume']


def build_liquid_universe(close_df, vol_df, top_n=50, lookback=20):
    """
    建立動態流動性 Universe。

    每日取「過去 lookback 日平均成交額 Top-N」作為當日可投資池。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣
    vol_df : pd.DataFrame
        成交量矩陣
    top_n : int
        每日 universe 大小
    lookback : int
        成交額均值回溯期

    Returns
    -------
    universe_mask : pd.DataFrame (bool)
        (日期 x 股票) 的布林矩陣，True 代表當日在 universe 中
    """
    print(f"🌐 建立動態流動性 Universe (Top-{top_n}, 回溯 {lookback} 日)...")

    # 平均成交額 = 收盤價 × 成交量 的 rolling mean
    turnover = (close_df * vol_df).rolling(lookback).mean()

    # 每日取 top_n
    universe_mask = turnover.rank(axis=1, ascending=False) <= top_n

    # 確保 NaN 的位置不被選入
    universe_mask = universe_mask & close_df.notna() & (close_df > 0)

    avg_size = universe_mask.sum(axis=1).mean()
    print(f"   ✅ 動態 Universe 建立完成，平均每日 {avg_size:.0f} 檔")
    return universe_mask


def engineer_features(close_df, vol_df, universe_mask=None,
                      ma_period=60, short_ma_period=20, multi_ma=False,
                      ml_weights=False, inst_flow_weight=0.0,
                      inst_flow_df=None,
                      residual_momentum=False,
                      trend_quality=False,
                      liq_stability=False,
                      liq_mode='raw',
                      market_close=None,
                      rsi_weight=0.0,
                      breakout_weight=0.0,
                      value_weight=0.0,
                      rev_momentum_weight=0.0):
    """
    計算 AI 多維度特徵並做橫向百分位排名。

    Parameters
    ----------
    close_df : pd.DataFrame
        收盤價矩陣 (日期 x 股票代號)
    vol_df : pd.DataFrame
        成交量矩陣 (日期 x 股票代號)
    universe_mask : pd.DataFrame (bool), optional
        動態 Universe 遮罩。若提供，只在當日 universe 中做排名。
    ma_period : int
        主趨勢均線天數（預設 60）
    short_ma_period : int
        短期均線天數（用於多均線確認，預設 20）
    multi_ma : bool
        啟用多均線確認（short_ma > long_ma 才允許進場）
    ml_weights : bool
        啟用 ML 因子加權（LightGBM 取代等權加總）

    Returns
    -------
    total_score : pd.DataFrame
        各股票的 AI 綜合評分，日期 x 股票
    ma_long : pd.DataFrame
        主趨勢均線矩陣，用於進場信號過濾
    atr_df : pd.DataFrame
        20 日 ATR 矩陣，用於自適應 TP/SL 與 position sizing
    short_ma : pd.DataFrame or None
        短期均線矩陣（multi_ma=True 時有效）
    """
    print("🧠 正在計算多維度弱特徵與 Rank 排名...")

    # === 原始指標計算 ===
    # 1. 20 日動能：今天收盤 / 20 天前收盤
    mom_20 = close_df / close_df.shift(20)

    # 2. MA 乖離率：價格偏離均線的幅度
    ma_long = close_df.rolling(ma_period).mean()
    trend_bias = close_df / ma_long

    # 3. 量能爆發比：5 日均量 / 20 日均量
    vol_surge = vol_df.rolling(5).mean() / (vol_df.rolling(20).mean() + 1e-8)

    # 4. 穩定度：波動率的倒數（越穩定越好）
    volatility = close_df.pct_change().rolling(20).std()
    stability = 1 / (volatility + 1e-8)

    # 短期均線（多均線確認用）
    short_ma = close_df.rolling(short_ma_period).mean() if multi_ma else None
    ma_20 = close_df.rolling(20).mean()

    # === ATR 計算 (用於 TP/SL 與 sizing) ===
    atr_df = close_df.pct_change().abs().rolling(20).mean() * close_df

    # === 殘差動量：扣除市場 beta ===
    residual_mom = None
    if residual_momentum and market_close is not None:
        try:
            stock_ret = close_df.pct_change()
            mkt_ret = market_close.pct_change()
            mkt_ret_aligned = mkt_ret.reindex(stock_ret.index, method='ffill')
            stock_cum_20 = stock_ret.rolling(20).sum()
            mkt_cum_20 = mkt_ret_aligned.rolling(20).sum()
            residual_mom = stock_cum_20.sub(mkt_cum_20, axis=0)
            print("   \U0001f52c 殘差動量已計算 (market-beta adjusted)")
        except Exception as e:
            print(f"   ⚠️ 殘差動量計算失敗: {e}")

    # === 趨勢品質 ===
    tq_score = None
    if trend_quality:
        try:
            ma60_slope = (ma_long - ma_long.shift(5)) / (ma_long.shift(5) + 1e-8)
            ma_alignment = ((close_df > ma_20) & (ma_20 > ma_long)).astype(float)
            overheat = (close_df / ma_20 - 1).clip(lower=0)
            overheat_penalty = 1 - overheat.clip(upper=0.15) / 0.15
            tq_score = ma60_slope * 100 + ma_alignment * 0.5 + overheat_penalty * 0.3
            print("   \U0001f4d0 趨勢品質已計算")
        except Exception as e:
            print(f"   ⚠️ 趨勢品質計算失敗: {e}")

    # === 流動性穩定度 ===
    liq_stab = None
    if liq_stability:
        try:
            turnover = close_df * vol_df
            raw_liq = turnover.rolling(20).mean() / (turnover.rolling(20).std() + 1e-8)

            if liq_mode == 'demeaned':
                # 殘差 liq: 扣除橫截面平均，保留個股相對穩定度
                cross_mean = raw_liq.mean(axis=1)
                liq_stab = raw_liq.sub(cross_mean, axis=0)
                print("   \U0001f4a7 流動性穩定度已計算 (demeaned)")
            elif liq_mode == 'sector':
                # 行業中性: 電子 vs 非電子分開計算再合併
                elec_prefixes = ('23','24','30','33','34','35','36','37',
                                 '49','61','63','64','65','66','67','68','69')
                elec_cols = [c for c in close_df.columns if str(c).startswith(elec_prefixes)]
                non_elec_cols = [c for c in close_df.columns if c not in elec_cols]
                liq_stab = raw_liq.copy()
                if elec_cols:
                    elec_mean = raw_liq[elec_cols].mean(axis=1)
                    liq_stab[elec_cols] = raw_liq[elec_cols].sub(elec_mean, axis=0)
                if non_elec_cols:
                    ne_mean = raw_liq[non_elec_cols].mean(axis=1)
                    liq_stab[non_elec_cols] = raw_liq[non_elec_cols].sub(ne_mean, axis=0)
                print("   \U0001f4a7 流動性穩定度已計算 (sector-neutral)")
            else:
                liq_stab = raw_liq
                print("   \U0001f4a7 流動性穩定度已計算 (raw)")
        except Exception:
            pass

    # === 橫向百分位排名 ===
    def _rank(df):
        if universe_mask is not None:
            return df.where(universe_mask).rank(axis=1, pct=True)
        return df.rank(axis=1, pct=True)

    rank_mom = _rank(mom_20)
    rank_trend = _rank(trend_bias)
    rank_res_mom = _rank(residual_mom) if residual_mom is not None else None
    rank_tq = _rank(tq_score) if tq_score is not None else None
    rank_liq = _rank(liq_stab) if liq_stab is not None else None

    # === 籌碼因子排名 ===
    rank_inst = None
    if inst_flow_weight > 0 and inst_flow_df is not None:
        inst_aligned = inst_flow_df.reindex(
            index=close_df.index, columns=close_df.columns
        )
        rank_inst = _rank(inst_aligned)
        print(f"   \U0001f3db\ufe0f 籌碼因子已載入 (weight={inst_flow_weight})")

    # === FinLab 啟發因子（可選） ===
    rank_rsi = None
    rank_breakout = None
    rank_value = None
    rank_rev_mom = None

    if rsi_weight > 0:
        try:
            from strategy.finlab_factors import compute_rsi_rank
            rank_rsi = compute_rsi_rank(close_df, period=20, universe_mask=universe_mask)
            print(f"   📈 RSI-20 因子已計算 (weight={rsi_weight})")
        except Exception as e:
            print(f"   ⚠️ RSI 因子計算失敗: {e}")

    if breakout_weight > 0:
        try:
            from strategy.finlab_factors import compute_breakout_rank
            rank_breakout = compute_breakout_rank(close_df, window=300, universe_mask=universe_mask)
            print(f"   🏔️ Breakout-300 因子已計算 (weight={breakout_weight})")
        except Exception as e:
            print(f"   ⚠️ Breakout 因子計算失敗: {e}")

    if value_weight > 0:
        try:
            from strategy.finlab_factors import compute_value_rank
            rank_value = compute_value_rank(
                close_df, universe_mask=universe_mask,
                tickers=list(close_df.columns),
            )
            print(f"   💰 Value (PB+PE) 因子已計算 (weight={value_weight})")
        except Exception as e:
            print(f"   ⚠️ Value 因子計算失敗: {e}")

    if rev_momentum_weight > 0:
        try:
            from strategy.finlab_factors import compute_revenue_momentum
            rank_rev_mom = compute_revenue_momentum(close_df, period=60, universe_mask=universe_mask)
            print(f"   📊 RevMomentum-60 因子已計算 (weight={rev_momentum_weight})")
        except Exception as e:
            print(f"   ⚠️ RevMomentum 因子計算失敗: {e}")

    # === 因子加權 ===
    if ml_weights:
        # NOTE: ml_weights 已驗證無效 (Sharpe -55%), 保留但加警告
        import warnings as _w
        _w.warn('ml_weights 已驗證無效 (Sharpe -55%), 不建議使用', stacklevel=2)
        rank_vol = _rank(vol_surge)
        rank_stab = _rank(stability)
        total_score = _ml_factor_score(
            close_df, rank_mom, rank_trend, rank_vol, rank_stab, universe_mask
        )
    else:
        mom_factor = rank_res_mom if rank_res_mom is not None else rank_mom
        trend_factor = rank_tq if rank_tq is not None else rank_trend
        total_score = mom_factor * 3 + trend_factor * 1
        if rank_liq is not None:
            total_score = total_score + rank_liq * 0.3

        # FinLab 因子加權（opt-in，預設全部為 0 不影響 baseline）
        if rank_rsi is not None and rsi_weight > 0:
            total_score = total_score + rank_rsi * rsi_weight
        if rank_breakout is not None and breakout_weight > 0:
            total_score = total_score + rank_breakout * breakout_weight
        if rank_value is not None and value_weight > 0:
            total_score = total_score + rank_value * value_weight
        if rank_rev_mom is not None and rev_momentum_weight > 0:
            total_score = total_score + rank_rev_mom * rev_momentum_weight

    # 籌碼因子加權（opt-in）
    if rank_inst is not None and inst_flow_weight > 0:
        total_score = total_score + rank_inst * inst_flow_weight

    print("   ✅ 特徵計算完成")
    return total_score, ma_long, atr_df, short_ma


def _ml_factor_score(close_df, rank_mom, rank_trend, rank_vol, rank_stab,
                     universe_mask=None, train_window=120, forward_days=10):
    """
    使用 LightGBM 進行因子加權。
    滾動訓練：用過去 train_window 天的因子 → 未來 forward_days 天報酬的關係，
    產出每日因子加權分數。

    若 LightGBM 未安裝，自動 fallback 到等權加總。
    """
    try:
        import lightgbm as lgb
        print("   🤖 使用 LightGBM 因子加權模式...")
    except ImportError:
        print("   ⚠️ lightgbm 未安裝，fallback 到等權加總")
        return rank_mom + rank_trend + rank_vol + rank_stab

    # 未來 N 天報酬（作為 label）
    fwd_ret = close_df.shift(-forward_days) / close_df - 1

    total_score = pd.DataFrame(np.nan, index=close_df.index, columns=close_df.columns)
    dates = close_df.index

    # 每 20 天重新訓練一次模型（避免每天都訓練太慢）
    retrain_interval = 20
    model = None
    last_train_idx = -retrain_interval

    for i in range(train_window + forward_days, len(dates)):
        # 訓練（每 retrain_interval 天更新）
        if i - last_train_idx >= retrain_interval:
            train_start = max(0, i - train_window - forward_days)
            train_end = i - forward_days  # 確保 label 可用

            # 收集訓練資料
            X_list, y_list = [], []
            for t in range(train_start, train_end):
                for col in close_df.columns:
                    if universe_mask is not None:
                        if not universe_mask[col].iloc[t]:
                            continue
                    feats = [
                        rank_mom[col].iloc[t],
                        rank_trend[col].iloc[t],
                        rank_vol[col].iloc[t],
                        rank_stab[col].iloc[t],
                    ]
                    label = fwd_ret[col].iloc[t]
                    if any(pd.isna(f) for f in feats) or pd.isna(label):
                        continue
                    X_list.append(feats)
                    y_list.append(label)

            if len(X_list) >= 50:
                X_train = np.array(X_list)
                y_train = np.array(y_list)
                model = lgb.LGBMRegressor(
                    n_estimators=50, max_depth=3, learning_rate=0.1,
                    min_child_samples=10, subsample=0.8,
                    verbosity=-1, n_jobs=-1
                )
                model.fit(X_train, y_train)
                last_train_idx = i

        # 預測
        if model is not None:
            for col in close_df.columns:
                feats = [
                    rank_mom[col].iloc[i],
                    rank_trend[col].iloc[i],
                    rank_vol[col].iloc[i],
                    rank_stab[col].iloc[i],
                ]
                if any(pd.isna(f) for f in feats):
                    continue
                pred = model.predict([feats])[0]
                total_score[col].iloc[i] = pred
        else:
            # 模型還沒訓練好，用等權 fallback
            total_score.iloc[i] = (rank_mom.iloc[i] + rank_trend.iloc[i]
                                   + rank_vol.iloc[i] + rank_stab.iloc[i])

    # 轉為橫向排名（讓分數可比較）
    total_score = total_score.rank(axis=1, pct=True) * 4

    print(f"   ✅ ML 因子加權完成 (模型訓練 {(len(dates) - train_window) // retrain_interval} 次)")
    return total_score

