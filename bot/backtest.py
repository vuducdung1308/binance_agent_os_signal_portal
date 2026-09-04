"""Backtest the exact production signal logic (compute_entry_signal / compute_exit_event
from src/signal_engine.py — no separate reimplementation) against historical candles.

Walks forward causally: at each step only candles up to that point are visible, so
this cannot suffer the look-ahead bug we just fixed in price_data.py.

Assumes a 50/50 partial exit at TP1 (common convention): half the position books at
TP1 (which sits at +2R by construction) and the stop moves to breakeven for the rest,
matching src/position_tracker.py's move_stop_to_breakeven behavior. PnL is tracked in
R-multiples (multiples of the initial risk) since that's the standard way to evaluate
a system independent of position size.

Usage:
    python backtest.py BTC --days 30
    python backtest.py ETH --days 60 --timeframe 1h
"""

from __future__ import annotations

import argparse
from dataclasses import dataclass, field

from src.price_data import fetch_klines_history
from src.signal_engine import (
    DEFAULT_PARAMS,
    SignalParams,
    compute_entry_signal,
    compute_exit_event,
    min_candles_required,
)

TP1_R_MULTIPLE = 2.0  # must match signal_engine.TP1_RISK_MULTIPLE
PARTIAL_EXIT_WEIGHT = 0.5  # fraction of the position booked at TP1


@dataclass
class ClosedTrade:
    symbol: str
    direction: str
    setup: str
    entry_idx: int
    exit_idx: int
    entry_price: float
    r_multiple: float
    entry_rsi: float = 0.0
    entry_adx: float = 0.0
    hold_candles: int = 0
    exit_reasons: list[str] = field(default_factory=list)


def run_backtest(
    symbol: str, timeframe: str, test_days: int, params: SignalParams = DEFAULT_PARAMS
) -> list[ClosedTrade]:
    candles_per_day = {"1h": 24, "30m": 48, "15m": 96}.get(timeframe, 24)
    warmup = min_candles_required(params) + 5
    test_candles_needed = test_days * candles_per_day
    total_needed = warmup + test_candles_needed

    all_candles = fetch_klines_history(symbol, interval=timeframe, total_candles=total_needed)
    if len(all_candles) < warmup + 10:
        raise RuntimeError(f"Không đủ dữ liệu lịch sử ({len(all_candles)} nến, cần tối thiểu {warmup + 10})")

    start_idx = max(warmup, len(all_candles) - test_candles_needed)

    trades: list[ClosedTrade] = []
    position: dict | None = None
    entry_idx: int | None = None
    entry_rsi: float = 0.0
    entry_adx: float = 0.0
    initial_risk: float = 0.0
    partial_r_booked: float = 0.0

    for i in range(start_idx, len(all_candles)):
        window = all_candles[: i + 1]

        if position is None:
            signal = compute_entry_signal(symbol, timeframe, window, params)
            if signal is not None:
                position = {
                    "symbol": signal.symbol,
                    "direction": signal.direction,
                    "setup": signal.setup,
                    "entry": signal.entry,
                    "stop_loss": signal.stop_loss,
                    "take_profit_1": signal.take_profit_1,
                    "take_profit_2": signal.take_profit_2,
                    "tp1_hit": False,
                }
                entry_idx = i
                entry_rsi = signal.rsi_value
                entry_adx = signal.adx_value
                initial_risk = abs(signal.entry - signal.stop_loss)
                partial_r_booked = 0.0
            continue

        exit_event = compute_exit_event(position, window, params)
        if exit_event is None:
            continue

        if exit_event.reason == "tp1_hit":
            partial_r_booked = PARTIAL_EXIT_WEIGHT * TP1_R_MULTIPLE
            position["stop_loss"] = position["entry"]
            position["tp1_hit"] = True
            continue

        # position fully closes here
        move = (
            exit_event.price - position["entry"]
            if position["direction"] == "LONG"
            else position["entry"] - exit_event.price
        )
        remaining_weight = PARTIAL_EXIT_WEIGHT if position["tp1_hit"] else 1.0
        remaining_r = remaining_weight * (move / initial_risk if initial_risk else 0.0)
        total_r = partial_r_booked + remaining_r

        trades.append(
            ClosedTrade(
                symbol=symbol,
                direction=position["direction"],
                setup=position["setup"],
                entry_idx=entry_idx,
                exit_idx=i,
                entry_price=position["entry"],
                r_multiple=total_r,
                entry_rsi=entry_rsi,
                entry_adx=entry_adx,
                hold_candles=i - entry_idx,
                exit_reasons=(["tp1_hit"] if position["tp1_hit"] else []) + [exit_event.reason],
            )
        )
        position = None
        entry_idx = None

    return trades


def summarize(trades: list[ClosedTrade]) -> None:
    if not trades:
        print("Không có giao dịch nào được kích hoạt trong giai đoạn backtest.")
        return

    wins = [t for t in trades if t.r_multiple > 0]
    losses = [t for t in trades if t.r_multiple <= 0]
    total_r = sum(t.r_multiple for t in trades)
    gross_win_r = sum(t.r_multiple for t in wins)
    gross_loss_r = abs(sum(t.r_multiple for t in losses))
    win_rate = len(wins) / len(trades) * 100
    avg_r = total_r / len(trades)
    profit_factor = (gross_win_r / gross_loss_r) if gross_loss_r else float("inf")

    # running R equity curve -> max drawdown in R
    equity = 0.0
    peak = 0.0
    max_dd = 0.0
    for t in trades:
        equity += t.r_multiple
        peak = max(peak, equity)
        max_dd = min(max_dd, equity - peak)

    print(f"Tổng số giao dịch: {len(trades)}")
    print(f"Tỉ lệ thắng: {win_rate:.1f}% ({len(wins)} thắng / {len(losses)} thua)")
    print(f"Tổng lợi nhuận: {total_r:+.2f}R")
    print(f"R trung bình/lệnh: {avg_r:+.2f}R")
    print(f"Profit factor: {profit_factor:.2f}")
    print(f"Max drawdown: {max_dd:.2f}R")
    print()
    print("Chi tiết từng lệnh:")
    for t in trades:
        print(
            f"  [{t.setup:14s}] {t.direction:5s} entry_idx={t.entry_idx} hold={t.hold_candles:3d}nến "
            f"entry={t.entry_price:.4g} RSI={t.entry_rsi:.0f} ADX={t.entry_adx:.0f} "
            f"R={t.r_multiple:+.2f} lý do={'+'.join(t.exit_reasons)}"
        )

    by_setup: dict[str, list[ClosedTrade]] = {}
    for t in trades:
        by_setup.setdefault(t.setup, []).append(t)
    print()
    print("Theo kiểu setup:")
    for setup, ts in by_setup.items():
        w = len([t for t in ts if t.r_multiple > 0])
        print(f"  {setup}: {len(ts)} lệnh, {w}/{len(ts)} thắng ({w/len(ts)*100:.0f}%), tổng {sum(t.r_multiple for t in ts):+.2f}R")


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("coin", help="Ký hiệu coin, vd BTC")
    parser.add_argument("--timeframe", default="1h")
    parser.add_argument("--days", type=int, default=30)
    args = parser.parse_args()

    symbol = f"{args.coin.upper()}USDT"
    print(f"Backtest {symbol} ({args.timeframe}), {args.days} ngày gần nhất...")
    print("(Dùng đúng logic sản xuất trong src/signal_engine.py, đi tuần tự theo thời gian, không nhìn trước.)")
    print()
    trades = run_backtest(symbol, args.timeframe, args.days)
    summarize(trades)
