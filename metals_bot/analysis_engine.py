"""
analysis_engine.py — محرك التحليل الرئيسي (مُحدّث لعرض النتائج النهائية)
"""

from datetime import datetime

from config import BARS_H4, BARS_H1, BARS_M15, BARS_FVG, MIN_RR_RATIO
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

    # لو لا يوجد توافق مع القواعد → WAIT
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

    # بناء prompt
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

    liquidity_context = {"XAUUSD": summaries["gold_h1"].get("liquidity", {}), "XAGUSD": summaries["silver_h1"].get("liquidity", {})}
    h1_context = {"XAUUSD": summaries["gold_h1"], "XAGUSD": summaries["silver_h1"]}
    saved = extract_and_save_signals(response_text, liquidity_context, h1_context)

    # دمج الاقتراح البرمجي مع ناتج Gemini (أولوية للإشارات المحفوظة)
    import time

    def _filter_gemini_for_symbol(gemini_text: str, symbol: str) -> str:
        if not gemini_text:
            return ""
        lines = gemini_text.splitlines()
        symbol_lines = [ln for ln in lines if symbol.upper() in ln or symbol.lower() in ln.lower()]
        if symbol_lines:
            return "\n".join(symbol_lines)
        return gemini_text[:1000] + ("..." if len(gemini_text) > 1000 else "")

    # Build gold message
    gold_lines = []
    gold_lines.append(f"📍 DXY: {dxy_text}")
    gold_lines.append(f"🪙 الذهب: {_format_brief('XAUUSD', summaries['gold_h1'])}")
    gold_lines.append("\n══ اقتراح برمجي (مبني على المستويات):")
    gold_lines.append(f"ذهب: {_format_signal_text('XAUUSD', gold_sugg)}")
    gold_lines.append("\n══ إخراج Gemini (مختصر):")
    gold_lines.append(_filter_gemini_for_symbol(response_text, "XAUUSD"))

    gold_report = "\n".join(gold_lines)

    # Build silver message
    silver_lines = []
    silver_lines.append(f"📍 DXY: {dxy_text}")
    silver_lines.append(f"🥈 الفضة: {_format_brief('XAGUSD', summaries['silver_h1'])}")
    silver_lines.append("\n══ اقتراح برمجي (مبني على المستويات):")
    silver_lines.append(f"فضة: {_format_signal_text('XAGUSD', silver_sugg)}")
    silver_lines.append("\n══ إخراج Gemini (مختصر):")
    silver_lines.append(_filter_gemini_for_symbol(response_text, "XAGUSD"))

    silver_report = "\n".join(silver_lines)

    # Send separately with short pause (to avoid hitting telegram rate limits)
    sent_gold = send_to_telegram(gold_report)
    time.sleep(1.0)
    sent_silver = send_to_telegram(silver_report)

    if sent_gold and sent_silver:
        log.info("✅ تقارير الذهب والفضة أُرسلت إلى Telegram.")
    else:
        log.warning("⚠️ فشل إرسال أحد التقارير إلى Telegram.")
