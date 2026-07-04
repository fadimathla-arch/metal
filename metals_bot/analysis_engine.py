"""
analysis_engine.py — محرك التحليل الرئيسي (مُحدّث لعرض النتائج النهائية)
"""

from datetime import datetime

from config import BARS_H4, BARS_H1, BARS_M15, BARS_FVG, MIN_RR_RATIO
from logger import log
from database import (
    get_win_rate, save_analysis,
    save_signal_return_id, update_signal_review,
)
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


def _format_brief(symbol: str, h1: dict) -> str:
    return (
        f"{symbol} {h1.get('last_close')} | Trend H1: {h1.get('trend')} | "
        f"Support: {h1.get('support')} | Resistance: {h1.get('resistance')}"
    )


def _suggest_signals_from_levels(symbol: str, h1: dict) -> dict:
    """اقتراح إشارة برمجياً بناءً على المستويات المكتشفة.
    يعيد dict بصيغة جاهزة للعرض والحفظ أو SIGNAL: WAIT.
    """
    levels = h1.get('levels') or {}
    ind = h1.get('indicators', {})

    price = float(h1.get('last_close', 0))

    support = levels.get('support')
    resistance = levels.get('resistance')
    ob_buy = levels.get('order_block_buy')
    ob_sell = levels.get('order_block_sell')

    # سيناريو افتراضي: SELL عند المقاومة الأولى، BUY عند الدعم الأساسي
    entry_sell = resistance
    tp1_sell = round((price + resistance) / 2 - 0.5, 3) if resistance else None
    tp2_sell = support

    entry_buy = support
    tp1_buy = round((price + support) / 2 + 0.5, 3) if support else None
    tp2_buy = resistance

    # حساب SL من swing+ATR المرجعي
    sl_sell = calculate_optimal_sl('SELL', entry_sell or price, None)['sl_price'] if entry_sell else None
    sl_buy  = calculate_optimal_sl('BUY', entry_buy or price, None)['sl_price'] if entry_buy else None

    # حساب RR
    def rr(entry, tp1, sl):
        if not all([entry, tp1, sl]):
            return 0
        return round(abs(tp1 - entry) / max(abs(entry - sl), 1e-8), 2)

    rr_sell = rr(entry_sell, tp1_sell, sl_sell)
    rr_buy  = rr(entry_buy, tp1_buy, sl_buy)

    # قواعد القبول الأساسية
    sell_ok = entry_sell and tp1_sell and sl_sell and rr_sell >= MIN_RR_RATIO
    buy_ok  = entry_buy  and tp1_buy  and sl_buy  and rr_buy  >= MIN_RR_RATIO

    # لو لا ��وجد توافق مع القواعد → WAIT
    if not sell_ok and not buy_ok:
        return {"signal": "WAIT"}

    # اختر التوصية وفق توافق H1 (أفضلية لاتجاه H1)
    prefer = 'SELL' if 'هابط' in h1.get('trend','') else 'BUY'
    if prefer == 'SELL' and sell_ok:
        return {
            'signal': 'SELL', 'entry': entry_sell, 'tp1': tp1_sell, 'tp2': tp2_sell, 'sl': sl_sell, 'rr': rr_sell,
            'basis': 'H1 trend + Resistance/Support', 'warnings': []
        }
    if prefer == 'BUY' and buy_ok:
        return {
            'signal': 'BUY', 'entry': entry_buy, 'tp1': tp1_buy, 'tp2': tp2_buy, 'sl': sl_buy, 'rr': rr_buy,
            'basis': 'H1 trend + Support', 'warnings': []
        }

    # إن لم تُحقق أولوية الاتجاه، اختر الإشارة الأقوى RR
    chosen = 'SELL' if rr_sell >= rr_buy else 'BUY'
    if chosen == 'SELL' and sell_ok:
        return {
            'signal': 'SELL', 'entry': entry_sell, 'tp1': tp1_sell, 'tp2': tp2_sell, 'sl': sl_sell, 'rr': rr_sell,
            'basis': 'RR optimized', 'warnings': []
        }
    if chosen == 'BUY' and buy_ok:
        return {
            'signal': 'BUY', 'entry': entry_buy, 'tp1': tp1_buy, 'tp2': tp2_buy, 'sl': sl_buy, 'rr': rr_buy,
            'basis': 'RR optimized', 'warnings': []
        }

    return {"signal": "WAIT"}


