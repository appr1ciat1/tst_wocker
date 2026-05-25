#!/usr/bin/env python3
"""
歷史危機壓力測試 (Historical Crisis Stress Test)

在已知歷史危機期間跑策略回測，測試策略在極端市場環境下的真實表現。

測試期間：
- COVID 崩盤 (2020-01 ~ 2020-06)：大盤 -30%，V 型反轉
- COVID 復甦 (2020-06 ~ 2021-06)：航運 + 半導體超級多頭
- 升息衝擊 (2022-01 ~ 2022-10)：大盤 -25%，電子崩跌
- AI 行情 (2023-01 ~ 2024-06)：半導體主導，窄幅上漲
- 近期 (2024-06 ~ now)：高檔震盪

使用方式:
  python crisis_test.py               # 跑所有危機期間
  python crisis_test.py --period covid_crash  # 只跑某一段
"""

import subprocess
import re
import sys
import argparse
from datetime import datetime, timedelta


# 每段危機 = (名稱, 取資料起始, 回測起始, 回測結束, 市場描述)
# 取資料起始比回測起始早 120 天，用於 MA60 暖機
CRISIS_PERIODS = {
    'covid_crash': {
        'label': '🦠 COVID 崩盤',
        'fetch_start': '2019-09-01',
        'eval_start': '2020-01-01',
        'eval_end': '2020-06-30',
        'description': '大盤 -30%，3/19 暴跌後 V 型反轉',
    },
    'covid_recovery': {
        'label': '📈 疫後復甦',
        'fetch_start': '2020-02-01',
        'eval_start': '2020-06-01',
        'eval_end': '2021-06-30',
        'description': '航運 + 半導體超級多頭，大盤 +80%',
    },
    'rate_hike': {
        'label': '📉 升息衝擊',
        'fetch_start': '2021-09-01',
        'eval_start': '2022-01-01',
        'eval_end': '2022-10-31',
        'description': 'Fed 暴力升息，台股 -25%，電子崩跌',
    },
    'ai_rally': {
        'label': '🤖 AI 行情',
        'fetch_start': '2022-09-01',
        'eval_start': '2023-01-01',
        'eval_end': '2024-06-30',
        'description': '半導體主導，NVDA +200%，窄幅上漲',
    },
    'recent': {
        'label': '📊 近期',
        'fetch_start': '2024-02-01',
        'eval_start': '2024-06-01',
        'eval_end': datetime.today().strftime('%Y-%m-%d'),
        'description': '高檔震盪，市場分歧',
    },
}


def run_backtest_range(start_date, end_date, eval_start=None):
    """Run ai_report.py with explicit date range and extract metrics."""
    cmd = (f'{sys.executable} ai_report.py '
           f'--start-date {start_date} --end-date {end_date}')
    if eval_start:
        cmd += f' --eval-start {eval_start}'
    r = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=300)
    out = r.stdout + r.stderr
    if r.returncode != 0:
        raise RuntimeError(
            f"Backtest command failed with exit code {r.returncode}: {cmd}\n"
            f"{out[-2000:]}"
        )

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
        'sortino': get(r'Sortino Ratio:\s+([\+\-\d\.]+)', 'Sortino'),
        'calmar': get(r'Calmar Ratio:\s+([\+\-\d\.]+)', 'Calmar'),
        'mdd': get(r'最大回撤:\s+([\+\-\d\.]+)%', 'max drawdown'),
        'trades': int(get_first([r'總交易數:\s+(\d+)', r'共 (\d+) 筆交易'], 'trade count')),
        'win_rate': get_first([r'勝率[:：]\s*([\d\.]+)%', r'勝率\s*([\d\.]+)%'], 'win rate'),
        'pf': get(r'Profit Factor:\s+(inf|[\d\.]+)', 'profit factor'),
    }


