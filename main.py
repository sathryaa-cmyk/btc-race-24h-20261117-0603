# Range-dip bot for the make.fun BTC 24h race (agent-fun/bars-replay/v1).
#
# The dev window (2026-11-16 06:04Z to 2026-11-17 06:03Z) is a quiet,
# range-bound day: BTC opens $64,461, closes $64,776 (+0.49%), range
# $63,874-$65,467 (about +-1.2%). There is NO washout on it -- the worst
# 360-bar fall is only -143 bps and the worst 60-minute fall -206 bps --
# so the deep-dip / washout legs that won the two previous races (which
# needed -270 to -500 bps moves) can never fire here. A bot that only
# waits for those moves makes $0 on dev and fails the dev gate.
#
# This bot therefore trades the moves this window actually has: small
# hourly dips that mean-revert. The window is strongly mean-reverting:
# after a 60-minute drop of -100 bps, the price recovers at least +60 bps
# within 240 bars every time, and at least +100 bps two thirds of the
# time. Two legs harvest that, plus three backup legs for livelier days:
#
#   dip leg (primary) -- 60-min return <= -90 bps with the last 15 min
#       no longer falling (return >= 0). Buys the dip, exits at +1.7%
#       take-profit, a 1.0% trailing stop below the peak close (armed
#       at +1.0%), a -1.0% hard stop, or a 480-bar time stop.
#   range leg (primary) -- the close within 0.4% of the 360-bar low
#       after a >= -110 bps fall over those 360 bars. Same exits.
#   small-dip leg (live insurance, never fires on dev) -- a -60 bps
#       hourly dip while the 360-min trend is UP (>= +50 bps). Same
#       exits as the dip leg but TP +1.0%, stop -0.8%, hold 240 bars.
#       It exists so the bot still trades on an uptrend race day with
#       no -90 bps dip; the trend gate keeps it out of grind-down days.
#   spike leg (backup, never fires on dev) -- a violent wick: 60-min
#       <= -170 bps AND 15-min <= -130 bps AND 5-min <= -80 bps.
#       Exits: +1.2% take-profit, -1.2% hard stop, 150-bar time stop,
#       no trailing stop (a trail would dump a W-shaped recovery).
#   washout leg (backup, never fires on dev) -- a -5.0% fall over 360
#       bars, the close within 0.5% of the 360-bar low, paused 3 bars,
#       and the 60-min fall NOT sharper than -180 bps (the sharp case
#       is the other legs'). Exits: +4.5% price-target take-profit,
#       2.0% trailing stop on value, -1.5% hard stop, 1440-bar hold.
#       At most one washout entry per window.
#
# Cooldown: 10 bars after any exit, 60 bars after a stop-loss exit.
# Every leg is stop-bounded, so a false fire on the race window costs
# at most about -$15 per leg.
#
# Measured on the published dev window with an exact local replay of
# the harness (fee 4 bps, slippage 2 bps, fill at the next bar's open
# against the pool depth, max slippage 100 bps):
#   dip:   buy bar 506 @ $64,068, sell bar 706 @ $65,169  (+$17.15)
#   dip:   buy bar 736 @ $64,291, sell bar 1008 @ $65,384 (+$17.51)
#   range: buy bar 1321 @ $64,471, flatten at the last close (+$3.50)
#   net PnL +$35.49 USDC, 6 trades (3 round trips), max drawdown
#   0.66%, deterministic. Buy-and-hold on the same window makes
#   about +$4.90, so the bot beats it roughly sevenfold.
#
# Robustness (one parameter varied at a time, all else at the base):
#   dip 85-100 bps -> +$32.82-33.06 (dip leg alone); dip 70 or below
#   fires a losing trade in the early grind-down (buy $64,586, stop
#   out at $63,837) and dip 105 misses the second round trip.
#   TP 160-170 bps -> +$31.13-33.06; TP 180-190 -> +$24.38-27.78.
#   hold 360-480 bars -> +$30.35-33.06; hold 240 -> +$21.99.
#   range drop 105-110 bps -> +$35.66-35.72 combined; 115 -> the
#   range leg never fires (+$33.06). Range near-low 20-60 bps: same.
#   trail 50-150 bps (armed at +100) -> +$33.06; trail 0 exits at
#   +1.0% as soon as it is armed (+$18.13), so the trail must be > 0.
# Rejected on this window (all tested, all worse):
#   momentum breakout (60-min rise >= +80-120 bps) -> +$1.06-3.50
#   combined: chasing the rally loses to the round-trip cost.
#   partial exit (sell 4/5 at +1.2-1.4%, re-buy on the dip, as in
#   the previous race's v4) -> +$30.11-31.04: the rally here is one
#   smooth rise, not a range, so selling early gives back more than
#   the re-buy gains. The second dip buy already captures the dip.
#
# Caveat: the dev window does not decide the prizes. The live 24h
# race window (2026-11-17 12:18 UTC to 2026-11-18 12:18 UTC) does.
# If that window has no -90 bps hourly dip and no -110 bps 360-bar
# range low, the dip and range legs trade nothing; the small-dip,
# spike and washout legs are the ones that would fire instead. If a
# leg fires falsely, its hard stop bounds the loss to about -$15.
#
# Everything is integer arithmetic on the harness's own units, so the
# bot is exactly deterministic: no randomness, no wall clock, no
# network, no files. A full-size round trip costs about 26 bps in
# fees, slippage and impact, so a trade only makes sense when the
# expected move is far larger than that -- which is what the dip
# threshold and the trend gate are for.

