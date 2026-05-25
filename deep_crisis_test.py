#!/usr/bin/env python3
"""
深度危機壓測 + 00981A 對標 + 弱勢分析

對 Sector Rotation v2 策略在所有已知歷史危機做完整壓測，
用 00981A 和 0050 做 benchmark，並分析弱勢期間的指標特徵，
找出需要注意的信號和權重調整方向。
"""

import subprocess
import re
import sys
import numpy as np
import pandas as pd
import yfinance as yf
from datetime import datetime, timedelta
import warnings

warnings.filterwarnings('ignore')

# ====== 危機期間定義 ======
CRISIS_PERIODS = {
    # 金融海嘯
    'gfc_crash': {
        'label': '💥 金融海嘯',
        'fetch_start': '2007-09-01',
        'eval_start': '2008-01-01',
        'eval_end': '2009-03-31',
        'description': '雷曼倒閉，全球崩盤，台股 -58%',
    },
    'gfc_recovery': {
        'label': '💥 海嘯復甦',
        'fetch_start': '2008-11-01',
        'eval_start': '2009-03-01',
        'eval_end': '2010-01-31',
        'description': '觸底反彈，台灣 V 型復甦',
    },
    # COVID
    'covid_pre': {
        'label': '🦠 疫情前',
        'fetch_start': '2019-06-01',
        'eval_start': '2019-10-01',
        'eval_end': '2020-01-31',
        'description': '疫情消息傳出前，市場還在高點',
    },
    'covid_crash': {
        'label': '🦠 疫情爆發',
        'fetch_start': '2019-09-01',
        'eval_start': '2020-01-20',
        'eval_end': '2020-06-30',
        'description': '3月大崩盤，VIX > 80，V 型反轉',
    },
    'covid_recovery': {
        'label': '🦠 疫後牛市',
        'fetch_start': '2020-02-01',
        'eval_start': '2020-06-01',
        'eval_end': '2021-06-30',
        'description': '航運 + 半導體超級多頭，大盤 +80%',
    },
    # 烏克蘭戰爭
    'ukraine_war': {
        'label': '⚔️ 烏俄戰爭',
        'fetch_start': '2021-10-01',
        'eval_start': '2022-02-01',
        'eval_end': '2022-06-30',
        'description': '2022-02-24 開戰，原物料飆漲，電子崩',
    },
    # 升息衝擊
    'rate_hike': {
        'label': '📉 升息衝擊',
        'fetch_start': '2021-09-01',
        'eval_start': '2022-01-01',
        'eval_end': '2022-10-31',
        'description': 'Fed 暴力升息，台股 -25%',
    },
    # AI 行情
    'ai_rally': {
        'label': '🤖 AI 行情',
        'fetch_start': '2022-09-01',
        'eval_start': '2023-01-01',
        'eval_end': '2024-06-30',
        'description': '半導體主導，窄幅上漲',
    },
    # 關稅衝擊
    'tariff_pre': {
        'label': '🏛️ 關稅前一月',
        'fetch_start': '2025-10-01',
        'eval_start': '2026-03-01',
        'eval_end': '2026-04-01',
        'description': '關稅公告前，市場預期與波動',
    },
    'tariff_shock': {
        'label': '🏛️ 關稅衝擊',
        'fetch_start': '2025-09-01',
        'eval_start': '2026-02-01',
        'eval_end': '2026-04-09',
        'description': '川普對等關稅，全球供應鏈衝擊',
    },
    # 近期
    'recent': {
        'label': '📊 近期',
        'fetch_start': '2025-09-01',
        'eval_start': '2026-01-01',
        'eval_end': '2026-04-09',
        'description': '2026 開年表現',
    },
}


