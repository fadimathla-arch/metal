"""
analysis_engine.py — محرك التحليل الرئيسي
════════════════════════════════════════════
يجمع كل البيانات ويبني الـ Prompt ويُرسله لـ Gemini
ويُعالج الرد ويُرسله لـ Telegram.
"""

from datetime import datetime

from config import BARS_H4, BARS_H1, BARS_M15, BARS_FVG
from logger import log
from database import get_win_rate, save_analysis
from mt5_handler import (
    ensure_mt5_connected,
    get_symbol_summary,
    check_fvg_imbalance,
    analyze_dxy_trend,
    get_current_price,
    get_open_positions_summary,
    calculate_optimal_sl,
)
from gemini_handler import (
    generate_content,
    extract_and_save_signals,
)
from schedulers import (
    load_previous_analysis,
    save_current_analysis,
)
from telegram_handler import send_to_telegram

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False


# ╔══════════════════════════════════════════╗
# ║  1. بناء الـ Prompt                      ║
# ╚══════════════════════════════════════════╝

def _build_dxy_rule(dxy_direction: str) -> str:
    if dxy_direction == "DOWN":
        return (
            "⚠️ الدولار هابط = تحيّز للشراء في المعادن. "
            "إذا أوصيت بالبيع، اذكر سبباً تقنياً واضحاً "
            "(مثال: OB بيعي قوي أو تشبع شرائي شديد)."
        )
    if dxy_direction == "UP":
        return (
            "⚠️ الدولار صاعد = ضغط على المعادن. "
            "إذا أوصيت بالشراء، اذكر سبباً تقنياً واضحاً "
            "(مثال: OB شرائي قوي أو تشبع بيعي شديد)."
        )
    return "بيانات الدولار غير متاحة — اعتمد على الصورة التقنية فقط."


def _format_liquidity_line(h1: dict) -> str:
    """
    يبني سطر معلومات السيولة — كمرجع استرشادي للنموذج
    وليس أمراً قاطعاً، لأن القرار النهائي (رفض/تحذير/سماح)
    يُحسم برمجياً لاحقاً عبر confidence_engine بناءً على
    Divergence والحجم، وليس القرب وحده.
    """
    liq = h1.get("liquidity", {})
    score = liq.get("liquidity_score", 0)

    if liq.get("near_major_low"):
        div_note = (
            " + ضعف بيعي محتمل (Divergence)"
            if liq.get("bullish_divergence") else ""
        )
        return (
            f"📍 قرب القاع الرئيسي ({liq.get('major_low')}) "
            f"— {liq.get('distance_atr')}×ATR{div_note} "
            f"| liquidity_score={score}. "
            f"كن حذراً مع BUY فقط لو رأيت زخماً نزولياً "
            f"قوياً مستمراً (موجة دافعة) رغم القرب.\n"
        )
    if liq.get("near_major_high"):
        div_note = (
            " + ضعف شرائي محتمل (Divergence)"
            if liq.get("bearish_divergence") else ""
        )
        return (
            f"📍 قرب القمة الرئيسية ({liq.get('major_high')}) "
            f"— {liq.get('distance_atr')}×ATR{div_note} "
            f"| liquidity_score={score}. "
            f"كن حذراً مع SELL فقط لو رأيت زخماً صعودياً "
            f"قوياً مستمراً (موجة دافعة) رغم القرب.\n"
        )
    return ""


def _format_open_positions_line(symbol: str) -> str:
    """
    يبني سطر تحذير لو توجد صفقة مفتوحة فعلياً على هذا
    الرمز — حتى يكون Gemini على علم بالوضع الفعلي للحساب
    قبل اقتراح اتجاه جديد، ويُذكر التعارض صراحة في رده
    بدل تجاهله تماماً.

    هذا لا يمنع التوصية المعاكسة (قد تكون صحيحة فنياً)،
    لكنه يضمن أن المستخدم يرى تنبيهاً واضحاً بالتعارض.
    """
    positions = get_open_positions_summary(symbol)
    if not positions:
        return ""

    lines = [f"📂 صفقات مفتوحة فعلياً على {symbol}:"]
    for p in positions:
        lines.append(
            f"   #{p['ticket']} {p['direction']} "
            f"@ {p['price_open']} "
            f"(ربح/خسارة: ${p['profit']:+.2f})"
        )
    lines.append(
        "   ⚠️ إذا كانت توصيتك الجديدة بعكس اتجاه هذه "
        "الصفقة، اذكر ذلك صراحةً في تحليلك ووضّح أن هذا "
        "يعني هيدج أو تعارضاً مباشراً.\n"
    )
    return "\n".join(lines)


