"""
confidence_engine.py — محرك تقييم الثقة بالإشارات
════════════════════════════════════════════════════
بدل الرفض الثنائي المطلق (قبول/رفض) لإشارة قرب
القاع/القمة، هذا المحرك يحسب درجة ثقة (0-100) ويتخذ
قراراً متدرجاً:

  - liquidity_score منخفض  → الإشارة تمر عادي
  - liquidity_score متوسط  → تمر مع تخفيض RR المطلوب
  - liquidity_score عالي
    + Divergence مضاد
    + حجم تداول صاعد        → رفض قوي

يشمل أيضاً: Signal Quality Score (0-100) لتقييم
جودة كل إشارة قبل حفظها.
"""

from logger import log


# ── أوزان نظام السيولة ────────────────────────
HARD_REJECT_SCORE_THRESHOLD = 70
SOFT_WARNING_SCORE_THRESHOLD = 40


def evaluate_signal_confidence(
    symbol:    str,
    direction: str,
    liquidity: dict,
) -> dict:
    """
    يحسب قرار الإشارة بناءً على بيانات السيولة.
    يُعيد: {"decision": "ALLOW"|"WARN"|"REJECT", "reason", "score"}
    """
    score = liquidity.get("liquidity_score", 0)

    near_low  = liquidity.get("near_major_low",  False)
    near_high = liquidity.get("near_major_high", False)

    bullish_div = liquidity.get("bullish_divergence", False)
    bearish_div = liquidity.get("bearish_divergence", False)
    vol_rising  = liquidity.get("volume_rising", False)

    # BUY قرب القاع أو SELL قرب القمة — طبيعي
    if direction == "BUY" and near_low:
        return _allow(score, "شراء قرب قاع — اتجاه منطقي")
    if direction == "SELL" and near_high:
        return _allow(score, "بيع قرب قمة — اتجاه منطقي")

    # الحالة الخطرة: SELL قرب القاع
    if direction == "SELL" and near_low:
        return _evaluate_against_trend(
            symbol, direction, score,
            divergence = bullish_div,
            vol_rising = vol_rising,
            zone_label = "القاع الرئيسي",
        )

    # الحالة الخطرة: BUY قرب القمة
    if direction == "BUY" and near_high:
        return _evaluate_against_trend(
            symbol, direction, score,
            divergence = bearish_div,
            vol_rising = vol_rising,
            zone_label = "القمة الرئيسية",
        )

    return _allow(score, "لا توجد منطقة سيولة قريبة")


def _evaluate_against_trend(
    symbol:     str,
    direction:  str,
    score:      int,
    divergence: bool,
    vol_rising: bool,
    zone_label: str,
) -> dict:
    if (
        score >= HARD_REJECT_SCORE_THRESHOLD
        and divergence
        and vol_rising
    ):
        return {
            "decision": "REJECT",
            "reason": (
                f"{symbol} {direction}: رفض قوي — قرب "
                f"{zone_label} (score={score}) مع "
                f"Divergence مضاد وحجم صاعد."
            ),
            "score": score,
        }

    if score >= SOFT_WARNING_SCORE_THRESHOLD:
        return {
            "decision": "WARN",
            "reason": (
                f"{symbol} {direction}: تحذير — قرب "
                f"{zone_label} (score={score}) — تُمرَّر مع الحذر."
            ),
            "score": score,
        }

    return _allow(
        score,
        f"قرب {zone_label} لكن score منخفض ({score})"
    )


def _allow(score: int, reason: str) -> dict:
    return {"decision": "ALLOW", "reason": reason, "score": score}


# ╔══════════════════════════════════════════╗
# ║  Signal Quality Score — نظام تقييم جودة  ║
# ╚══════════════════════════════════════════╝

def calculate_signal_quality(
    symbol:    str,
    direction: str,
    entry:     float,
    tp1:       float,
    tp2:       float,
    sl:        float,
    h1_data:   dict,
) -> dict:
    """
    يحسب درجة جودة الإشارة (0-100) بناءً على 4 معايير:

    1. Entry يطابق Fib Level أو OB (25 نقطة)
    2. RR ≥ 1.5 بناءً على TP1       (25 نقطة)
    3. SL تحت/فوق مستوى دعم/مقاومة  (25 نقطة)
    4. Confluence: كم مؤشر متفق      (25 نقطة)

    يُعيد:
        {
            "score":         int 0-100,
            "quality_level": "EXCELLENT"|"GOOD"|"FAIR"|"POOR",
            "components":    dict,
            "recommendation": str,
        }
    """
    components = {}
    sl_distance = abs(entry - sl)

    # ── 1. Entry قريب من Fib Level ───────────
    fib_raw  = h1_data.get("fibonacci", "")
    fib_score = _score_entry_fib_alignment(entry, fib_raw, symbol)
    components["entry_fib"] = fib_score

    # ── 2. جودة RR ────────────────────────────
    if sl_distance > 0:
        rr = abs(tp1 - entry) / sl_distance
        if rr >= 2.0:
            components["rr_quality"] = 25
        elif rr >= 1.5:
            components["rr_quality"] = 20
        elif rr >= 1.2:
            components["rr_quality"] = 10
        else:
            components["rr_quality"] = 0
    else:
        components["rr_quality"] = 0
        rr = 0

    # ── 3. SL بالنسبة لمستويات S/R ───────────
    support_list    = h1_data.get("support_list",    [])
    resistance_list = h1_data.get("resistance_list", [])
    components["sl_protection"] = _score_sl_placement(
        direction, entry, sl, support_list, resistance_list
    )

    # ── 4. Confluence (كم مؤشر يتفق مع Direction) ──
    components["confluence"] = _score_confluence(
        direction, entry, h1_data
    )

    total = min(100, sum(components.values()))
    level = (
        "EXCELLENT" if total >= 85
        else "GOOD"  if total >= 70
        else "FAIR"  if total >= 55
        else "POOR"
    )

    emoji = {
        "EXCELLENT": "🏆", "GOOD": "✅",
        "FAIR": "⚠️", "POOR": "❌"
    }

    recommendation = (
        f"{emoji[level]} جودة الإشارة: {level} "
        f"({total}/100) | RR: {round(rr, 2)}"
    )

    return {
        "score":          total,
        "quality_level":  level,
        "components":     components,
        "recommendation": recommendation,
        "rr":             round(rr, 2) if sl_distance > 0 else 0,
    }