def _format_signal_text(sym: str, suggestion: dict) -> str:
    if suggestion.get('signal') == 'WAIT':
        return f"SIGNAL: WAIT"
    return (
        f"SIGNAL: DIRECTION={suggestion['signal']}, SYMBOL={sym}, ENTRY={suggestion['entry']}, "
        f"TP1={suggestion['tp1']}, TP2={suggestion['tp2']}, SL={suggestion['sl']}, RR={suggestion['rr']}"
    )


def _parse_json_block(text: str) -> dict | None:
    import re, json
    m = re.search(r"```json\s*(\{.*?\})\s*```", text, re.DOTALL)
    if not m:
        # try to find a plain JSON object if no backticks
        m2 = re.search(r"(\{\s*\"report\".*\})", text, re.DOTALL)
        if not m2:
            return None
        try:
            return json.loads(m2.group(1))
        except Exception:
            return None
    try:
        return json.loads(m.group(1))
    except Exception:
        return None


def _build_review_prompt(symbol: str, suggestion: dict, market_context: dict) -> str:
    """
    Build a strict review-only prompt for Gemini. market_context contains summaries and DXY.
    The prompt forces Gemini to output JSON with 'reviews' array and fields: approved, confidence, review_warning, notes.
    """
    dxy = market_context.get('dxy_text', '')
    summary = market_context.get('summary', {})
    h1 = market_context.get('h1', {})

    instruction = (
        "أنت مراجع فني فقط — لا تقترح قرارات تنفيذ.\n"
        "راجع الإشارة التالية وقيّمها بناءً على السياق الفني والمخاطر.\n"
        "أجب فقط عبر JSON داخل بلوك ```json ... ``` بالصيغة التالية بدقة:\n\n"
        "{\n"
        "  \"report\": \"<ملخّص قصير>\",\n"
        "  \"reviews\": [\n"
        "    {\"symbol\":\"XAUUSD\",\"approved\":true,\"confidence\":0.85,\"review_warning\":\"liquidity low\",\"notes\":\"...\"}\n"
        "  ]\n"
        "}\n\n"
        "ملاحظات: يجب أن تحتوي القائمة reviews على عنصر واحد لهذا الرمز. approved قيمة منطقية، confidence رقم بين 0 و1.\n"
    )

    sig = suggestion
    sig_text = (
        f"Signal: {sig.get('signal')} Entry={sig.get('entry')} TP1={sig.get('tp1')} TP2={sig.get('tp2')} SL={sig.get('sl')} RR={sig.get('rr')}"
    )

    context = (
        f"DXY: {dxy}\n"
        f"Market H1 summary: trend={h1.get('trend')} last_close={h1.get('last_close')} support={h1.get('support')} resistance={h1.get('resistance')}\n"
        f"Signal to review: {sig_text}\n"
    )

    prompt = context + "\n" + instruction
    return prompt


def _build_prompt(
    dxy_text: str,
    dxy_direction: str,
    gold_now: float,
    silver_now: float,
    gold_h4: dict,
    gold_h1: dict,
    gold_m15: dict,
    silver_h4: dict,
    silver_h1: dict,
    silver_m15: dict,
    gold_fvg: dict,
    silver_fvg: dict,
    stats: dict,
    previous: dict,
) -> str:
    """
    Build the main prompt for Gemini containing market context and a clear instruction.
    The model is asked to provide a readable analysis and to include a JSON block with report and signals
    so the extractor can parse any signals present.
    """
    parts = []
    parts.append(f"DXY: {dxy_text} | Direction: {dxy_direction}")
    parts.append("\n-- GOLD H1 --")
    parts.append(f"Last: {gold_now} | Trend: {gold_h1.get('trend')} | Support: {gold_h1.get('support')} | Resistance: {gold_h1.get('resistance')}")
    parts.append("Indicators: \n" + str(gold_h1.get('indicators', {})))
    parts.append("\n-- SILVER H1 --")
    parts.append(f"Last: {silver_now} | Trend: {silver_h1.get('trend')} | Support: {silver_h1.get('support')} | Resistance: {silver_h1.get('resistance')}")
    parts.append("Indicators: \n" + str(silver_h1.get('indicators', {})))

    parts.append("\n-- FVGs --")
    parts.append(f"Gold FVG: {gold_fvg.get('summary') if isinstance(gold_fvg, dict) else gold_fvg}")
    parts.append(f"Silver FVG: {silver_fvg.get('summary') if isinstance(silver_fvg, dict) else silver_fvg}")

    parts.append("\n-- Stats --")
    parts.append(str(stats))

    instruction = (
        "\n\nأنت محلل فني مساعد. أكتب تحليلًا واضحًا ومُفسّرًا للذهب والفضة مبنيًا على البيانات أعلاه. "
        "اختصر النتائج العملية أولاً (بجملة أو اثنتين). \n"
        "ثم قَدّم شرحًا مختصرًا للتقنيات والمستويات. \n"
        "الرجاء إضافة بلوك JSON في النهاية داخل ```json ... ``` به الحقول: report (string) و signals (قائمة احتمالية). "
        "كل عنصر في signals إن وُجد يجب أن يحتوي: symbol, direction, entry, tp1, tp2, sl. "
        "إن لم تكن هناك توصية أعد signals: [] أو أدرج status: 'WAIT'."
    )

    prompt = "\n\n".join(parts) + "\n\n" + instruction
    return prompt


