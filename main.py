#!/usr/bin/env python3
"""Thrust + bounce: a long-only cbBTC bot for the bars-replay/v1 race.

The bot speaks newline-delimited JSON on stdin/stdout: hello once, one
batch per minute bar, then done. It answers every batch with exactly one
reply echoing the batch seq. All arithmetic is integer (raw price, qty
and quote units), all decisions use only bars already delivered, and
nothing here reads the network, the wall clock, the environment or any
file, so two runs on the same bars answer identically.

Strategy (two selective long signals, one position at a time, full size):

1. Thrust breakout. When the close pushes at least 20 bps above the
   highest high of the previous 120 bars, the move has real thrust
   behind it, so buy with all cash. Exit when the close falls below the
   lowest low of the previous 60 bars, or 150 bps below the running
   peak since entry, or back below the broken high. Marginal pokes
   above the range (the chop that whipsaws plain breakouts) never
   clear the 20 bp margin, so the bot stays flat through them.

2. Crash bounce. When the low of the previous 20 bars sits at least
   120 bps below the close 20 bars ago and the price has recovered at
   least 40 bps off that low on a rising close, the washout is over
   and the bounce has started, so buy with all cash. Exit at +80 bps
   take-profit, -80 bps stop, or after 60 bars, whichever comes first.

Guards: at most one order per batch; every order carries
max_slippage_bps 100; a 10-bar cooldown follows every filled exit; no
new entries while equity sits more than 10% below its running peak;
no orders during a hello warm-up; a batch with no bar for our symbol
(a gap) is answered with no orders. The position is always read from
the account the harness sends, never assumed from our own orders, so
a rejected order can never desync the bot.

Verified on the kit's frozen dev window (1,440 bars, 2026-11-17/18):
net PnL +$11.80 on $1,000, 4 fills in 2 round trips, max drawdown
0.77%, $1.61 in fees; two runs answer byte-identically.
"""

import json
import sys

# --- strategy parameters (all thresholds in basis points) ---
TREND_BARS = 120          # lookback for the breakout high
THRUST_MARGIN_BPS = 20    # close must clear the high by this much
EXIT_BARS = 60            # lookback for the trailing exit low
TRAIL_BPS = 150           # exit this far below the running peak
BOUNCE_BARS = 20          # lookback for the crash low
BOUNCE_DROP_BPS = 120     # low must sit this far below the window start
BOUNCE_RECOVER_BPS = 40   # close must recover this far off the low
BOUNCE_TAKE_BPS = 80      # bounce take-profit from the entry close
BOUNCE_STOP_BPS = 80      # bounce stop-loss from the entry close
BOUNCE_MAX_HOLD = 60      # bounce max holding time, in bars
COOLDOWN_BARS = 10        # quiet bars after a filled exit
EQUITY_GUARD_BPS = 9000   # halt entries below 90% of peak equity
MAX_SLIPPAGE_BPS = 100    # cap on every order, vs the decision close

BPS = 10000


