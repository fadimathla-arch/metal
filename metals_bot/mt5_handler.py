"""
mt5_handler.py — كل شيء يخص MT5
════════════════════════════════════════════
- الاتصال وإعادة الاتصال
- المؤشرات التقنية المحسّنة:
    EMA (20/50/200) | RSI | ATR
    Bollinger Bands | Stochastic | VWAP
- فيبوناتشي Retracement + Extension
- Order Blocks + BOS
- Fair Value Gaps (FVG) المفلترة
- ملخص الرمز للـ Prompt
"""

import time
import pandas as pd
import numpy as np
from datetime import datetime

from config import (
    EMA_PERIODS, RSI_PERIOD, ATR_PERIOD,
    BB_PERIOD, BB_STD,
    STOCH_K, STOCH_D, STOCH_SMOOTH,
    BARS_H4, BARS_H1, BARS_M15,
    BARS_FVG, BARS_OB, OB_LOOKBACK,
    DXY_SYMBOLS, SYMBOLS,
)
from logger import log
from advanced_levels import detect_levels

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False
    log.warning("⚠️ MetaTrader5 غير مثبّت.")


# ╔══════════════════════════════════════════╗
# ║  1. إدارة الاتصال                        ║
# ╚══════════════════════════════════════════╝

def ensure_mt5_connected() -> bool:
    if not MT5_AVAILABLE:
        return False
    if mt5.terminal_info() is not None:
        return True

    log.warning("⚠️ MT5 منقطع — إعادة الاتصال...")
    for attempt in range(3):
        if mt5.initialize():
            log.info("✅ MT5 متصل مجدداً.")
            return True
        log.warning(f"محاولة {attempt + 1}/3 فشلت.")
        time.sleep(3)

    log.error("❌ فشل الاتصال بـ MT5 نهائياً.")
    return False


def get_current_price(symbol: str) -> float:
    """يُعيد سعر Ask الحالي أو 0 عند الفشل."""
    if not ensure_mt5_connected():
        return 0.0
    tick = mt5.symbol_info_tick(symbol)
    return round(tick.ask, 3) if tick else 0.0


# ╔════════════════════════════════��═════════╗
# ║  2. المؤشرات التقنية المحسّنة            ║
# ╚══════════════════════════════════════════╝