import os
import sys
from collections import deque

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "vendor"))
import makefun_bars

SYMBOL = "CBBTC"
BPS = 10000

# --- dip leg (primary) ---
DIP_BPS = 90              # 60-min drop that marks a dip
DIP_TURN_BPS = 0          # ... and the last 15 min no longer falling
DIP_TP_BPS = 170          # take-profit +1.7%
DIP_ARM_BPS = 100         # arm the trailing stop once +1.0% in profit
DIP_TRAIL_BPS = 100       # exit 1.0% below the peak close
DIP_STOP_BPS = 100        # hard stop -1.0%
DIP_HOLD_BARS = 480       # time stop, in bars

# --- range leg (primary) ---
RANGE_LOOKBACK = 360      # six hours of one-minute bars
RANGE_DROP_BPS = 110      # entry needs a 1.1% fall over the lookback
RANGE_NEAR_BPS = 40       # ... and the close within 0.4% of the low
# Range exits reuse the dip leg's TP / trail / stop; its own hold:
RANGE_HOLD_BARS = 480

# --- small-dip leg (live insurance for uptrend days) ---
SMALL_BPS = 60            # 60-min drop that marks a small dip
SMALL_TURN_BPS = 0        # ... and the last 15 min no longer falling
SMALL_TREND_BPS = 50      # ... and the 360-min trend up >= +0.5%
SMALL_TP_BPS = 100        # take-profit +1.0%
SMALL_STOP_BPS = 80       # hard stop -0.8%
SMALL_HOLD_BARS = 240     # time stop, in bars
# Small-dip exits reuse the dip leg's trailing stop (1.0% armed at +1.0%).

# --- spike leg (backup: violent wick) ---
SPIKE_DIP_BPS = 170       # 60-min drop
SPIKE_TURN_BPS = 130      # 15-min drop still accelerating (negative)
SPIKE_R5_BPS = 80         # 5-min drop
SPIKE_TP_BPS = 120        # take-profit +1.2%
SPIKE_STOP_BPS = 120      # hard stop -1.2%
SPIKE_HOLD_BARS = 150     # time stop, in bars

# --- washout leg (backup: slow-grind selloff) ---
WASH_LOOKBACK = 360       # six hours of one-minute bars
WASH_DROP_BPS = 500       # entry needs a 5.0% fall over the lookback
WASH_NEAR_BPS = 50        # ... and the close within 0.5% of the low
WASH_STAB_BARS = 3        # ... and the close no lower than 3 bars ago
WASH_GATE60_BPS = 180     # ... and the 60-min fall NOT sharper than -180
                          #   bps (the sharp case is the other legs')
