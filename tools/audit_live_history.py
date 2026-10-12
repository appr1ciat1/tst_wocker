#!/usr/bin/env python3
"""
tools/audit_live_history.py — 用「每天上線產生的報表」這份樣本外紀錄，稽核資料品質關卡。

做法：從 git 歷史取出每一次 stock_report.html，逐次判斷當天的價格是否為真值，
再模擬現行關卡（假 bar 過濾、新鮮度、盤中 bar、代號股池檢查）會不會放行。
期望結果：正常執行全數放行、有問題的執行全數攔下。

需要完整 git 歷史（本機 clone；CI 的 fetch-depth 1 不夠）。
用法：python tools/audit_live_history.py [--since 2026-04-01] [--closed 2026-07-10,...]
  --closed：XTAI 行事曆不知道的臨時休市日（颱風等），用來標記真值。
"""
import argparse
import json
import os
import re
import subprocess
import sys
from collections import Counter, defaultdict
from datetime import datetime, timezone

import pandas as pd

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, ROOT)
from strategy.data_quality import (  # noqa: E402
    expected_latest_session, is_session_closed, is_valid_tw_code,
)


def git(*a):
    return subprocess.check_output(['git', '-C', ROOT, *a], text=True, errors='replace')


def parse_report(page):
    m = re.search(r'報表日期: ([0-9-]+)', page)
    rows = []
    for code, body in re.findall(r'<tr><td>([^<]{2,12})</td>(.*?)</tr>', page, re.DOTALL):
        if '建議買進' not in body and '候選' not in body:
            continue
        cells = re.findall(r'<td>(.*?)</td>', body, re.DOTALL)
        tp = re.search(r'停利:</b> <span[^>]*>[\d.]+</span> \(\+([\d.]+)%\)', body)
        rows.append({'code': code.strip(), 'buy': '建議買進' in body,
                     'price': cells[1] if len(cells) > 1 else None,
                     'tp_pct': float(tp.group(1)) if tp else None})
    return (m.group(1) if m else None), rows


_universe_cache = {}


def universe_at(sha):
    blob = git('rev-parse', f'{sha}:ai_report.py').strip()
    if blob not in _universe_cache:
        m = re.search(r'EXTENDED_TICKERS = \[(.*?)\n\]', git('show', f'{sha}:ai_report.py'), re.S)
        _universe_cache[blob] = set(re.findall(r"'(\d{4,6}[A-Z]?)'", m.group(1))) if m else None
    return _universe_cache[blob]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--since', default='2026-04-01')
    ap.add_argument('--closed', default='2026-07-10', help='臨時休市日，逗號分隔')
    args = ap.parse_args()
    closed = set(filter(None, args.closed.split(',')))
    import exchange_calendars as xcals
    cal = xcals.get_calendar('XTAI')

    runs = []
    for line in git('log', f'--since={args.since}', '--format=%H %cI', '--', 'stock_report.html').splitlines():
        sha, ci = line.split(' ', 1)
        d, rows = parse_report(git('show', f'{sha}:stock_report.html'))
        if d:
            runs.append({'sha': sha[:7], 'now': datetime.fromisoformat(ci).astimezone(timezone.utc),
                         'date': d, 'rows': rows})
    runs.sort(key=lambda r: r['now'])
    px = lambda r: {x['code']: x['price'] for x in r['rows']}
    fixed = lambda r: any(x['buy'] for x in r['rows']) and all(
        x['tp_pct'] == 15.0 for x in r['rows'] if x['buy'] and x['tp_pct'] is not None)
    intraday = lambda r: not is_session_closed(pd.Timestamp(r['date']), r['now'])

    votes = defaultdict(Counter)   # 每個日期的真值價格：完整、非固定%、非盤中執行的多數決
    for r in runs:
        if not fixed(r) and not intraday(r):
            votes[r['date']][json.dumps(sorted(px(r).items()))] += 1
    truth = {d: dict(json.loads(v.most_common(1)[0][0])) for d, v in votes.items()}

    def agree(a, b):
        common = [k for k in a if k in b]
        return len(common) >= 3 and all(a[k] == b[k] for k in common)

    table = []
    for r in runs:
        d = r['date']
        ps = cal.date_to_session(pd.Timestamp(d), 'previous')
        ps = (cal.previous_session(ps) if ps == pd.Timestamp(d) else ps).strftime('%Y-%m-%d')
        if intraday(r):
            kind = 'INTRADAY'
        elif d in closed:
            kind = 'CLOSED_DAY'
        elif agree(px(r), truth.get(ps, {})) and not agree(px(r), truth.get(d, {})):
            kind = 'STALE_ROW'
        elif fixed(r):
            kind = 'NO_HL'
        else:
            kind = 'OK'
        exp = expected_latest_session(r['now']).strftime('%Y-%m-%d')
        latest = ps if kind != 'OK' else d
        gate = 'PASS' if latest >= exp else 'ABORT'
        if kind == 'INTRADAY' and gate == 'PASS':
            gate = 'TRIM'
        bad = kind != 'OK' or d < exp
        U = universe_at(r['sha'])
        rejected = [x['code'] for x in r['rows'] if x['buy']
                    and not (is_valid_tw_code(x['code']) and (U is None or x['code'] in U))]
        table.append(dict(sha=r['sha'], run_utc=r['now'].strftime('%Y-%m-%d %H:%M'), date=d,
                          expected=exp, kind=kind, gate=gate, bad=bad, rejected=','.join(rejected)))
    df = pd.DataFrame(table)
    print(f'上線執行 {len(df)} 次（{df["date"].min()} → {df["date"].max()}）')
    print(df['kind'].value_counts().to_string(), '\n')
    print('「舊流程是否發出有問題的訂單」 × 「現行關卡判定」')
    print(pd.crosstab(df['bad'], df['gate']).to_string(), '\n')
    miss = df[df['bad'] & (df['gate'] == 'PASS')]
    false_alarm = df[~df['bad'] & (df['gate'] != 'PASS')]
    print('有問題的執行：'); print(df[df['bad']].to_string(index=False), '\n')
    rej = df[df['rejected'] != '']
    print(f'代號關卡會拒收的歷史買進列：{rej["rejected"].str.split(",").explode().value_counts().to_dict()}')
    print(f'\n漏放 {len(miss)} 次、誤攔 {len(false_alarm)} 次 →',
          '✅ 通過' if miss.empty and false_alarm.empty else '❌ 需要檢查')
    return 0 if miss.empty and false_alarm.empty else 1


if __name__ == '__main__':
    sys.exit(main())
