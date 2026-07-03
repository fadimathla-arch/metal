"""
advanced_levels.py — كشف متقدم للمستويات والمناطق الحرجة
═══════════════════════════════════════════════════════════
- كشف Multiple Support/Resistance (أساسي، قريب، تاريخي)
- Order Blocks المتعددة مع Strength
- Divergence المتقدمة (Price + MACD + RSI)
- Volume Profiling للضغط البيعي/الشرائي
- Confluence Zones الحرجة
"""

import pandas as pd
import numpy as np
from logger import log


def detect_multiple_levels(
    rates,
    lookback: int = 100,
    swing_window: int = 5,
) -> dict:
    """
    كشف متقدم للمستويات المتعددة:
    - Major Support/Resistance (أقوى)
    - Recent Support/Resistance (حديث)
    - Historical Levels (قديم لكن مهم)
    """
    if rates is None or len(rates) < swing_window * 2 + 1:
        return {
            "major_support": 0,
            "major_resistance": 0,
            "recent_support": 0,
            "recent_resistance": 0,
            "historical_support": [],
            "historical_resistance": [],
            "critical_zones": [],
        }

    df = pd.DataFrame(rates).tail(lookback).reset_index(drop=True)
    n = len(df)

    swing_highs = []
    swing_lows = []

    # ── كشف Swing Points ──────────────────────
    for i in range(swing_window, n - swing_window):
        window_h = df["high"].iloc[i - swing_window : i + swing_window + 1]
        window_l = df["low"].iloc[i - swing_window : i + swing_window + 1]

        if float(df["high"].iloc[i]) == float(window_h.max()):
            swing_highs.append(
                {
                    "price": round(float(df["high"].iloc[i]), 3),
                    "index": i,
                    "strength": 1,  # عدد اللمسات (يُحدّث لاحقاً)
                }
            )
        if float(df["low"].iloc[i]) == float(window_l.min()):
            swing_lows.append(
                {
                    "price": round(float(df["low"].iloc[i]), 3),
                    "index": i,
                    "strength": 1,
                }
            )

    # ── تصنيف المستويات حسب الحداثة والقوة ────
    if not swing_highs or not swing_lows:
        return {
            "major_support": 0,
            "major_resistance": 0,
            "recent_support": 0,
            "recent_resistance": 0,
            "critical_zones": [],
        }

    # Resistance
    resistance_sorted = sorted(swing_highs, key=lambda x: x["index"], reverse=True)
    major_resistance = (
        resistance_sorted[0]["price"] if resistance_sorted else 0
    )
    recent_resistance = (
        resistance_sorted[1]["price"] if len(resistance_sorted) > 1 else major_resistance
    )
    historical_resistances = [
        r["price"] for r in resistance_sorted[2:4]
    ]  # آخر قديمة

    # Support
    support_sorted = sorted(swing_lows, key=lambda x: x["index"], reverse=True)
    major_support = support_sorted[0]["price"] if support_sorted else 0
    recent_support = (
        support_sorted[1]["price"] if len(support_sorted) > 1 else major_support
    )
    historical_supports = [s["price"] for s in support_sorted[2:4]]

    # ── حساب Strength (عدد اللمسات) ────────────
    current_price = float(df["close"].iloc[-1])
    atr = _calculate_atr(df)
    touch_tolerance = atr * 0.5

    for r in swing_highs:
        touches = sum(
            1
            for close in df["close"]
            if abs(close - r["price"]) <= touch_tolerance
        )
        r["strength"] = touches

    for s in swing_lows:
        touches = sum(
            1
            for close in df["close"]
            if abs(close - s["price"]) <= touch_tolerance
        )
        s["strength"] = touches

    # ── تحديد Confluence Zones ────────────────
    critical_zones = _find_confluence_zones(
        df,
        major_support,
        major_resistance,
        recent_support,
        recent_resistance,
    )

    return {
        "major_support": major_support,
        "major_resistance": major_resistance,
        "recent_support": recent_support,
        "recent_resistance": recent_resistance,
        "historical_support": historical_supports,
        "historical_resistance": historical_resistances,
        "support_strength": max([s["strength"] for s in support_sorted]) if support_sorted else 1,
        "resistance_strength": max([r["strength"] for r in resistance_sorted]) if resistance_sorted else 1,
        "critical_zones": critical_zones,
        "current_price": current_price,
        "distance_to_support": round(current_price - major_support, 3),
        "distance_to_resistance": round(major_resistance - current_price, 3),
    }