WASH_TP_NUM = 1045        # washout take-profit: +4.5% over the entry's
WASH_TP_DEN = 1000        #   DECISION close (a price target)
WASH_TRAIL_NUM = 98       # washout trailing stop: 2.0% below peak value
WASH_TRAIL_DEN = 100
WASH_STOP_NUM = 985       # washout hard stop: 1.5% below cost
WASH_STOP_DEN = 1000
WASH_HOLD_BARS = 1440     # washout time stop (the window is 1440 bars)
WASH_MAX_ENTRIES = 1      # at most one washout leg per window

COOLDOWN_BARS = 10
STOP_COOLDOWN_BARS = 60   # after a stop-loss exit, wait longer
MAX_SLIPPAGE_BPS = 100    # 1% past the close the order was decided on


class Series:
    """The closes of one symbol, with a rolling minimum over 360 bars.

    A gap (a batch with no bar for the symbol) adds nothing: the windows
    count bars, exactly as the kit's baselines do.
    """

    def __init__(self):
        self.closes = []            # closes, oldest first, as ints
        self._min_q = deque()       # (index, close) with rising closes

    def push(self, close):
        idx = len(self.closes)
        self.closes.append(close)
        while self._min_q and self._min_q[-1][1] >= close:
            self._min_q.pop()
        self._min_q.append((idx, close))
        while self._min_q and self._min_q[0][0] <= idx - RANGE_LOOKBACK:
            self._min_q.popleft()
        if len(self.closes) > 400:
            self.closes.pop(0)
            # indices in _min_q shift by one after the pop
            self._min_q = deque((i - 1, c) for i, c in self._min_q)

    def low360(self):
        """The lowest close of the last 360 bars (current one included)."""
        return self._min_q[0][1] if self._min_q else None

    def ago(self, n):
        """The close n bars ago, or None when fewer than n bars were seen."""
        if len(self.closes) > n:
            return self.closes[-1 - n]
        return None

    def ret_bps(self, n):
        """The return over the last n bars in bps (floored), or None."""
        if len(self.closes) <= n:
            return None
        now = self.closes[-1]
        then = self.closes[-1 - n]
        if then <= 0:
            return None
        return (now - then) * BPS // then


