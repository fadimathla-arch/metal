"""
metals_bot/gemini_handler.py — updated validations for SL vs OB & use liquidity context
"""

import re
import threading
from logger import log
from database import save_signal
from config import (
    GEMINI_KEYS_POOL, GEMINI_MODEL,
    MIN_SL_DISTANCE, MAX_SL_DISTANCE, MIN_RR_RATIO,
)
from confidence_engine import (
    evaluate_signal_confidence,
    calculate_signal_quality,
)

try:
    from google import genai
    GENAI_AVAILABLE = True
except ImportError:
    GENAI_AVAILABLE = False
    log.warning("⚠️ google-genai غير مثبّت.")

_key_index      = 0
_key_index_lock = threading.Lock()


def get_gemini_client():
    if not GENAI_AVAILABLE:
        raise RuntimeError("google-genai غير مثبّت.")
    if not GEMINI_KEYS_POOL:
        raise RuntimeError("لا توجد مفاتيح Gemini في .env")
    with _key_index_lock:
        key = GEMINI_KEYS_POOL[_key_index]
    return genai.Client(api_key=key)


def rotate_key():
    global _key_index
    with _key_index_lock:
        if len(GEMINI_KEYS_POOL) > 1:
            _key_index = (_key_index + 1) % len(GEMINI_KEYS_POOL)
            log.info(f"🔄 تدوير المفتاح ← {_key_index + 1}")
        else:
            log.warning("⚠️ مفتاح Gemini واحد فقط — انتظر قبل الإعادة.")


def generate_content(prompt: str) -> str | None:
    if not GENAI_AVAILABLE or not GEMINI_KEYS_POOL:
        log.warning("⚠️ Gemini غير متاح.")
        return None

    import time
    for attempt in range(5):
        try:
            client = get_gemini_client()
            log.info(f"📡 Gemini — محاولة {attempt + 1}/5")
            response = client.models.generate_content(model=GEMINI_MODEL, contents=prompt)
            if not response.text or len(response.text) < 100:
                log.warning(f"⚠️ رد قصير جداً ({len(response.text or '')} حرف) — إعادة المحاولة.")
                continue
            log.info(f"✅ رد Gemini: {len(response.text)} حرف")
            return response.text
        except Exception as e:
            err = str(e)
            log.error(f"⚠️ خطأ محاولة {attempt + 1}: {err[:100]}")
            if "429" in err or "RESOURCE_EXHAUSTED" in err:
                rotate_key()
            time.sleep((attempt + 1) * 5)
    log.error("❌ فشلت جميع محاولات Gemini.")
    return None


_SIGNAL_PATTERN = re.compile(
    r"SIGNAL\s*:.*?"
    r"DIRECTION\s*=\s*(BUY|SELL)"
    r".*?SYMBOL\s*=\s*(XAUUSD|XAGUSD)"
    r".*?ENTRY\s*=\s*([\d.]+)"
    r".*?TP1\s*=\s*([\d.]+)"
    r".*?TP2\s*=\s*([\d.]+)"
    r".*?SL\s*=\s*([\d.]+)",
    re.IGNORECASE | re.DOTALL,
)


def _sl_within_ob(symbol: str, sl: float, h1_data: dict) -> bool:
    """تتحقق أن SL موضوع ضمن OB المناسب إن وُجد (أو قريب منه)."""
    levels = h1_data.get('levels', {})
    if not levels:
        return True
    ob_buy = levels.get('order_block_buy')
    ob_sell = levels.get('order_block_sell')
    # BUY → SL يجب أن يكون أقل أو قريب من ob_buy
    if ob_buy and sl <= ob_buy + 1e-6:
        return True
    # SELL → SL أعلى أو قريب من ob_sell
    if ob_sell and sl >= ob_sell - 1e-6:
        return True
    # لا يوجد OB مناسب — نسمح لكن سنسجل تحذير
    return False