def _score_entry_fib_alignment(
    entry:   float,
    fib_raw: str,
    symbol:  str,
) -> int:
    """
    يستخرج مستويات Fib من النص بـ pattern دقيق
    (Ret X.XXX : Y.YYY) بدل كل الأرقام العشوائية.
    """
    import re
    tolerance = 5.0 if symbol == "XAUUSD" else 0.05

    # ✅ استخرج أسعار Ret و Ext فقط — يتجنب الأرقام العشوائية
    patterns = re.findall(
        r"(?:Ret|Ext)\s+[\d.]+\s*:\s*([\d.]+)", fib_raw
    )

    if not patterns:
        return 10  # لا يمكن التحقق — نقاط جزئية

    numbers  = [float(p) for p in patterns]
    min_dist = min(abs(entry - n) for n in numbers)

    if min_dist <= tolerance:
        return 25
    elif min_dist <= tolerance * 3:
        return 15
    else:
        return 0


def _score_sl_placement(
    direction:       str,
    entry:           float,
    sl:              float,
    support_list:    list,
    resistance_list: list,
) -> int:
    """SL تحت آخر دعم (BUY) أو فوق آخر مقاومة (SELL)."""
    if direction == "BUY" and support_list:
        supports_below = [s for s in support_list if s < entry]
        if supports_below:
            nearest_support = max(supports_below)
            if sl < nearest_support:
                return 25
            elif sl < entry:
                return 10
    elif direction == "SELL" and resistance_list:
        resistances_above = [r for r in resistance_list if r > entry]
        if resistances_above:
            nearest_resistance = min(resistances_above)
            if sl > nearest_resistance:
                return 25
            elif sl > entry:
                return 10
    return 5


def _score_confluence(
    direction: str,
    entry:     float,
    h1_data:   dict,
) -> int:
    """
    يحسب Confluence من 7 مؤشرات (بدل 4 سابقاً):
    EMA + RSI + Stoch + OB + Divergence + Liquidity + Volume
    كل مؤشر = 4 نقاط → max 28 → يُقلَّص لـ 25
    """
    count = 0
    ind   = h1_data.get("indicators", {})
    liq   = h1_data.get("liquidity", {})

    # 1. EMA Trend
    ema_trend = ind.get("ema_trend", "")
    if direction == "BUY"  and "صاعد" in ema_trend: count += 1
    if direction == "SELL" and "هابط" in ema_trend: count += 1

    # 2. RSI
    rsi_val = ind.get("rsi", 50)
    if direction == "BUY"  and rsi_val <= 40: count += 1
    if direction == "SELL" and rsi_val >= 60: count += 1

    # 3. Stochastic
    stoch = ind.get("stoch_signal", "")
    if direction == "BUY"  and ("شراء" in stoch or "بيعي" in stoch): count += 1
    if direction == "SELL" and ("بيع"  in stoch or "شرائي" in stoch): count += 1

    # 4. OB في نفس اتجاه الصفقة
    ob_text = h1_data.get("order_blocks", "")
    if direction == "BUY"  and "شرائي" in ob_text: count += 1
    if direction == "SELL" and "بيعي"  in ob_text: count += 1

    # ✅ 5. Divergence
    if direction == "BUY"  and liq.get("bullish_divergence"): count += 1
    if direction == "SELL" and liq.get("bearish_divergence"): count += 1

    # ✅ 6. Liquidity Score منخفض = جيد (لا توجد منطقة سيولة خطرة)
    if liq.get("liquidity_score", 0) <= 30: count += 1

    # ✅ 7. Volume صاعد يدعم الحركة
    vol_trend = ind.get("vol_trend", "")
    if "مرتفع" in vol_trend or "فوق" in vol_trend: count += 1

    return min(25, count * 4)  # max 7×4=28 → cap 25