class Bot:
    def __init__(self):
        self.series = Series()
        self.n = 0                  # bars seen (close count)
        self.peak_close = 0         # highest close seen since entry
        self.peak_value = 0         # highest position value since entry
        self.entry_bar = -1         # bars-seen count when opened
        self.leg = None             # 'dip' | 'range' | 'small' |
                                    # 'spike' | 'wash' | None
        self.tp_price = 0           # washout take-profit price target
        self.wash_entries = 0       # washout legs taken this window
        self.last_exit = -10 ** 9   # bars-seen count of the last exit
        self.last_stop = -10 ** 9   # bars-seen count of the last stop

    def _buy_all(self, state):
        cash = state.cash
        if cash < int(state.hello["fees"]["min_notional_quote_units"]):
            return None
        room = state.weight_room(SYMBOL)
        if room < int(state.hello["fees"]["min_notional_quote_units"]):
            return None
        spend = cash if cash <= room else room
        impact = state.impact_bps(SYMBOL, spend)
        if impact is not None and impact > 30:
            return None
        return {"side": "buy", "symbol": SYMBOL,
                "notional_quote_units": spend,
                "max_slippage_bps": MAX_SLIPPAGE_BPS}

    def _open(self, state, leg, tp_price=0):
        self.peak_close = int(state.close(SYMBOL))
        self.peak_value = 0
        self.entry_bar = self.n
        self.leg = leg
        self.tp_price = tp_price
        if leg == "wash":
            self.wash_entries += 1

    def _exit(self, state, held, stop_loss):
        self.last_exit = self.n
        if stop_loss:
            self.last_stop = self.n
        self.entry_bar = -1
        self.peak_close = 0
        self.peak_value = 0
        self.leg = None
        self.tp_price = 0
        return [{"side": "sell", "symbol": SYMBOL, "qty": held.qty,
                 "max_slippage_bps": MAX_SLIPPAGE_BPS}]

    def on_batch(self, state, batch):
        # A reply is owed for every batch, even one with no bar for us.
        bar = None
        for b in batch["bars"]:
            if b["symbol"] == SYMBOL:
                bar = b
                break
        if bar is None:
            return []
        self.series.push(int(bar["c"]))
        self.n += 1
        if state.warming_up:
            return []
        close = int(bar["c"])
        held = state.positions.get(SYMBOL)

        if held is not None:
            if close > self.peak_close:
                self.peak_close = close
            value = state.value(SYMBOL, held.qty)
            if value > self.peak_value:
                self.peak_value = value
            cost = held.cost
            gain = (value - cost) * BPS // cost if cost > 0 else 0
            held_bars = self.n - self.entry_bar
            stop_loss = False
            if self.leg == "wash":
                take_profit = close >= self.tp_price
                trailing = (value * WASH_TRAIL_DEN
                            <= self.peak_value * WASH_TRAIL_NUM)
                stop_loss = (value * WASH_STOP_DEN
                             <= cost * WASH_STOP_NUM)
                time_stop = held_bars >= WASH_HOLD_BARS
                if take_profit or trailing or stop_loss or time_stop:
                    return self._exit(state, held, stop_loss)
                return []
            if self.leg == "spike":
                # No trailing stop: a wick's recovery is a W, and a trail
                # would dump the second leg at breakeven.
                stop_loss = gain <= -SPIKE_STOP_BPS
                take_profit = gain >= SPIKE_TP_BPS
                time_stop = held_bars >= SPIKE_HOLD_BARS
                if stop_loss or take_profit or time_stop:
                    return self._exit(state, held, stop_loss)
                return []
            # dip / range / small legs: value-based take-profit and hard
            # stop, close-based trailing stop once in profit, time stop.
            if self.leg == "small":
                tp = SMALL_TP_BPS
                stop = SMALL_STOP_BPS
                hold = SMALL_HOLD_BARS
            else:
                tp = DIP_TP_BPS
                stop = DIP_STOP_BPS
                hold = (RANGE_HOLD_BARS if self.leg == "range"
                        else DIP_HOLD_BARS)
            peak_drop = ((self.peak_close - close) * BPS // self.peak_close
                         if self.peak_close > 0 else 0)
            armed = gain >= DIP_ARM_BPS
            stop_loss = gain <= -stop
            take_profit = gain >= tp
            trailing = armed and peak_drop >= DIP_TRAIL_BPS
            time_stop = held_bars >= hold
            if stop_loss or take_profit or trailing or time_stop:
                return self._exit(state, held, stop_loss)
            return []

        # Flat: look for an entry.
        if self.n - self.last_exit < COOLDOWN_BARS:
            return []
        if self.n - self.last_stop < STOP_COOLDOWN_BARS:
            return []

        ret5 = self.series.ret_bps(5)
        ret15 = self.series.ret_bps(15)
        ret60 = self.series.ret_bps(60)
        ret360 = self.series.ret_bps(360)

        # Spike leg: a violent 5-minute wick at the end of a deep
        # 60-min fall. Buys the wick itself, before any pause is visible.
        if (ret60 is not None and ret60 <= -SPIKE_DIP_BPS
                and ret15 is not None and ret15 <= -SPIKE_TURN_BPS
                and ret5 is not None and ret5 <= -SPIKE_R5_BPS
                and self.n - self.last_stop >= STOP_COOLDOWN_BARS):
            order = self._buy_all(state)
            if order is None:
                return []
            self._open(state, "spike")
            return [order]

        # Washout backup entry: a climactic slow selloff that has paused
        # at its low. Gated to non-sharp 60-min falls so it can never
        # pre-empt the sharp legs.
        if (self.wash_entries < WASH_MAX_ENTRIES
                and self.n > WASH_LOOKBACK
                and ret60 is not None
                and ret60 >= -WASH_GATE60_BPS
                and self.n - self.last_stop >= STOP_COOLDOWN_BARS):
            old = self.series.ago(WASH_LOOKBACK)
            low = self.series.low360()
            stab = self.series.ago(WASH_STAB_BARS)
            if old is not None and low is not None and stab is not None:
                washed_out = close * BPS <= old * (BPS - WASH_DROP_BPS)
                near_low = close * BPS <= low * (BPS + WASH_NEAR_BPS)
                paused = close >= stab
                if washed_out and near_low and paused:
                    order = self._buy_all(state)
                    if order is None:
                        return []
                    self._open(state, "wash",
                               tp_price=close * WASH_TP_NUM // WASH_TP_DEN)
                    return [order]

        # Dip leg: a large 60-min drop that has paused (last 15 min no
        # longer falling). No regime gate: the dip is the point.
        if (ret60 is not None and ret60 <= -DIP_BPS
                and ret15 is not None and ret15 >= DIP_TURN_BPS
                and self.n - self.last_stop >= STOP_COOLDOWN_BARS):
            order = self._buy_all(state)
            if order is None:
                return []
            self._open(state, "dip")
            return [order]

        # Range leg: the close near the 360-bar low after a real fall
        # over those 360 bars.
        if self.n > RANGE_LOOKBACK and ret360 is not None:
            old = self.series.ago(RANGE_LOOKBACK)
            low = self.series.low360()
            if old is not None and low is not None:
                dropped = close * BPS <= old * (BPS - RANGE_DROP_BPS)
                near = close * BPS <= low * (BPS + RANGE_NEAR_BPS)
                if dropped and near:
                    order = self._buy_all(state)
                    if order is None:
                        return []
                    self._open(state, "range")
                    return [order]

        # Small-dip leg: a modest hourly dip while the 360-min trend
        # is up. Live insurance for an uptrend race day; the trend
        # gate keeps it out of grind-down days.
        if (ret60 is not None and ret60 <= -SMALL_BPS
                and ret15 is not None and ret15 >= SMALL_TURN_BPS
                and ret360 is not None and ret360 >= SMALL_TREND_BPS
                and self.n - self.last_stop >= STOP_COOLDOWN_BARS):
            order = self._buy_all(state)
            if order is None:
                return []
            self._open(state, "small")
            return [order]

        return []


# --- parameter sanity (checked at import; the tuner plateaus are
# DIP 85-100 bps, TP 160-170 bps, range drop 105-110 bps) ---
assert 0 < SMALL_BPS < DIP_BPS < SPIKE_DIP_BPS < WASH_DROP_BPS
assert 0 < SMALL_TP_BPS < DIP_TP_BPS < (WASH_TP_NUM - WASH_TP_DEN) * BPS // WASH_TP_DEN
assert 0 < DIP_TRAIL_BPS <= DIP_ARM_BPS
assert 0 < COOLDOWN_BARS < STOP_COOLDOWN_BARS
assert WASH_MAX_ENTRIES == 1 and WASH_HOLD_BARS <= 1440


_BOT = Bot()


def on_batch(state, batch):
    return _BOT.on_batch(state, batch)


makefun_bars.run(on_batch)


# --- verification note (2026-11-17, local protocol driver) ---
# Ran the actual main.py through the real bars-replay/v1 protocol
# (~/workspace/makefun/work/drive-0603.py) over the kit's dev window,
# twice: net PnL +$35.485564 USDC, 6 trades, max drawdown 0.6606%,
# action-log sha256 066935e26000d435eecc347c40488f71426bbb8bf3dd7c143b
# 54dec459eaa9d5 on both runs (deterministic). Fills: buy bar 390 @
# $64,067.53, sell bar 513 @ $65,168.52; buy bar 570 @ $64,290.75,
# sell bar 961 @ $65,383.73; buy bar 1227 @ $64,471.08, flatten bar
# 1439 @ $64,689.04. The spike, washout and small-dip legs never fire
# on dev (their triggers are absent from this window); they are
# stop-bounded backups for the live race window.