def extract_and_save_signals(text: str, liquidity: dict | None = None, h1_data: dict | None = None) -> int:
    liquidity = liquidity or {}
    h1_data   = h1_data   or {}
    matches   = _SIGNAL_PATTERN.finditer(text)
    saved     = 0
    found     = 0

    for m in matches:
        found += 1
        direction = m.group(1).upper()
        symbol    = m.group(2).upper()
        entry     = float(m.group(3))
        tp1       = float(m.group(4))
        tp2       = float(m.group(5))
        sl        = float(m.group(6))

        liq        = liquidity.get(symbol, {})
        confidence = evaluate_signal_confidence(symbol, direction, liq)

        if confidence['decision'] == 'REJECT':
            log.warning(f"⚠️ إشارة مرفوضة ({symbol} {direction}): {confidence['reason']}")
            continue
        if confidence['decision'] == 'WARN':
            log.info(f"⚠️ تحذير سيولة ({symbol} {direction}): {confidence['reason']}")

        valid, reason, rr = _validate_signal(symbol, direction, entry, tp1, tp2, sl)
        if not valid:
            log.warning(f"⚠️ إشارة مرفوضة ({symbol} {direction}): {reason}")
            continue

        # تحقق SL ضمن OB
        if not _sl_within_ob(symbol, sl, h1_data.get(symbol, {})):
            log.info(f"⚠️ SL ليس ضمن OB المتوقع لـ {symbol} — سيُقبل كـ WARN")

        sym_h1 = h1_data.get(symbol, {})
        quality = calculate_signal_quality(symbol, direction, entry, tp1, tp2, sl, sym_h1)
        log.info(f"📊 {quality['recommendation']} | {symbol} {direction}")

        if quality['score'] < 55:
            log.info(f"🚫 إشارة منخفضة الجودة ({quality['score']}/100 — POOR) — {symbol} {direction} مرفوضة.")
            continue

        ok = save_signal(symbol=symbol, direction=direction, entry=entry, tp1=tp1, tp2=tp2, sl=sl, rr_ratio=rr)
        if ok:
            saved += 1

    if found == 0 and 'SIGNAL:' in text.upper():
        log.warning("⚠️ وُجد SIGNAL: لكن لم يُستخرج — تحقق من تنسيق Gemini.")
    elif found > 0:
        log.info(f"📊 إشارات: وُجدت {found} | صالحة ومحفوظة: {saved}")

    return saved


def _validate_signal(symbol: str, direction: str, entry: float, tp1: float, tp2: float, sl: float) -> tuple[bool, str, float]:
    if any(v <= 0 for v in [entry, tp1, sl]):
        return False, "أرقام غير منطقية (صفر أو سالبة)", 0.0
    if direction == 'BUY':
        if not (tp1 > entry > sl):
            return False, f"BUY: يجب tp1({tp1}) > entry({entry}) > sl({sl})", 0.0
    else:
        if not (tp1 < entry < sl):
            return False, f"SELL: يجب tp1({tp1}) < entry({entry}) < sl({sl})", 0.0

    sl_distance = abs(entry - sl)
    min_sl = MIN_SL_DISTANCE.get(symbol, 0)
    max_sl = MAX_SL_DISTANCE.get(symbol, 9999)
    if sl_distance < min_sl:
        return False, f"SL ضيق: {sl_distance:.3f} < {min_sl}", 0.0
    if sl_distance > max_sl:
        return False, f"SL بعيد جداً: {sl_distance:.3f} > {max_sl} — يجب أن يكون تحت/فوق OB مباشرة", 0.0

    tp1_distance = abs(tp1 - entry)
    rr = tp1_distance / sl_distance if sl_distance > 0 else 0
    if rr < MIN_RR_RATIO:
        return False, f"RR ضعيف: {rr:.2f} < {MIN_RR_RATIO}", rr
    return True, 'صالحة', round(rr, 2)