def _format_symbol_section(
    name:    str,
    symbol:  str,
    price:   float,
    h4:      dict,
    h1:      dict,
    m15:     dict,
    fvg:     str,
) -> str:
    ind = h1.get("indicators", {})

    bb_line = (
        f"     BB: {ind.get('bb_pos')} "
        f"| عرض: {ind.get('bb_width')}%\n"
        if ind.get("bb_pos") else ""
    )
    stoch_line = (
        f"     Stoch: {ind.get('stoch_signal')}\n"
        if ind.get("stoch_signal") else ""
    )
    vwap_line = (
        f"     VWAP: {ind.get('vwap')}\n"
        if ind.get("vwap") else ""
    )
    liquidity_line      = _format_liquidity_line(h1)
    open_positions_line = _format_open_positions_line(symbol)

    fib_h4 = h4.get("fibonacci", "—")
    fib_h1 = h1.get("fibonacci", "—")

    # ✅ SL الأمثل محسوب مسبقاً مع rates الفعلية في get_symbol_summary
    sl_buy  = h1.get("sl_buy",  {})
    sl_sell = h1.get("sl_sell", {})
    sl_ref_line = (
        f"     SL المرجعي (من Swing+ATR):\n"
        f"       BUY  → تحت {sl_buy.get('sl_price','—')} "
        f"(مسافة={sl_buy.get('sl_distance','—')}, "
        f"أساس={sl_buy.get('basis','—')})\n"
        f"       SELL → فوق {sl_sell.get('sl_price','—')} "
        f"(مسافة={sl_sell.get('sl_distance','—')}, "
        f"أساس={sl_sell.get('basis','—')})\n"
    )

    return (
        f"═══ {name} — {price} ═══\n"
        f"H4 : {h4.get('trend')} "
        f"| أعلى: {h4.get('high')} "
        f"| أدنى: {h4.get('low')}\n"
        f"H1 : {h1.get('trend')} "
        f"| إغلاق: {h1.get('last_close')}\n"
        f"     RSI: {ind.get('rsi_state')}\n"
        f"     EMA: {ind.get('ema_trend')}\n"
        f"     ATR: {ind.get('atr')}\n"
        f"     Vol: {ind.get('vol_trend')}\n"
        f"{bb_line}"
        f"{stoch_line}"
        f"{vwap_line}"
        f"     دعم (Swing): {h1.get('support')}\n"
        f"     مقاومة (Swing): {h1.get('resistance')}\n"
        f"{sl_ref_line}"
        f"M15: {m15.get('trend')}\n"
        f"FVG:\n{fvg}\n"
        f"OB + BOS:\n{h1.get('order_blocks')}\n"
        f"فيبوناتشي H4:\n{fib_h4}\n"
        f"فيبوناتشي H1 (دخول دقيق):\n{fib_h1}\n"
        f"{liquidity_line}"
        f"{open_positions_line}"
    )


