# tests/test_advanced_levels.py
"""
Unit test for advanced_levels.detect_levels
Run with: pytest tests/test_advanced_levels.py
"""
from advanced_levels import detect_levels


def make_sample_candles():
    # create 30 sample candles with varying highs/lows
    candles = []
    base = 4000.0
    for i in range(30):
        # create a gentle up then down pattern
        if i < 15:
            high = base + i * 2 + 5
            low = base + i * 2 - 5
            close = base + i * 2 + 1
            open_ = base + i * 2 - 1
        else:
            j = i - 15
            high = base + (15 - j) * 2 + 10
            low = base + (15 - j) * 2 - 10
            close = base + (15 - j) * 2 - 0.5
            open_ = base + (15 - j) * 2 + 0.5

        candles.append({
            "time": i,
            "open": float(open_),
            "high": float(high),
            "low": float(low),
            "close": float(close),
            "volume": 100 + i,
        })
    return candles


def test_detect_levels_basic_keys():
    candles = make_sample_candles()
    levels = detect_levels(candles, lookback=20)

    # basic assertions
    assert isinstance(levels, dict)
    for k in [
        'major_low','recent_low','major_high','recent_high',
        'support','resistance','order_block_buy','order_block_sell'
    ]:
        assert k in levels
        assert isinstance(levels[k], float) or isinstance(levels[k], int)

    # sanity checks
    assert levels['major_low'] <= levels['major_high']
    assert levels['recent_low'] <= levels['recent_high']