def calculate_indicators(rates) -> dict:
    """
    يحسب مجموعة كاملة من المؤشرات:
    EMA 20/50/200 | RSI | ATR
    Bollinger Bands | Stochastic | VWAP
    """
    if rates is None or len(rates) < 50:
        return {}

    df = pd.DataFrame(rates)
    c  = df["close"]
    h  = df["high"]
    l  = df["low"]

    # ── EMA ──────────────────────────────────
    ema20  = c.ewm(span=20,  adjust=False).mean()
    ema50  = c.ewm(span=50,  adjust=False).mean()
    ema200 = c.ewm(span=200, adjust=False).mean()

    # ── RSI ──────────────────────────────────
    delta    = c.diff()
    gain     = delta.clip(lower=0)
    loss     = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    avg_loss = loss.ewm(com=RSI_PERIOD - 1, adjust=False).mean()
    rs       = avg_gain / avg_loss.replace(0, float("nan"))
    rsi      = 100 - (100 / (1 + rs))
    rsi_val  = float(rsi.iloc[-1])

    # ── ATR ──────────────────────────────────
    hl  = h - l
    hcp = (h - c.shift()).abs()
    lcp = (l - c.shift()).abs()
    atr = pd.concat(
        [hl, hcp, lcp], axis=1
    ).max(axis=1).ewm(com=ATR_PERIOD - 1, adjust=False).mean()
    atr_val = float(atr.iloc[-1])

    # ── Bollinger Bands ───────────────────────
    bb_mid   = c.rolling(BB_PERIOD).mean()
    bb_std   = c.rolling(BB_PERIOD).std()
    bb_upper = bb_mid + BB_STD * bb_std
    bb_lower = bb_mid - BB_STD * bb_std

    price    = float(c.iloc[-1])
    bb_pos   = _bb_position(
        price,
        float(bb_upper.iloc[-1]),
        float(bb_lower.iloc[-1]),
        float(bb_mid.iloc[-1]),
    )
    bb_width = round(
        (float(bb_upper.iloc[-1]) - float(bb_lower.iloc[-1]))
        / float(bb_mid.iloc[-1]) * 100, 2
    )

    # ── Stochastic ───────────────────────────
    low_min  = l.rolling(STOCH_K).min()
    high_max = h.rolling(STOCH_K).max()
    stoch_k  = 100 * (c - low_min) / (high_max - low_min + 1e-10)
    stoch_k  = stoch_k.rolling(STOCH_SMOOTH).mean()
    stoch_d  = stoch_k.rolling(STOCH_D).mean()
    sk_val   = round(float(stoch_k.iloc[-1]), 1)
    sd_val   = round(float(stoch_d.iloc[-1]), 1)
    stoch_signal = _stoch_signal(sk_val, sd_val)

    # ── VWAP (اليوم الحالي فقط) ───────────────
    vwap = _calculate_vwap(df)

    # ── حجم التداول ──────────────────────────
    vol = (
        df["tick_volume"]
        if "tick_volume" in df.columns
        else pd.Series([0] * len(df))
    )
    vol_avg   = vol.rolling(20).mean()
    vol_trend = (
        "مرتفع 🔥"
        if vol.iloc[-1] > vol_avg.iloc[-1] * 1.5
        else "فوق المتوسط"
        if vol.iloc[-1] > vol_avg.iloc[-1] * 1.2
        else "عادي"
    )

    # ── اتجاه EMA ────────────────────────────
    e20  = float(ema20.iloc[-1])
    e50  = float(ema50.iloc[-1])
    e200 = float(ema200.iloc[-1])
    ema_trend = _ema_trend(price, e20, e50, e200)

    # ── حالة RSI ─────────────────────────────
    rsi_state = (
        f"{rsi_val:.1f} ⚠️ تشبع شرائي"
        if rsi_val >= 70
        else f"{rsi_val:.1f} ⚠️ تشبع بيعي"
        if rsi_val <= 30
        else f"{rsi_val:.1f} ✅ محايد"
    )

    return {
        # EMA
        "ema20":     round(e20,  3),
        "ema50":     round(e50,  3),
        "ema200":    round(e200, 3),
        "ema_trend": ema_trend,
        # RSI
        "rsi":       round(rsi_val, 1),
        "rsi_state": rsi_state,
        # ATR
        "atr":       round(atr_val, 3),
        # Bollinger Bands
        "bb_upper":  round(float(bb_upper.iloc[-1]), 3),
        "bb_mid":    round(float(bb_mid.iloc[-1]),   3),
        "bb_lower":  round(float(bb_lower.iloc[-1]), 3),
        "bb_pos":    bb_pos,
        "bb_width":  bb_width,
        # Stochastic
        "stoch_k":      sk_val,
        "stoch_d":      sd_val,
        "stoch_signal": stoch_signal,
        # VWAP
        "vwap":      round(vwap, 3) if vwap else None,
        # Volume
        "vol_trend": vol_trend,
    }


def _ema_trend(price, e20, e50, e200) -> str:
    if price > e20 > e50 > e200:
        return "صاعد قوي ↑↑"
    elif price > e50 > e200:
        return "صاعد معتدل ↑"
    elif price > e200:
        return "فوق EMA200 ↑"
    elif price < e20 < e50 < e200:
        return "هابط قوي ↓↓"
    elif price < e50 < e200:
        return "هابط معتدل ↓"
    elif price < e200:
        return "تحت EMA200 ↓"
    return "محايد ↔"


def _bb_position(price, upper, lower, mid) -> str:
    if price >= upper:
        return "فوق الحد العلوي ⚠️ (تشبع)"
    elif price >= mid:
        return "بين المتوسط والحد العلوي"
    elif price <= lower:
        return "تحت الحد السفلي ⚠️ (تشبع)"
    else:
        return "بين المتوسط والحد السفلي"


def _stoch_signal(k: float, d: float) -> str:
    if k >= 80 and d >= 80:
        return f"K={k} D={d} ⚠️ تشبع شرائي"
    elif k <= 20 and d <= 20:
        return f"K={k} D={d} ⚠️ تشبع بيعي"
    elif k > d and k < 80:
        return f"K={k} D={d} 📈 إشارة شراء"
    elif k < d and k > 20:
        return f"K={k} D={d} 📉 إشارة بيع"
    return f"K={k} D={d} محايد"


def _calculate_vwap(df: pd.DataFrame) -> float | None:
    """
    يحسب VWAP اليومي من الشموع المتاحة.
    يُعيد None إذا لم تتوفر بيانات الحجم.
    """
    try:
        vol = (
            df["tick_volume"]
            if "tick_volume" in df.columns
            else None
        )
        if vol is None or vol.sum() == 0:
            return None

        typical = (df["high"] + df["low"] + df["close"]) / 3
        vwap    = (typical * vol).cumsum() / vol.cumsum()
        return float(vwap.iloc[-1])
    except Exception:
        return None

[TRUNCATED FOR BREVITY - rest of file unchanged]