def run_sr_backtest(start_date, end_date, eval_start=None):
    """跑 Sector Rotation v2 回測。"""
    cmd = (f'{sys.executable} sector_rotation_report.py '
           f'--start-date {start_date} --end-date {end_date}')
    if eval_start:
        cmd += f' --eval-start {eval_start}'
    r = subprocess.run(cmd, shell=True, capture_output=True,
                       text=True, timeout=300)
    if r.returncode != 0:
        out = r.stdout + r.stderr
        raise RuntimeError(
            f"Sector rotation command failed with exit code {r.returncode}: {cmd}\n"
            f"{out[-2000:]}"
        )
    return _parse_output(r.stdout + r.stderr)


def run_v85_backtest(start_date, end_date, eval_start=None):
    """跑 v8.5 momentum 回測。"""
    cmd = (f'{sys.executable} ai_report.py '
           f'--start-date {start_date} --end-date {end_date}')
    if eval_start:
        cmd += f' --eval-start {eval_start}'
    r = subprocess.run(cmd, shell=True, capture_output=True,
                       text=True, timeout=300)
    if r.returncode != 0:
        out = r.stdout + r.stderr
        raise RuntimeError(
            f"v8.5 command failed with exit code {r.returncode}: {cmd}\n"
            f"{out[-2000:]}"
        )
    return _parse_output(r.stdout + r.stderr)


def _parse_output(out):
    """解析回測輸出。"""
    def get(pattern, label):
        m = re.search(pattern, out)
        if not m:
            raise ValueError(f"Failed to parse {label} from backtest output")
        return float(m.group(1))

    def get_first(patterns, label):
        for pattern in patterns:
            m = re.search(pattern, out)
            if m:
                return float(m.group(1))
        raise ValueError(f"Failed to parse {label} from backtest output")

    return {
        'ann': get(r'年化報酬率:\s+([\+\-\d\.]+)%', 'annual return'),
        'sharpe': get(r'Sharpe Ratio:\s+([\+\-\d\.]+)', 'Sharpe'),
        'calmar': get(r'Calmar Ratio:\s+([\+\-\d\.]+)', 'Calmar'),
        'mdd': get(r'最大回撤:\s+([\+\-\d\.]+)%', 'max drawdown'),
        'trades': int(get_first([r'總交易數:\s+(\d+)', r'共\s*(\d+)\s*筆交易'], 'trade count')),
        'win_rate': get_first([r'勝率[:：]\s*([\d\.]+)%', r'勝率\s*([\d\.]+)%'], 'win rate'),
        'pf': get(r'Profit Factor:\s+(inf|[\d\.]+)', 'profit factor'),
    }


def get_benchmark_return(ticker, start_date, end_date):
    """取得 benchmark 在指定期間的報酬。"""
    try:
        data = yf.download(f'{ticker}.TW', start=start_date,
                           end=end_date, progress=False)
        if data.empty or len(data) < 5:
            return None
        close = data['Close'].squeeze()
        total_ret = (close.iloc[-1] / close.iloc[0] - 1) * 100
        daily_ret = close.pct_change().dropna()
        sharpe = (daily_ret.mean() / daily_ret.std() * np.sqrt(252)
                  if daily_ret.std() > 0 else 0)
        peak = close.cummax()
        mdd = ((close - peak) / peak).min() * 100
        return {'total_ret': total_ret, 'sharpe': sharpe, 'mdd': mdd}
    except Exception:
        return None


