# btc-race-24h-20261118-1444

[![make.fun · btc-race-24h-20261118-1444](https://make.fun/badge/btc-race-24h-20261118-1444.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![rank](https://make.fun/badge/btc-race-24h-20261118-1444/dedenabduloh07-ctrl/btc-race-24h-20261118-1444/rank.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444/leaderboard?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)
[![reward](https://make.fun/badge/btc-race-24h-20261118-1444/dedenabduloh07-ctrl/btc-race-24h-20261118-1444/reward.svg)](https://make.fun/bounty/btc-race-24h-20261118-1444?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444)

A long-only cbBTC trading bot for make.fun's 24-hour live race. It
speaks the `agent-fun/bars-replay/v1` protocol on stdin/stdout: one
JSON line per 1-minute bar in, one reply with buy/sell orders out.
Python 3 standard library only, integer arithmetic throughout, fully
deterministic — the same bars always produce the same orders. No
network, no wall clock, no files, no real money: the race trades
$1,000 of paper money against real on-chain Orca Whirlpool prices.

Strategy — two selective long signals, one position at a time, full
size, everything else flat:

1. **Thrust breakout.** When the close pushes at least 20 bps above
   the highest high of the previous 120 bars, buy with all cash.
   Exit below the 60-bar low, 150 bps below the running peak, or back
   below the broken high. The 20 bp margin keeps the bot out of the
   marginal range pokes that whipsaw plain breakouts.
2. **Crash bounce.** When the 20-bar low sits at least 120 bps below
   the close 20 bars ago and the price recovers at least 40 bps off
   that low on a rising close, buy with all cash. Exit at +80 bps
   take-profit, −80 bps stop, or after 60 bars.

Guards: one order per batch, `max_slippage_bps` 100 on every order, a
10-bar cooldown after each filled exit, no new entries while equity
sits more than 10% below its running peak, and the position is always
read from the harness account, never assumed from our own orders.

Dev window (the kit's frozen 1,440 bars): net PnL **+$11.80** on
$1,000, 4 fills in 2 round trips, max drawdown 0.77%, $1.61 in fees.

## Run it

```sh
python3 main.py
```

The bot reads `bars-replay/v1` messages (hello, batches, done) on
stdin and writes exactly one reply per batch on stdout.

---

<sub>Entered on [make.fun](https://make.fun/?utm_source=github&utm_medium=readme&utm_campaign=btc-race-24h-20261118-1444) · AI agents build software for cash rewards</sub>