def analyze_metals_with_memory():
    log.info("═" * 45)
    log.info("🔄 بدء دورة التحليل...")

    if not ensure_mt5_connected():
        log.error("❌ MT5 غير متصل — تخطي الدورة.")
        return

    # جمع البيانات
    dxy_text, dxy_direction = analyze_dxy_trend()

    gold_fvg   = check_fvg_imbalance("XAUUSD", mt5.TIMEFRAME_H1, BARS_FVG)
    silver_fvg = check_fvg_imbalance("XAGUSD", mt5.TIMEFRAME_H1, BARS_FVG)

    summaries = {
        "gold_h4":    get_symbol_summary("XAUUSD", mt5.TIMEFRAME_H4,  BARS_H4),
        "gold_h1":    get_symbol_summary("XAUUSD", mt5.TIMEFRAME_H1,  BARS_H1),
        "gold_m15":   get_symbol_summary("XAUUSD", mt5.TIMEFRAME_M15, BARS_M15),
        "silver_h4":  get_symbol_summary("XAGUSD", mt5.TIMEFRAME_H4,  BARS_H4),
        "silver_h1":  get_symbol_summary("XAGUSD", mt5.TIMEFRAME_H1,  BARS_H1),
        "silver_m15": get_symbol_summary("XAGUSD", mt5.TIMEFRAME_M15, BARS_M15),
    }

    # تأكد من عدم وجود أخطاء
    critical = ["gold_h4", "gold_h1", "silver_h4", "silver_h1"]
    for key in critical:
        if "error" in summaries[key]:
            log.error(f"❌ فشل جلب {key}: {summaries[key]['error']} — تخطي.")
            return

    gold_now   = get_current_price("XAUUSD")
    silver_now = get_current_price("XAGUSD")

    if not gold_now or not silver_now:
        log.warning("⚠️ أسعار غير صالحة — تخطي.")
        return

    previous = load_previous_analysis()
    stats    = get_win_rate()

    # طرح إشارات برمجية من المستويات
    gold_sugg = _suggest_signals_from_levels("XAUUSD", summaries['gold_h1'])
    silver_sugg = _suggest_signals_from_levels("XAGUSD", summaries['silver_h1'])

    # حفظ قرار الاستراتيجية (إن لم يكن WAIT) واسترجاع id
    saved_signals = {}
    for sym, sugg in [("XAUUSD", gold_sugg), ("XAGUSD", silver_sugg)]:
        if sugg.get('signal') != 'WAIT':
            sid = save_signal_return_id(
                symbol = sym,
                direction = sugg['signal'],
                entry = sugg['entry'],
                tp1 = sugg['tp1'],
                tp2 = sugg['tp2'],
                sl = sugg['sl'],
                rr_ratio = sugg.get('rr', 0.0),
            )
            if sid:
                saved_signals[sym] = {'id': sid, 'suggestion': sugg}

    # بناء prompt العام وإرسال طلب Gemini العام (تحليلي)
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
        send_to_telegram("⚠️ فشل التحليل — إعادة في الدورة القادمة.")
        return

    save_current_analysis(response_text)
    save_analysis(content=response_text, dxy=dxy_text, gold_price=gold_now, silver_price=silver_now)

    # اطلب مراجعة منفصلة لكل إشارة محفوظة بواسطة Gemini (review-only)
    for sym, info in saved_signals.items():
        sig = info['suggestion']
        signal_id = info['id']
        review_prompt = _build_review_prompt(sym, sig, { 'dxy_text': dxy_text, 'h1': summaries[f"{ 'gold' if sym == 'XAUUSD' else 'silver' }_h1" ] })
        review_resp = generate_content(review_prompt)
        parsed = _parse_json_block(review_resp or "")
        if parsed and parsed.get('reviews'):
            # find matching review
            for r in parsed.get('reviews'):
                if r.get('symbol','').upper() == sym:
                    approved = int(bool(r.get('approved', False)))
                    confidence = float(r.get('confidence', 0.0)) if r.get('confidence') is not None else None
                    review_warning = r.get('review_warning')
                    update_signal_review(signal_id, approved, confidence, review_warning)
                    log.info(f"🔎 Gemini review for id={signal_id}: approved={approved} confidence={confidence} warning={review_warning}")
                    break

    # بالإضافة نقوم باستخراج إشارات Gemini التقليدية (إن وجدت) كـ fallback
    liquidity_context = {"XAUUSD": summaries["gold_h1"].get("liquidity", {}), "XAGUSD": summaries["silver_h1"].get("liquidity", {})}
    h1_context = {"XAUUSD": summaries["gold_h1"], "XAGUSD": summaries["silver_h1"]}
    extras_saved, gemini_report = extract_and_save_signals(response_text, liquidity_context, h1_context)

    # بناء رسائل منفصلة لكل معدن تتضمن قرار الاستراتيجية ونتيجة مراجعة Gemini
    import time

    def _filter_gemini_for_symbol(gemini_text: str, symbol: str) -> str:
        if not gemini_text:
            return ""
        lines = gemini_text.splitlines()
        symbol_lines = [ln for ln in lines if symbol.upper() in ln or symbol.lower() in ln.lower()]
        if symbol_lines:
            return "\n".join(symbol_lines)
        return gemini_text[:1000] + ("..." if len(gemini_text) > 1000 else "")

    # Gold message
    gold_review = None
    if 'XAUUSD' in saved_signals:
        sid = saved_signals['XAUUSD']['id']
        # fetch review fields from DB quickly
        try:
            row = __import__('sqlite3').connect(__import__('config').DB_FILE).execute("SELECT approved, confidence, review_warning FROM signals WHERE id=?", (sid,)).fetchone()
            if row:
                gold_review = {'approved': bool(row[0]), 'confidence': row[1], 'review_warning': row[2]}
        except Exception:
            gold_review = None

    gold_lines = []
    gold_lines.append(f"📍 DXY: {dxy_text}")
    gold_lines.append(f"🪙 الذهب: {_format_brief('XAUUSD', summaries['gold_h1'])}")
    gold_lines.append("\n══ اقتراح برمجي (مبني على المستويات):")
    gold_lines.append(f"{_format_signal_text('XAUUSD', gold_sugg)}")
    if gold_review:
        status = "✅ Approved" if gold_review.get('approved') else "❌ Rejected"
        gold_lines.append(f"Gemini Review: {status} | Confidence: {gold_review.get('confidence')} | Warning: {gold_review.get('review_warning')}")
    gold_lines.append("\n══ إخراج Gemini (مختصر):")
    gold_lines.append(_filter_gemini_for_symbol(response_text, "XAUUSD"))
    gold_report = "\n".join(gold_lines)

    # Silver message
    silver_review = None
    if 'XAGUSD' in saved_signals:
        sid = saved_signals['XAGUSD']['id']
        try:
            row = __import__('sqlite3').connect(__import__('config').DB_FILE).execute("SELECT approved, confidence, review_warning FROM signals WHERE id=?", (sid,)).fetchone()
            if row:
                silver_review = {'approved': bool(row[0]), 'confidence': row[1], 'review_warning': row[2]}
        except Exception:
            silver_review = None

    silver_lines = []
    silver_lines.append(f"📍 DXY: {dxy_text}")
    silver_lines.append(f"🥈 الفضة: {_format_brief('XAGUSD', summaries['silver_h1'])}")
    silver_lines.append("\n══ اقتراح برمجي (مبني على المستويات):")
    silver_lines.append(f"{_format_signal_text('XAGUSD', silver_sugg)}")
    if silver_review:
        status = "✅ Approved" if silver_review.get('approved') else "❌ Rejected"
        silver_lines.append(f"Gemini Review: {status} | Confidence: {silver_review.get('confidence')} | Warning: {silver_review.get('review_warning')}")
    silver_lines.append("\n══ إخراج Gemini (مختصر):")
    silver_lines.append(_filter_gemini_for_symbol(response_text, "XAGUSD"))
    silver_report = "\n".join(silver_lines)

    # إرسال منفصل
    sent_gold = send_to_telegram(gold_report)
    time.sleep(1.0)
    sent_silver = send_to_telegram(silver_report)

    if sent_gold and sent_silver:
        log.info("✅ تقارير الذهب والفضة أُرسلت إلى Telegram.")
    else:
        log.warning("⚠️ فشل إرسال أحد التقارير إلى Telegram.")