def _build_prompt(
    dxy_text:      str,
    dxy_direction: str,
    gold_now:      float,
    silver_now:    float,
    gold_h4:       dict,
    gold_h1:       dict,
    gold_m15:      dict,
    silver_h4:     dict,
    silver_h1:     dict,
    silver_m15:    dict,
    gold_fvg:      str,
    silver_fvg:    str,
    stats:         dict,
    previous:      str,
) -> str:

    dxy_rule   = _build_dxy_rule(dxy_direction)
    prev_short = (
        " ".join(previous.split()[:80]) + "..."
        if len(previous) > 50
        else previous
    )

    gold_section   = _format_symbol_section(
        "الذهب XAUUSD", "XAUUSD", gold_now,
        gold_h4, gold_h1, gold_m15, gold_fvg,
    )
    silver_section = _format_symbol_section(
        "الفضة XAGUSD", "XAGUSD", silver_now,
        silver_h4, silver_h1, silver_m15, silver_fvg,
    )

    return f"""
بصفتك كبير استراتيجيي التداول ومحلل SMC المتخصص في المعادن.

╔══ قواعد صارمة لا تُخالَف ══╗
1. التوصية الرقمية من H1 فقط.
2. {dxy_rule}
3. تحديد SL:
   - XAUUSD: لا يقل عن 35 وحدة ولا يزيد عن 80 وحدة
   - XAGUSD: لا يقل عن 0.25 ولا يزيد عن 1.5
   - يُوضع SL دائماً تحت/فوق OB المستخدم للدخول مباشرة
   - لا تستخدم أدنى/أعلى الفترة الزمنية لحساب SL
   إذا لم يتوفر SL مناسب → أصدر SIGNAL: WAIT
4. نسبة Risk/Reward لا تقل عن 1:1.5 بناءً على TP1
5. راعِ مؤشرات Bollinger Bands وStochastic في تقييم التشبع.
6. استخدم مستويات فيبوناتشي H1 كمرجع للدخول والأهداف:
   - الدخول المفضّل عند: Ret 0.382 أو 0.5 أو 0.618 (⭐)
   - الأهداف المفضّلة: Ext 1.0 أو 1.272 أو 1.618
   - المستويات مذكورة بالأرقام الفعلية في قسم كل رمز
     أدناه (انظر "فيبوناتشي H1") — استخدم تلك الأرقام
     مباشرة في Entry/TP بدل حساب تقريبي.
   - إذا لم تتوافق الفيبوناتشي، اعتمد OB أو FVG بدلاً منها.
   - مثال: قاع=4000 وقمة=4100 →
     Ret 0.382 = 4038.2 | Ret 0.618 = 4061.8
     Ext 1.618 = 4161.8 (هدف)
6b. SL من Swing Points + ATR (مذكور تحت كل رمز):
   - استخدم "SL المرجعي (من Swing+ATR)" الظاهر أدناه
     كنقطة انطلاق — يمكنك تعديله بـ ±5 نقاط حسب السياق
     لكن لا تتجاوزه بأكثر من ATR واحد.
7. قاعدة السيولة (استرشادية — القرار النهائي برمجي):
   - عند ظهور "📍" بجانب رمز، فالسعر قريب من قاع/قمة
     رئيسية حقيقية (Swing) — ليس مجرد حافة فترة زمنية.
   - هذا لا يعني تلقائياً تجنّب الاتجاه المعاكس — في
     الموجات الدافعة القوية (Wave 3 / Wave C) قد تكون
     فرصة ممتازة فعلاً.
   - فقط كن أكثر تحفظاً وصريحاً في تحليلك: اذكر إن كان
     الزخم الحالي (حجم تداول، RSI) يدعم استمرار الحركة
     أم يُظهر ضعفاً (Divergence) قد يدعم انعكاساً.
   - النظام البرمجي سيُقيّم Divergence والحجم تلقائياً
     ويرفض فقط الحالات شديدة الخطورة.
8. قاعدة تعارض الصفقات المفتوحة:
   - عند ظهور "📂 صفقات مفتوحة فعلياً" بجانب رمز، تحقق
     من اتجاهها قبل إصدار توصيتك.
   - لو توصيتك بنفس اتجاه الصفقة المفتوحة، اذكر أنها
     تدعمها (Add-on/تأكيد الاتجاه).
   - لو توصيتك معاكسة، يجب ذكر ذلك صراحةً في أول سطر
     من تحليلك (مثال: "⚠️ ملاحظة: هذه التوصية معاكسة
     لصفقتك المفتوحة #X — قد ينتج هيدج"). لا تتجاهل
     هذا التعارض ولا تخفِه.
╚═════════════════════════════╝

═══ مؤشر الدولار ═══
{dxy_text}

═══ إحصائيات الصفقات الفعلية ═══
Win Rate: {stats['win_rate']}% | إجمالي: {stats['total']} صفقة
إجمالي الربح: ${stats['total_profit']:+.2f} | متوسط RR: {stats['avg_rr']}

═══ ملخص الذاكرة ═══
{prev_short}

{gold_section}
{silver_section}
المطلوب — تقرير Telegram مكثف:
* تحديث M15: مصير التوصية السابقة (سطر واحد)

1. 🪙 الذهب {gold_now}:
   تحليل توافقي (سطرين) مع ذكر DXY وBB وStoch ومستوى فيبوناتشي
   ⬛ Entry: ___ (Fib ___) | TP1: ___ | TP2: ___ | SL: ___
   نوع الأمر: معلق/سوقي | الجهة: شراء/بيع

2. 🥈 الفضة {silver_now}:
   تحليل توافقي (سطرين) مع ذكر DXY وBB وStoch ومستوى فيبوناتشي
   ⬛ Entry: ___ (Fib ___) | TP1: ___ | TP2: ___ | SL: ___
   نوع الأمر: معلق/سوقي | الجهة: شراء/بيع

* تحذير واحد إن وجد. بدون ديباجات.

أنهِ دائماً بسطر منفصل لكل رمز:
SIGNAL: DIRECTION=BUY أو SELL, SYMBOL=XAUUSD أو XAGUSD, ENTRY=X, TP1=Y, TP2=Z, SL=W
أو عند الانتظار:
SIGNAL: WAIT
"""


# ╔══════════════════════════════════════════╗
# ║  2. دورة التحليل الكاملة                 ║
# ╚══════════════════════════════════════════╝

