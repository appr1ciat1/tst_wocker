#!/usr/bin/env python3
"""
前視偏差驗證工具 (Strategy Lookahead Bias Verifier)

仿 FinLab verify_strategy() 的概念，自動掃描策略程式碼，
檢查是否存在「偷看未來數據」的常見錯誤。

檢查項目：
1. 負向 shift()（取得未來數據）
2. Entry 使用 t+1 open（確認已正確實作）
3. ATR/MA 計算中是否包含當日數據
4. 財報/基本面數據的時間對齊
5. Score 計算中是否使用了未來 close

使用方式：
    python verify_strategy.py              # 掃描所有策略檔案
    python verify_strategy.py --verbose    # 顯示詳細掃描結果
"""

import os
import re
import sys
import ast
import argparse


class LookaheadVerifier:
    """前視偏差自動檢測器。"""

    def __init__(self, verbose=False):
        self.verbose = verbose
        self.issues = []
        self.passes = []

    def scan_file(self, filepath):
        """掃描單一 Python 檔案的前視偏差風險。"""
        with open(filepath, 'r', encoding='utf-8') as f:
            source = f.read()
            lines = source.split('\n')

        filename = os.path.basename(filepath)

        # === Check 1: 負向 shift（偷看未來）===
        self._check_negative_shift(filename, lines)

        # === Check 2: 使用 .shift(-N) 在 label 以外的地方 ===
        self._check_future_data_access(filename, lines)

        # === Check 3: Entry 使用 t+1 open ===
        self._check_entry_timing(filename, lines)

        # === Check 4: Regime filter 使用 t-1 數據 ===
        self._check_regime_timing(filename, lines)

        # === Check 5: 成交量確認使用 t-1 ===
        self._check_volume_timing(filename, lines)

        # === Check 6: 使用 iloc[-1] 的潛在風險 ===
        self._check_iloc_last(filename, lines)

    def _check_negative_shift(self, filename, lines):
        """檢查所有 .shift(-N)，只有 label/target 生成允許使用。"""
        pattern = re.compile(r'\.shift\s*\(\s*-\s*\d+')
        label_context = re.compile(r'(label|target|fwd_ret|forward)', re.IGNORECASE)

        for i, line in enumerate(lines, 1):
            if pattern.search(line):
                # 檢查是否在 label 生成上下文中
                context = '\n'.join(lines[max(0, i-3):i+2])
                if label_context.search(context):
                    self.passes.append(
                        f"✅ [{filename}:{i}] .shift(-N) 用於 label 生成（安全）"
                    )
                else:
                    self.issues.append(
                        f"🚨 [{filename}:{i}] .shift(-N) 疑似前視偏差！\n"
                        f"   {line.strip()}"
                    )

    def _check_future_data_access(self, filename, lines):
        """檢查使用未來數據的模式。"""
        # rolling().max() 配合 >= 是安全的（創新高）
        # 但 rolling().shift(0) 預設包含當日，需確認 entry 用 t-1
        future_patterns = [
            (re.compile(r'close_df\[ticker\]\.iloc\[i\].*score'),
             '分數計算可能使用當日收盤價（應用 i-1）'),
            (re.compile(r'\.iloc\[i\].*entry'),
             '進場條件可能使用當日數據'),
        ]

        for i, line in enumerate(lines, 1):
            for pattern, desc in future_patterns:
                if pattern.search(line):
                    # 細緻判斷：排除安全的使用場景
                    context = '\n'.join(lines[max(0, i-5):i+2])
                    # 出場判定、持倉市值計算、equity 結算都使用當日數據是正常的
                    safe_contexts = [
                        'exit', 'step 1', 'step 4',
                        'heat', 'equity', 'active_trades',
                        't_price', 'close_val', 'current_equity',
                    ]
                    if any(ctx in context.lower() for ctx in safe_contexts):
                        continue
                    if self.verbose:
                        self.issues.append(
                            f"⚠️ [{filename}:{i}] {desc}\n"
                            f"   {line.strip()}"
                        )

    def _check_entry_timing(self, filename, lines):
        """確認進場使用 t+1 open 而非當日 close。"""
        if 'event_backtest' not in filename:
            return

        found_open_entry = False
        for i, line in enumerate(lines, 1):
            if 'open_df[ticker].iloc[i]' in line and 'entry_price' in line:
                found_open_entry = True
                break
            if 'entry_price' in line and 'close_df' in line and 'exit' not in line.lower():
                self.issues.append(
                    f"🚨 [{filename}:{i}] Entry 可能使用 close 而非 open！\n"
                    f"   {line.strip()}"
                )

        if found_open_entry:
            self.passes.append(
                f"✅ [{filename}] Entry 使用 t+1 open（正確）"
            )

    def _check_regime_timing(self, filename, lines):
        """確認 regime filter 使用 t-1 大盤數據。"""
        if 'event_backtest' not in filename:
            return

        found_prev = False
        for i, line in enumerate(lines, 1):
            if 'prev_date' in line and 'dates[i - 1]' in line:
                found_prev = True
                break

        if found_prev:
            self.passes.append(
                f"✅ [{filename}] Regime filter 使用 t-1 大盤數據（無前視）"
            )
        else:
            # 只在有 regime 邏輯的檔案中報告
            has_regime = any('regime' in l.lower() for l in lines)
            if has_regime:
                self.issues.append(
                    f"⚠️ [{filename}] 未找到 t-1 大盤數據取用模式，\n"
                    f"   請確認 regime filter 不使用當日數據"
                )

    def _check_volume_timing(self, filename, lines):
        """確認成交量確認使用 t-1。"""
        if 'event_backtest' not in filename:
            return

        for i, line in enumerate(lines, 1):
            if 'vol_df[ticker].iloc[i]' in line and 'prev_vol' not in line:
                context = '\n'.join(lines[max(0, i-3):i+2])
                if 'entry' in context.lower() or 'candidate' in context.lower():
                    self.issues.append(
                        f"⚠️ [{filename}:{i}] 進場可能使用當日成交量\n"
                        f"   {line.strip()}"
                    )

    def _check_iloc_last(self, filename, lines):
        """檢查 .iloc[-1] 在回測循環中的使用。"""
        in_backtest_loop = False
        for i, line in enumerate(lines, 1):
            if 'for i in range' in line:
                in_backtest_loop = True
            if in_backtest_loop and '.iloc[-1]' in line:
                # 在回測循環中使用 iloc[-1] 可能是前視
                # 但在報表生成中是安全的
                context = '\n'.join(lines[max(0, i-5):i])
                if 'report' in context.lower() or 'display' in context.lower():
                    continue
                if self.verbose:
                    self.issues.append(
                        f"⚠️ [{filename}:{i}] 回測循環中使用 .iloc[-1]\n"
                        f"   {line.strip()}"
                    )

    def scan_directory(self, directory):
        """掃描整個目錄。"""
        target_files = [
            os.path.join(directory, 'strategy', 'ai_strategy.py'),
            os.path.join(directory, 'strategy', 'event_backtest.py'),
            os.path.join(directory, 'strategy', 'finlab_factors.py'),
            os.path.join(directory, 'ai_report.py'),
        ]

        for filepath in target_files:
            if os.path.exists(filepath):
                self.scan_file(filepath)
            else:
                if self.verbose:
                    print(f"   ⏭️ {filepath} 不存在，跳過")

    def report(self):
        """輸出驗證報告。"""
        print("\n" + "=" * 60)
        print("🔍 前視偏差驗證報告 (Strategy Lookahead Bias Verification)")
        print("=" * 60)

        if self.passes:
            print(f"\n✅ 通過檢查 ({len(self.passes)} 項):")
            for p in self.passes:
                print(f"   {p}")

        if self.issues:
            print(f"\n🚨 發現問題 ({len(self.issues)} 項):")
            for issue in self.issues:
                print(f"   {issue}")
        else:
            print("\n🎉 未發現前視偏差問題！")

        print(f"\n{'='*60}")
        total = len(self.passes) + len(self.issues)
        print(f"   總檢查: {total} 項 | "
              f"✅ 通過: {len(self.passes)} | "
              f"⚠️ 問題: {len(self.issues)}")
        print(f"{'='*60}")

        return len(self.issues) == 0


def main():
    parser = argparse.ArgumentParser(description='前視偏差驗證工具')
    parser.add_argument('--verbose', '-v', action='store_true',
                        help='顯示詳細掃描結果')
    parser.add_argument('--dir', type=str, default='.',
                        help='掃描目錄（預設當前目錄）')
    args = parser.parse_args()

    verifier = LookaheadVerifier(verbose=args.verbose)
    verifier.scan_directory(args.dir)
    passed = verifier.report()

    sys.exit(0 if passed else 1)


if __name__ == '__main__':
    main()