def _find_confluence_zones(df, major_sup, major_res, recent_sup, recent_res, tolerance_pct=0.15):
    """
    تحديد مناطق التقاء (Confluence Zones) حيث
    يلتقي Support مع Resistance القريب
    """
    zones = []
    atr = _calculate_atr(df)
    tolerance = atr * tolerance_pct

    # منطقة حول Major Support
    if major_sup > 0:
        zones.append(
            {
                "type": "SUPPORT_CRITICAL",
                "price": major_sup,
                "range": (major_sup - tolerance, major_sup + tolerance),
                "strength": "STRONG",
                "label": f"دعم أساسي: {major_sup:.2f}",
            }
        )

    # منطقة حول Major Resistance
    if major_res > 0:
        zones.append(
            {
                "type": "RESISTANCE_CRITICAL",
                "price": major_res,
                "range": (major_res - tolerance, major_res + tolerance),
                "strength": "STRONG",
                "label": f"مقاومة أساسية: {major_res:.2f}",
            }
        )

    # منطقة حول Recent Support
    if recent_sup > 0 and abs(recent_sup - major_sup) > tolerance:
        zones.append(
            {
                "type": "SUPPORT_RECENT",
                "price": recent_sup,
                "range": (recent_sup - tolerance, recent_sup + tolerance),
                "strength": "MEDIUM",
                "label": f"دعم قريب: {recent_sup:.2f}",
            }
        )

    # منطقة حول Recent Resistance
    if recent_res > 0 and abs(recent_res - major_res) > tolerance:
        zones.append(
            {
                "type": "RESISTANCE_RECENT",
                "price": recent_res,
                "range": (recent_res - tolerance, recent_res + tolerance),
                "strength": "MEDIUM",
                "label": f"مقاومة قريبة: {recent_res:.2f}",
            }
        )

    return zones


def detect_advanced_divergence(rates) -> dict:
    """
    كشف Divergence متقدمة:
    - Price Divergence (سعر)
    - MACD Divergence
    - RSI Divergence
    - Volume Divergence
    """
    if rates is None or len(rates) < 30:
        return {
            "price_bullish": False,
            "price_bearish": False,
            "macd_bullish": False,
            "macd_bearish": False,
            "rsi_bullish": False,
            "rsi_bearish": False,
            "volume_confirmation": "neutral",
            "overall_signal": "NEUTRAL",
        }

    df = pd.DataFrame(rates).tail(30).reset_index(drop=True)

    # ── Price Divergence ──────────────────────
    lows = df["low"].values
    highs = df["high"].values
    closes = df["close"].values

    # آخر قاعين
    recent_lows = [(i, lows[i]) for i in range(len(lows) - 1, max(0, len(lows) - 10), -1)]
    recent_lows.sort(key=lambda x: x[1])
    
    price_bullish_div = False
    if len(recent_lows) >= 2:
        # قاع أدنى لكن قد يكون هناك ارتداد
        if recent_lows[0][0] > recent_lows[1][0]:  # آخر واحد أقدم؟
            price_bullish_div = recent_lows[0][1] < recent_lows[1][1]

    # ── MACD Divergence ───────────────────────
    c = pd.Series(closes)
    ema12 = c.ewm(span=12, adjust=False).mean()
    ema26 = c.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal_line = macd.ewm(span=9, adjust=False).mean()

    macd_bullish = macd.iloc[-1] > signal_line.iloc[-1] and macd.iloc[-2] < signal_line.iloc[-2]
    macd_bearish = macd.iloc[-1] < signal_line.iloc[-1] and macd.iloc[-2] > signal_line.iloc[-2]

    # ── RSI Divergence ────────────────────────
    delta = c.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=13, adjust=False).mean()
    avg_loss = loss.ewm(com=13, adjust=False).mean()
    rs = avg_gain / avg_loss.replace(0, float("nan"))
    rsi = 100 - (100 / (1 + rs))

    rsi_bullish = rsi.iloc[-1] > 30 and rsi.iloc[-2] < 30
    rsi_bearish = rsi.iloc[-1] < 70 and rsi.iloc[-2] > 70

    # ── Volume Confirmation ───────────────────
    vol = df["tick_volume"] if "tick_volume" in df.columns else pd.Series([1] * len(df))
    vol_avg = vol.rolling(5).mean()
    
    recent_vol = vol.iloc[-1]
    vol_confirmation = (
        "BULLISH" if recent_vol > vol_avg.iloc[-1] * 1.3
        else "BEARISH" if recent_vol < vol_avg.iloc[-1] * 0.7
        else "NEUTRAL"
    )

    # ── Determine Overall Signal ──────────────
    bullish_count = sum([price_bullish_div, macd_bullish, rsi_bullish])
    bearish_count = sum([price_bearish, macd_bearish, rsi_bearish])

    overall_signal = (
        "BULLISH" if bullish_count >= 2
        else "BEARISH" if bearish_count >= 2
        else "NEUTRAL"
    )

    return {
        "price_bullish": price_bullish_div,
        "price_bearish": price_bearish,
        "macd_bullish": macd_bullish,
        "macd_bearish": macd_bearish,
        "rsi_bullish": rsi_bullish,
        "rsi_bearish": rsi_bearish,
        "rsi_current": round(rsi.iloc[-1], 1),
        "macd_current": round(macd.iloc[-1], 4),
        "volume_confirmation": vol_confirmation,
        "overall_signal": overall_signal,
        "bullish_count": bullish_count,
        "bearish_count": bearish_count,
    }