def analyze_metals_with_memory():
    log.info("═" * 45)
    log.info("🔄 بدء دورة التحليل...")

    if not ensure_mt5_connected():
        log.error("❌ MT5 غير متصل — تخطي الدورة.")
        return

    # ── جمع البيانات ─────────────────────────
    dxy_text, dxy_direction = analyze_dxy_trend()

    gold_fvg   = check_fvg_imbalance(
        "XAUUSD", mt5.TIMEFRAME_H1, BARS_FVG
    )
    silver_fvg = check_fvg_imbalance(
        "XAGUSD", mt5.TIMEFRAME_H1, BARS_FVG
    )

    summaries = {
        "gold_h4":    get_symbol_summary("XAUUSD", mt5.TIMEFRAME_H4,  BARS_H4),
        "gold_h1":    get_symbol_summary("XAUUSD", mt5.TIMEFRAME_H1,  BARS_H1),
        "gold_m15":   get_symbol_summary("XAUUSD", mt5.TIMEFRAME_M15, BARS_M15),
        "silver_h4":  get_symbol_summary("XAGUSD", mt5.TIMEFRAME_H4,  BARS_H4),
        "silver_h1":  get_symbol_summary("XAGUSD", mt5.TIMEFRAME_H1,  BARS_H1),
        "silver_m15": get_symbol_summary("XAGUSD", mt5.TIMEFRAME_M15, BARS_M15),
    }

    # ── التحقق من البيانات الأساسية ───────────
    critical = ["gold_h4", "gold_h1", "silver_h4", "silver_h1"]
    for key in critical:
        if "error" in summaries[key]:
            log.error(
                f"❌ فشل جلب {key}: "
                f"{summaries[key]['error']} — تخطي."
            )
            return

    gold_now   = get_current_price("XAUUSD")
    silver_now = get_current_price("XAGUSD")

    if not gold_now or not silver_now:
        log.warning("⚠️ أسعار غير صالحة — تخطي.")
        return

    # ── بناء وإرسال الـ Prompt ───────────────
    previous = load_previous_analysis()
    stats    = get_win_rate()

    prompt = _build_prompt(
        dxy_text      = dxy_text,
        dxy_direction = dxy_direction,
        gold_now      = gold_now,
        silver_now    = silver_now,
        gold_h4       = summaries["gold_h4"],
        gold_h1       = summaries["gold_h1"],
        gold_m15      = summaries["gold_m15"],
        silver_h4     = summaries["silver_h4"],
        silver_h1     = summaries["silver_h1"],
        silver_m15    = summaries["silver_m15"],
        gold_fvg      = gold_fvg,
        silver_fvg    = silver_fvg,
        stats         = stats,
        previous      = previous,
    )

    response_text = generate_content(prompt)

    if not response_text:
        send_to_telegram(
            "⚠️ فشل التحليل — إعادة في الدورة القادمة."
        )
        return

    # ── حفظ النتائج ──────────────────────────
    save_current_analysis(response_text)
    save_analysis(
        content      = response_text,
        dxy          = dxy_text,
        gold_price   = gold_now,
        silver_price = silver_now,
    )

    # بيانات السيولة + H1 لكل رمز — تُمرَّر للفلتر البرمجي
    liquidity_context = {
        "XAUUSD": summaries["gold_h1"].get("liquidity", {}),
        "XAGUSD": summaries["silver_h1"].get("liquidity", {}),
    }
    h1_context = {
        "XAUUSD": summaries["gold_h1"],
        "XAGUSD": summaries["silver_h1"],
    }
    saved = extract_and_save_signals(
        response_text, liquidity_context, h1_context
    )

    # ── بناء التقرير النهائي ──────────────────
    gold_obs   = summaries["gold_h1"].get("order_blocks", "—")
    silver_obs = summaries["silver_h1"].get("order_blocks", "—")

    signal_line = (
        f"💾 إشارات محفوظة: {saved}"
        if saved > 0
        else "⏳ لا توجد إشارات (WAIT أو رُفضت)"
    )

    final_report = (
        f"👑 *[تقرير SMC — "
        f"{datetime.now().strftime('%H:%M')}]*\n\n"
        f"🧭 *الدولار:* `{dxy_text}`\n"
        f"🏆 *Win Rate:* `{stats['win_rate']}%` "
        f"({stats['total']} صفقة فعلية)\n"
        f"💰 *إجمالي الربح:* `${stats['total_profit']:+.2f}`\n"
        f"{signal_line}\n\n"
        f"📍 *OB H1:*\n"
        f"🪙 الذهب:\n{gold_obs}\n\n"
        f"🥈 الفضة:\n{silver_obs}\n"
        f"{'─' * 30}\n\n"
        f"{response_text}"
    )

    if send_to_telegram(final_report):
        log.info("✅ التقرير أُرسل لـ Telegram.")
    else:
        log.warning("⚠️ فشل إرسال التقرير لـ Telegram.")