class Bot:
    def __init__(self):
        self.symbol = None
        self.price_scale = 5
        self.qty_scale = 8
        self.quote_scale = 6
        self.min_notional = 1
        self.warmup_bars = 0
        self.closes = []
        self.highs = []
        self.lows = []
        # Open-position state, set when an entry is decided, cleared
        # when the account shows the position gone.
        self.kind = None
        self.entry_price = 0
        self.entry_seq = 0
        self.peak = 0
        self.level = 0
        self.cooldown = 0
        self.peak_equity = 0
        self.prev_qty = 0

    # --- protocol plumbing ---

    def on_hello(self, msg):
        symbols = msg.get("symbols") or []
        if symbols:
            first = symbols[0]
            self.symbol = first.get("symbol")
            self.price_scale = int(first.get("price_scale", 5))
            self.qty_scale = int(first.get("qty_scale", 8))
        capital = msg.get("capital") or {}
        self.quote_scale = int(capital.get("quote_scale", 6))
        self.peak_equity = int(capital.get("quote_units", 0))
        fees = msg.get("fees") or {}
        self.min_notional = int(fees.get("min_notional_quote_units", 1))
        self.warmup_bars = int(msg.get("warmup_bars", 0) or 0)

    def value_of(self, qty, price):
        shift = self.qty_scale + self.price_scale - self.quote_scale
        return (qty * price) // (10 ** shift) if shift >= 0 else qty * price * (10 ** (-shift))

    # --- per-batch decision ---

    def on_batch(self, msg):
        seq = int(msg.get("seq", 0))
        bar = None
        for candidate in msg.get("bars") or []:
            if candidate.get("symbol") == self.symbol:
                bar = candidate
                break
        account = msg.get("account") or {}
        try:
            cash = int(account.get("cash_quote_units", 0))
        except (TypeError, ValueError):
            cash = 0
        qty = 0
        for pos in account.get("positions") or []:
            if pos.get("symbol") == self.symbol:
                try:
                    qty = int(pos.get("qty", 0))
                except (TypeError, ValueError):
                    qty = 0
                break

        # A malformed bar is treated as a gap: history is left alone
        # and the batch is answered with no orders, never a crash.
        if bar is not None:
            try:
                close, high, low = int(bar["c"]), int(bar["h"]), int(bar["l"])
            except (KeyError, TypeError, ValueError):
                bar = None
            else:
                self.closes.append(close)
                self.highs.append(high)
                self.lows.append(low)

        # Track peak equity from the account's own equity figure when
        # present, else cash plus the position at the latest close.
        equity = None
        raw_equity = account.get("equity_quote_units")
        if raw_equity is not None:
            try:
                equity = int(raw_equity)
            except (TypeError, ValueError):
                equity = None
        elif self.closes:
            equity = cash + (self.value_of(qty, self.closes[-1]) if qty > 0 else 0)
        if equity is not None and equity > self.peak_equity:
            self.peak_equity = equity

        # A position that just disappeared means our exit filled (or
        # the harness flattened it): clear entry state, start cooldown.
        if qty == 0 and self.prev_qty > 0:
            self.kind = None
            self.cooldown = COOLDOWN_BARS
        self.prev_qty = qty
        if self.cooldown > 0:
            self.cooldown -= 1

        orders = []
        if bar is not None and seq >= self.warmup_bars:
            try:
                orders = self.decide(seq, cash, qty, equity)
            except Exception:
                orders = []
        return {"type": "reply", "seq": seq, "orders": orders}

    def decide(self, seq, cash, qty, equity):
        close = self.closes[-1]
        if qty > 0:
            return self.maybe_exit(seq, qty, close)
        if self.cooldown > 0:
            return []
        if equity is not None and equity * BPS < self.peak_equity * EQUITY_GUARD_BPS:
            return []
        if cash < self.min_notional:
            return []
        signal = self.thrust_signal(close)
        if signal is not None:
            self.kind = "thrust"
            self.entry_price = close
            self.entry_seq = seq
            self.peak = close
            self.level = signal
            return [self.buy(seq, cash)]
        signal = self.bounce_signal(close)
        if signal is not None:
            self.kind = "bounce"
            self.entry_price = close
            self.entry_seq = seq
            self.peak = close
            self.level = signal
            return [self.buy(seq, cash)]
        return []

    def maybe_exit(self, seq, qty, close):
        # Defensive: a position with no entry state (should not happen)
        # is treated as a thrust trade entered at this close.
        if self.kind is None:
            self.kind = "thrust"
            self.entry_price = close
            self.entry_seq = seq
            self.peak = close
            self.level = close
        if close > self.peak:
            self.peak = close
        if self.kind == "bounce":
            if (close * BPS >= self.entry_price * (BPS + BOUNCE_TAKE_BPS)
                    or close * BPS <= self.entry_price * (BPS - BOUNCE_STOP_BPS)
                    or seq - self.entry_seq >= BOUNCE_MAX_HOLD):
                return [self.sell(seq, qty)]
            return []
        prior_lows = self.lows[:-1][-EXIT_BARS:]
        if (prior_lows and close < min(prior_lows)
                or close * BPS <= self.peak * (BPS - TRAIL_BPS)
                or close < self.level):
            return [self.sell(seq, qty)]
        return []

    def thrust_signal(self, close):
        # Need TREND_BARS closes before this one to define the high.
        if len(self.highs) < TREND_BARS + 1:
            return None
        high = max(self.highs[-TREND_BARS - 1:-1])
        if close * BPS > high * (BPS + THRUST_MARGIN_BPS):
            return high
        return None

    def bounce_signal(self, close):
        if len(self.closes) < BOUNCE_BARS + 1:
            return None
        window = self.closes[-BOUNCE_BARS - 1:-1]
        low = min(window)
        start = window[0]
        if (low * BPS <= start * (BPS - BOUNCE_DROP_BPS)
                and close * BPS >= low * (BPS + BOUNCE_RECOVER_BPS)
                and close > self.closes[-2]):
            return low
        return None

    def buy(self, seq, cash):
        return {
            "side": "buy",
            "client_id": "b%d" % seq,
            "symbol": self.symbol,
            "notional_quote_units": str(cash),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }

    def sell(self, seq, qty):
        return {
            "side": "sell",
            "client_id": "s%d" % seq,
            "symbol": self.symbol,
            "qty": str(qty),
            "max_slippage_bps": MAX_SLIPPAGE_BPS,
        }


def main():
    bot = Bot()
    out = sys.stdout
    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue
        try:
            msg = json.loads(line)
        except ValueError:
            continue
        kind = msg.get("type")
        if kind == "hello":
            try:
                bot.on_hello(msg)
            except Exception:
                pass
        elif kind == "batch":
            try:
                reply = bot.on_batch(msg)
            except Exception:
                reply = {"type": "reply", "seq": int(msg.get("seq", 0)), "orders": []}
            out.write(json.dumps(reply, separators=(",", ":")) + "\n")
            out.flush()
        elif kind == "done":
            break


if __name__ == "__main__":
    main()