def get_us_indicators(start_date, end_date):
    """取得危機期間的美股指標統計。"""
    try:
        spy = yf.download('^GSPC', start=start_date,
                          end=end_date, progress=False)
        vix = yf.download('^VIX', start=start_date,
                          end=end_date, progress=False)
        sox = yf.download('^SOX', start=start_date,
                          end=end_date, progress=False)

        result = {}
        if not spy.empty:
            spy_close = spy['Close'].squeeze()
            result['spy_ret'] = (spy_close.iloc[-1] / spy_close.iloc[0] - 1) * 100
            spy_ma60 = spy_close.rolling(60).mean()
            below_ma60_pct = (spy_close < spy_ma60).mean() * 100
            result['spy_below_ma60_pct'] = below_ma60_pct
        if not vix.empty:
            vix_close = vix['Close'].squeeze()
            result['vix_mean'] = vix_close.mean()
            result['vix_max'] = vix_close.max()
            result['vix_above_25_pct'] = (vix_close > 25).mean() * 100
            result['vix_above_30_pct'] = (vix_close > 30).mean() * 100
            result['vix_above_35_pct'] = (vix_close > 35).mean() * 100
        if not sox.empty:
            sox_close = sox['Close'].squeeze()
            result['sox_ret'] = (sox_close.iloc[-1] / sox_close.iloc[0] - 1) * 100
            sox_ma60 = sox_close.rolling(60).mean()
            result['sox_below_ma60_pct'] = (sox_close < sox_ma60).mean() * 100

        return result
    except Exception:
        return {}