def main():
    parser = argparse.ArgumentParser(
        description='歷史危機壓力測試'
    )
    parser.add_argument('--period', type=str, default=None,
                        choices=list(CRISIS_PERIODS.keys()),
                        help='只跑指定期間 (預設: 全部)')
    args = parser.parse_args()

    if args.period:
        periods = {args.period: CRISIS_PERIODS[args.period]}
    else:
        periods = CRISIS_PERIODS

    print(f"{'='*70}")
    print(f"🔥 歷史危機壓力測試 — {datetime.now().strftime('%Y-%m-%d')}")
    print(f"{'='*70}")
    print(f"   使用 v8.5 固定參數在各歷史危機期間回測")
    print(f"   每段含 ~120 天暖機期 (MA60 + buffer)，績效只統計危機 eval window")
    print()

    results = []

    for name, info in periods.items():
        period_str = f"{info['eval_start']} → {info['eval_end']}"
        print(f"\n{'─'*70}")
        print(f"{info['label']}  ({period_str})")
        print(f"   {info['description']}")
        print(f"{'─'*70}")

        try:
            metrics = run_backtest_range(
                info['fetch_start'], info['eval_end'], eval_start=info['eval_start']
            )

            # Color indicators
            sh_icon = '✅' if metrics['sharpe'] >= 1.0 else ('⚠️' if metrics['sharpe'] >= 0 else '🔴')
            mdd_icon = '✅' if metrics['mdd'] >= -20 else ('⚠️' if metrics['mdd'] >= -30 else '🔴')

            print(f"   年化報酬:  {metrics['ann']:>+7.1f}%")
            print(f"   Sharpe:    {metrics['sharpe']:>7.3f}  {sh_icon}")
            print(f"   MDD:       {metrics['mdd']:>7.1f}%  {mdd_icon}")
            print(f"   Calmar:    {metrics['calmar']:>7.3f}")
            print(f"   勝率:      {metrics['win_rate']:>7.1f}%")
            print(f"   Profit F:  {metrics['pf']:>7.2f}")
            print(f"   交易數:    {metrics['trades']:>7d}")

            results.append({
                'name': name,
                'label': info['label'],
                'period': period_str,
                **metrics,
            })

        except Exception as e:
            print(f"   ❌ FAILED: {e}")

    # Summary table
    if len(results) > 1:
        print(f"\n{'='*70}")
        print(f"📊 總覽")
        print(f"{'='*70}")

        header = (f"{'期間':<14s} | {'年化':>7s} | {'Sharpe':>6s} | "
                  f"{'MDD':>7s} | {'Calmar':>6s} | {'WR':>5s} | {'#Tr':>4s}")
        print(header)
        print('-' * len(header))

        for r in results:
            sh_icon = '✅' if r['sharpe'] >= 1.0 else ('⚠️' if r['sharpe'] >= 0 else '🔴')
            print(f"{r['label']:<14s} | {r['ann']:>+6.1f}% | "
                  f"{r['sharpe']:>5.2f} {sh_icon} | {r['mdd']:>6.1f}% | "
                  f"{r['calmar']:>6.2f} | {r['win_rate']:>4.1f}% | {r['trades']:>4d}")

        print('-' * len(header))

        # Worst case analysis
        if results:
            worst_sharpe = min(results, key=lambda x: x['sharpe'])
            worst_mdd = min(results, key=lambda x: x['mdd'])
            best_sharpe = max(results, key=lambda x: x['sharpe'])

            print(f"\n🔴 最差 Sharpe: {worst_sharpe['label']} ({worst_sharpe['sharpe']:.2f})")
            print(f"🔴 最深 MDD:    {worst_mdd['label']} ({worst_mdd['mdd']:.1f}%)")
            print(f"🟢 最佳 Sharpe: {best_sharpe['label']} ({best_sharpe['sharpe']:.2f})")

            # Key insight
            print(f"\n💡 關鍵洞察:")
            if worst_sharpe['sharpe'] < 0:
                print(f"   🚨 策略在 {worst_sharpe['label']} 期間是虧損的！")
                print(f"      這代表策略在此類市場環境下完全失效。")
            elif worst_sharpe['sharpe'] < 1.0:
                print(f"   ⚠️  策略在 {worst_sharpe['label']} 表現平庸 (Sharpe < 1.0)。")
                print(f"      遇到類似環境時應考慮降低曝險或暫停策略。")
            else:
                print(f"   ✅ 策略在所有歷史危機中 Sharpe > 1.0。")

            if worst_mdd['mdd'] < -30:
                print(f"   🚨 {worst_mdd['label']} 的 MDD 為 {worst_mdd['mdd']:.1f}%！")
                print(f"      起始資金需足以承受此回撤而不被迫砍倉。")


if __name__ == '__main__':
    main()
