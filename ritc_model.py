
"""RITC 2026 research model. Standard library only; no live order submission.

Run: python ritc_model.py --self-test
     python ritc_model.py --demo

All defaults are illustrative, NOT calibrated. Time is elapsed real seconds.
Call Model.observe once per snapshot, then Model.quotes with reconciled positions.
Returned orders form a REPLACEMENT portfolio: cancel/confirm old orders first.
Do not add these orders to existing resting orders. No API adapter is included.
"""
from dataclasses import dataclass
import argparse
import math
import random
import unittest

TICKERS = ('SPNG', 'SMMR', 'ATMN', 'WNTR')
REBATES = dict(zip(TICKERS, (.01, .02, .015, .025)))

@dataclass(frozen=True)
class Book:
    bid: float
    ask: float
    bid_size: int
    ask_size: int

    def __post_init__(self):
        if not (0 < self.bid < self.ask and self.bid_size > 0 and self.ask_size > 0):
            raise ValueError('Require an uncrossed book with positive sizes')
        if not all(math.isfinite(x) for x in (self.bid, self.ask)):
            raise ValueError('Nonfinite price')

    @property
    def mid(self):
        return (self.bid + self.ask) / 2

    @property
    def imbalance(self):
        return (self.bid_size - self.ask_size) / (self.bid_size + self.ask_size)

@dataclass(frozen=True)
class Config:
    # These are research starting values, not estimates of RIT behaviour.
    tick: float = .01
    fast_tau: float = 1.
    slow_tau: float = 8.
    variance_tau: float = 5.
    momentum_weight: float = .25
    reversal_weight: float = .10
    imbalance_weight: float = .01  # CAD/share for unit imbalance
    alpha_cap: float = .04
    inventory_skew: float = .05  # CAD/share at full per-stock capacity
    min_half_width: float = .01
    volatility_width: float = .5  # sqrt(seconds), multiplying CAD/sqrt(second)
    quote_size: int = 500
    per_stock_cap: int = 4000
    close_buffer: int = 1000
    close_window: float = 8.
    shock_threshold: float = .10  # CAD midpoint change between snapshots
    shock_cooldown: float = 2.
    mode: str = 'book'  # baseline, momentum, reversal, book, combined

@dataclass
class State:
    t: float
    mid: float
    fast: float
    slow: float
    variance: float = 0.
    shock_until: float = -1.

@dataclass(frozen=True)
class Order:
    ticker: str
    side: str
    price: float
    quantity: int

class Model:
    def __init__(self, config=Config()):
        self.c = config
        if config.mode not in ('baseline', 'momentum', 'reversal', 'book', 'combined'):
            raise ValueError('Unknown mode')
        self.state = {}

    def observe(self, ticker, t, book):
        if ticker not in TICKERS or not 0 <= t <= 300:
            raise ValueError('Invalid ticker or time')
        s = self.state.get(ticker)
        if s is None:
            self.state[ticker] = State(t, book.mid, book.mid, book.mid)
            return
        dt = t - s.t
        if dt <= 0:
            raise ValueError('Snapshots must have strictly increasing timestamps per stock')
        change = book.mid - s.mid