def detect_volume_pressure(rates) -> dict:
    """
    تحليل ضغط الحجم:
    - Buying Volume (أخضر)
    - Selling Volume (أحمر)
    - الفرق والقوة
    """
    if rates is None or len(rates) < 10:
        return {
            "buying_volume": 0,
            "selling_volume": 0,
            "volume_trend": "NEUTRAL",
            "pressure": "NONE",
        }

    df = pd.DataFrame(rates).tail(10)

    # ── حساب الحجم ────────────────────────────
    vol_col = df["tick_volume"] if "tick_volume" in df.columns else pd.Series([1] * len(df))

    # Bullish candles (green)
    bullish_candles = df[df["close"] > df["open"]]
    bullish_volume = vol_col[bullish_candles.index].sum()

    # Bearish candles (red)
    bearish_candles = df[df["close"] < df["open"]]
    bearish_volume = vol_col[bearish_candles.index].sum()

    total_volume = bullish_volume + bearish_volume
    bullish_pct = (bullish_volume / total_volume * 100) if total_volume > 0 else 0

    if bullish_pct > 60:
        pressure = "STRONG_BUY 🟢"
        trend = "BULLISH"
    elif bullish_pct > 50:
        pressure = "BUY 🟢"
        trend = "BULLISH"
    elif bullish_pct < 40:
        pressure = "STRONG_SELL 🔴"
        trend = "BEARISH"
    elif bullish_pct < 50:
        pressure = "SELL 🔴"
        trend = "BEARISH"
    else:
        pressure = "BALANCED"
        trend = "NEUTRAL"

    return {
        "buying_volume": int(bullish_volume),
        "selling_volume": int(bearish_volume),
        "bullish_percentage": round(bullish_pct, 1),
        "volume_trend": trend,
        "pressure": pressure,
        "recent_pressure": "SELLING" if bearish_volume > bullish_volume else "BUYING",
    }


def _calculate_atr(df, period=14):
    """حساب ATR من DataFrame"""
    h = df["high"]
    l = df["low"]
    c = df["close"]

    tr1 = h - l
    tr2 = (h - c.shift()).abs()
    tr3 = (l - c.shift()).abs()

    tr = pd.concat([tr1, tr2, tr3], axis=1).max(axis=1)
    atr = tr.ewm(com=period - 1, adjust=False).mean()

    return float(atr.iloc[-1]) if len(atr) > 0 else 1.0


def calculate_optimal_entry_exit(
    direction: str,
    current_price: float,
    levels: dict,
    divergence: dict,
    volume_data: dict,
) -> dict:
    """
    حساب Entry و TP و SL الأمثل بناءً على البيانات الكاملة
    """
    if direction == "SELL":
        # ── Entry: عند اختبار المقاومة ──────────
        entry = round(levels.get("recent_resistance", current_price), 2)

        # ── TP1: تصحيح بسيط (نصف المسافة) ────
        tp1 = round(levels["major_support"] + 
                   (levels["recent_resistance"] - levels["major_support"]) * 0.3, 2)

        # ── TP2: الدعم الأساسي ─────────────────
        tp2 = round(levels["major_support"], 2)

        # ── SL: فوق المقاومة ──────────────────
        sl = round(levels.get("historical_resistance", [levels["recent_resistance"] + 5])[0]
                  if levels.get("historical_resistance")
                  else levels["recent_resistance"] + 5, 2)

        signal_type = "SELL"

    else:  # BUY
        # ── Entry: عند اختبار الدعم ───────────
        entry = round(levels.get("recent_support", current_price), 2)

        # ── TP1: تصحيح بسيط (نصف المسافة) ────
        tp1 = round(levels["major_resistance"] - 
                   (levels["major_resistance"] - levels["recent_support"]) * 0.3, 2)

        # ── TP2: المقاومة الأساسية ────────────
        tp2 = round(levels["major_resistance"], 2)

        # ── SL: تحت الدعم ──────────────────────
        sl = round(levels.get("historical_support", [levels["recent_support"] - 5])[0]
                  if levels.get("historical_support")
                  else levels["recent_support"] - 5, 2)

        signal_type = "BUY"

    # ── حساب RR ────────────────────────────
    sl_distance = abs(entry - sl)
    tp1_distance = abs(tp1 - entry)
    rr = tp1_distance / sl_distance if sl_distance > 0 else 0

    # ── إنشاء التحذيرات ─────────────────────
    warnings = []
    if abs(current_price - entry) > 10:
        warnings.append(f"⚠️ السعر بعيد عن نقطة الدخول المثالية")

    if divergence.get("overall_signal") == "NEUTRAL":
        warnings.append(f"⚠️ الـ Divergence محايد - احذر")

    if volume_data.get("volume_trend") != ("BEARISH" if direction == "SELL" else "BULLISH"):
        warnings.append(f"⚠️ الحجم لا يدعم الاتجاه بقوة")

    return {
        "direction": signal_type,
        "entry": entry,
        "tp1": tp1,
        "tp2": tp2,
        "sl": sl,
        "rr": round(rr, 2),
        "warnings": warnings,
        "basis": {
            "support_level": levels["major_support"],
            "resistance_level": levels["major_resistance"],
            "divergence_signal": divergence.get("overall_signal"),
            "volume_pressure": volume_data.get("pressure"),
        },
    }
