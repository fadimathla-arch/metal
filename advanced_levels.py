# advanced_levels.py
"""Utilities to detect major/recent highs/lows and simple order blocks from OHLC candle data.

Expected input: list of candles where each candle is a dict with keys:
 'time','open','high','low','close','volume'.
This is intentionally lightweight and deterministic so it integrates safely with the existing repo.
"""
from typing import List, Dict, Any


def _rolling_extrema(candles: List[Dict[str, Any]], lookback: int = 20):
    highs = [c['high'] for c in candles]
    lows = [c['low'] for c in candles]
    recent_high = max(highs[-lookback:]) if len(highs) >= lookback else max(highs)
    recent_low = min(lows[-lookback:]) if len(lows) >= lookback else min(lows)
    major_high = max(highs)
    major_low = min(lows)
    return {
        'recent_high': recent_high,
        'recent_low': recent_low,
        'major_high': major_high,
        'major_low': major_low,
    }


def detect_levels(candles: List[Dict[str, Any]], lookback: int = 20) -> Dict[str, float]:
    """Return a dictionary of level names to price levels.
    Keys: major_low, recent_low, major_high, recent_high, support, resistance, order_block_buy, order_block_sell
    """
    if not candles:
        raise ValueError("candles must be a non-empty list")

    extrema = _rolling_extrema(candles, lookback=lookback)

    # Basic interpretation: support ~ recent_low rounded to 2 decimals, resistance ~ recent_high
    recent_low = extrema['recent_low']
    recent_high = extrema['recent_high']
    major_low = extrema['major_low']
    major_high = extrema['major_high']

    # Order blocks: small zones around recent extremes (15% of range to major extreme)
    # Guard against zero range
    range_high = max(major_high - recent_high, 1e-8)
    range_low = max(recent_low - major_low, 1e-8)

    ob_buy = round(recent_low - range_low * 0.15, 2)    # slightly below recent low
    ob_sell = round(recent_high + range_high * 0.15, 2) # slightly above recent high

    levels = {
        'major_low': round(major_low, 2),
        'recent_low': round(recent_low, 2),
        'major_high': round(major_high, 2),
        'recent_high': round(recent_high, 2),
        'support': round(recent_low, 2),
        'resistance': round(recent_high, 2),
        'order_block_buy': ob_buy,
        'order_block_sell': ob_sell,
    }
    return levels