def main():
    print(f"{'='*80}")
    print(f"🔥 深度危機壓測 + 00981A 對標 — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*80}")
    print(f"   策略: Sector Rotation v2 (美股前提 + 板塊資金流)")
    print(f"   對照: v8.5 Momentum + 0050 + 00981A")
    print()

    all_results = []

    for name, info in CRISIS_PERIODS.items():
        print(f"\n{'─'*80}")
        print(f"{info['label']}  ({info['eval_start']} → {info['eval_end']})")
        print(f"   {info['description']}")
        print(f"{'─'*80}")

        eval_start = info['eval_start']
        eval_end = info['eval_end']
        fetch_start = info['fetch_start']

        # SR v2
        sys.stderr.write(f"  Running SR v2...\n")
        sr_metrics = run_sr_backtest(fetch_start, eval_end, eval_start=eval_start)

        # v8.5
        sys.stderr.write(f"  Running v8.5...\n")
        v85_metrics = run_v85_backtest(fetch_start, eval_end, eval_start=eval_start)

        # Benchmarks
        bm_0050 = get_benchmark_return('0050', eval_start, eval_end)
        bm_00981a = get_benchmark_return('00981A', eval_start, eval_end)

        # US indicators
        us_ind = get_us_indicators(eval_start, eval_end)

        # 輸出
        print(f"\n   {'':16s} | {'SR v2':>10s} | {'v8.5':>10s} | {'0050':>10s} | {'00981A':>10s}")
        print(f"   {'─'*70}")

        sr_ann = f"{sr_metrics['ann']:+.1f}%" if sr_metrics['trades'] > 0 else 'N/A'
        v85_ann = f"{v85_metrics['ann']:+.1f}%" if v85_metrics['trades'] > 0 else 'N/A'
        bm0050_ret = f"{bm_0050['total_ret']:+.1f}%" if bm_0050 else 'N/A'
        bm981a_ret = f"{bm_00981a['total_ret']:+.1f}%" if bm_00981a else '未上市'

        print(f"   {'年化報酬':16s} | {sr_ann:>10s} | {v85_ann:>10s} | {bm0050_ret:>10s} | {bm981a_ret:>10s}")

        sr_sh = f"{sr_metrics['sharpe']:.2f}" if sr_metrics['trades'] > 0 else 'N/A'
        v85_sh = f"{v85_metrics['sharpe']:.2f}" if v85_metrics['trades'] > 0 else 'N/A'
        bm0050_sh = f"{bm_0050['sharpe']:.2f}" if bm_0050 else 'N/A'
        bm981a_sh = f"{bm_00981a['sharpe']:.2f}" if bm_00981a else 'N/A'

        print(f"   {'Sharpe':16s} | {sr_sh:>10s} | {v85_sh:>10s} | {bm0050_sh:>10s} | {bm981a_sh:>10s}")

        sr_mdd = f"{sr_metrics['mdd']:.1f}%" if sr_metrics['trades'] > 0 else 'N/A'
        v85_mdd = f"{v85_metrics['mdd']:.1f}%" if v85_metrics['trades'] > 0 else 'N/A'
        bm0050_mdd = f"{bm_0050['mdd']:.1f}%" if bm_0050 else 'N/A'

        print(f"   {'MDD':16s} | {sr_mdd:>10s} | {v85_mdd:>10s} | {bm0050_mdd:>10s} | {'—':>10s}")

        sr_tr = f"{sr_metrics['trades']}" if sr_metrics['trades'] > 0 else '0'
        v85_tr = f"{v85_metrics['trades']}" if v85_metrics['trades'] > 0 else '0'
        print(f"   {'交易數':16s} | {sr_tr:>10s} | {v85_tr:>10s} | {'—':>10s} | {'—':>10s}")

        # 美股指標
        if us_ind:
            print(f"\n   📊 美股指標:")
            if 'spy_ret' in us_ind:
                print(f"      SPY 報酬: {us_ind['spy_ret']:+.1f}%  "
                      f"(低於MA60: {us_ind.get('spy_below_ma60_pct', 0):.0f}%天)")
            if 'vix_mean' in us_ind:
                print(f"      VIX 均值: {us_ind['vix_mean']:.1f}  "
                      f"峰值: {us_ind['vix_max']:.1f}  "
                      f"(>25: {us_ind.get('vix_above_25_pct', 0):.0f}% | "
                      f">30: {us_ind.get('vix_above_30_pct', 0):.0f}% | "
                      f">35: {us_ind.get('vix_above_35_pct', 0):.0f}%)")
            if 'sox_ret' in us_ind:
                print(f"      SOX 報酬: {us_ind['sox_ret']:+.1f}%  "
                      f"(低於MA60: {us_ind.get('sox_below_ma60_pct', 0):.0f}%天)")

        result = {
            'name': name,
            'label': info['label'],
            'period': f"{eval_start} → {eval_end}",
            'sr': sr_metrics,
            'v85': v85_metrics,
            'bm_0050': bm_0050,
            'bm_00981a': bm_00981a,
            'us': us_ind,
        }
        all_results.append(result)

    # ====== 總覽表 ======
    print(f"\n\n{'='*80}")
    print(f"📊 總覽")
    print(f"{'='*80}")

    header = (f"  {'期間':<14s} | {'SR v2':>8s} | {'v8.5':>8s} | "
              f"{'0050':>8s} | {'SR MDD':>8s} | {'v85 MDD':>8s} | {'VIX':>5s}")
    print(header)
    print(f"  {'-'*76}")

    for r in all_results:
        sr_sh = f"{r['sr']['sharpe']:.2f}" if r['sr']['trades'] > 0 else '—'
        v85_sh = f"{r['v85']['sharpe']:.2f}" if r['v85']['trades'] > 0 else '—'
        bm_sh = f"{r['bm_0050']['sharpe']:.2f}" if r['bm_0050'] else '—'
        sr_mdd = f"{r['sr']['mdd']:.0f}%" if r['sr']['trades'] > 0 else '—'
        v85_mdd = f"{r['v85']['mdd']:.0f}%" if r['v85']['trades'] > 0 else '—'
        vix = f"{r['us'].get('vix_mean', 0):.0f}" if r['us'] else '—'
        print(f"  {r['label']:<14s} | {sr_sh:>8s} | {v85_sh:>8s} | "
              f"{bm_sh:>8s} | {sr_mdd:>8s} | {v85_mdd:>8s} | {vix:>5s}")

    # ====== 弱勢分析 ======
    print(f"\n\n{'='*80}")
    print(f"🔍 弱勢期間分析 — SR v2 Sharpe < 0 的期間")
    print(f"{'='*80}")

    weak_periods = [r for r in all_results if r['sr']['sharpe'] < 0 and r['sr']['trades'] > 0]

    if weak_periods:
        for r in weak_periods:
            print(f"\n  {r['label']} ({r['period']}):")
            print(f"    SR v2:  Sharpe {r['sr']['sharpe']:.2f}, "
                  f"MDD {r['sr']['mdd']:.1f}%, 勝率 {r['sr']['win_rate']:.0f}%")
            if r['v85']['trades'] > 0:
                print(f"    v8.5:   Sharpe {r['v85']['sharpe']:.2f}, "
                      f"MDD {r['v85']['mdd']:.1f}%")

            us = r['us']
            if us:
                print(f"    ────────────────────────────────")
                print(f"    SPY: {us.get('spy_ret', 0):+.1f}%  "
                      f"(低於MA60: {us.get('spy_below_ma60_pct', 0):.0f}%天)")
                print(f"    VIX: 均值{us.get('vix_mean', 0):.1f} "
                      f"峰值{us.get('vix_max', 0):.1f} "
                      f"(>30: {us.get('vix_above_30_pct', 0):.0f}% "
                      f">35: {us.get('vix_above_35_pct', 0):.0f}%)")
                print(f"    SOX: {us.get('sox_ret', 0):+.1f}%  "
                      f"(低於MA60: {us.get('sox_below_ma60_pct', 0):.0f}%天)")

        # 共同特徵
        print(f"\n  📋 弱勢期間的共同指標特徵:")
        vix_means = [r['us'].get('vix_mean', 0) for r in weak_periods if r['us']]
        spy_below = [r['us'].get('spy_below_ma60_pct', 0) for r in weak_periods if r['us']]
        sox_below = [r['us'].get('sox_below_ma60_pct', 0) for r in weak_periods if r['us']]

        if vix_means:
            print(f"    VIX 均值: {min(vix_means):.1f} ~ {max(vix_means):.1f} "
                  f"(平均 {np.mean(vix_means):.1f})")
        if spy_below:
            print(f"    SPY 低於 MA60 天數比: {min(spy_below):.0f}% ~ {max(spy_below):.0f}% "
                  f"(平均 {np.mean(spy_below):.0f}%)")
        if sox_below:
            print(f"    SOX 低於 MA60 天數比: {min(sox_below):.0f}% ~ {max(sox_below):.0f}% "
                  f"(平均 {np.mean(sox_below):.0f}%)")

        print(f"\n  💡 權重調整建議:")
        if vix_means and np.mean(vix_means) > 25:
            print(f"    → VIX 門檻從 35 降至 {min(25, int(min(vix_means)))}")
            print(f"      弱勢期間 VIX 平均已達 {np.mean(vix_means):.0f}，"
                  f"現行門檻 35 太寬鬆")
        if spy_below and np.mean(spy_below) > 50:
            print(f"    → SPY < MA60 時曝險應從 0.4 降至 0.2 或更低")
            print(f"      弱勢期間 SPY 平均 {np.mean(spy_below):.0f}% 天數低於 MA60")
        if sox_below and np.mean(sox_below) > 60:
            print(f"    → SOX < MA60 + VIX > 25 時，所有板塊（不只科技）應減半")
    else:
        print("  ✅ 沒有 Sharpe < 0 的期間")

    # ====== 00981A 共存期比較 ======
    coexist = [r for r in all_results if r['bm_00981a'] is not None]
    if coexist:
        print(f"\n\n{'='*80}")
        print(f"📊 00981A 共存期比較 (2025-05 至今)")
        print(f"{'='*80}")
        for r in coexist:
            sr_ann = r['sr']['ann']
            bm_ret = r['bm_00981a']['total_ret']
            diff = sr_ann - bm_ret if r['sr']['trades'] > 0 else 0
            icon = '✅' if diff > 0 else '🔴'
            print(f"  {r['label']}: SR v2 {sr_ann:+.1f}% vs 00981A {bm_ret:+.1f}% "
                  f"({icon} 差距 {diff:+.1f}%)")


if __name__ == '__main__':
    main()
