"""
signal_tracker.py — تتبع الصفقات الفعلية من MT5
══════════════════════════════════════════════════
يراقب الصفقات الفعلية المفتوحة في MT5 كل 60 ثانية:

• صفقة جديدة:
  → يبحث عن أقرب إشارة Gemini PENDING
    (نفس الرمز + الاتجاه + فارق < ATR × 2)
  → يُرسل إشعار ربط أو "صفقة يدوية"

• صفقة مغلقة:
  → يجيب النتيجة من history_deals_get()
  → يحسب pips والربح الفعلي بالدولار
  → يُحدّث DB ويُرسل إشعار النتيجة
"""

import time
import threading
from datetime import datetime, timedelta

from config import (
    MT5_GROUP_FILTER,
    ATR_MATCH_MULTIPLIER,
    CLOSED_DEALS_LOOKBACK_HOURS,
)
from logger import log
from database import (
    db_execute, db_fetchall,
    get_pending_signals, save_signal,
)
from mt5_handler import ensure_mt5_connected, get_current_atr
from telegram_handler import send_to_telegram

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False


# ── مجموعة الـ tickets المعروفة ──────────────
_tracked_tickets: set = set()
_tracked_lock         = threading.Lock()


# ╔══════════════════════════════════════════╗
# ║  1. البحث عن إشارة Gemini مطابقة         ║
# ╚══════════════════════════════════════════╝

def _find_matching_signal(
    symbol:     str,
    direction:  str,
    open_price: float,
) -> int | None:
    """
    يبحث عن أقرب إشارة PENDING بنفس الرمز والاتجاه.
    المعيار: فارق السعر عن Entry < ATR × ATR_MATCH_MULTIPLIER
    يُعيد id الإشارة أو None.
    """
    rows = get_pending_signals(symbol, direction)
    if not rows:
        return None

    atr_threshold = (
        get_current_atr(symbol) * ATR_MATCH_MULTIPLIER
    )

    best_id    = None
    best_delta = float("inf")

    for sig_id, entry, *_ in rows:
        delta = abs(open_price - entry)
        if delta < best_delta and delta <= atr_threshold:
            best_delta = delta
            best_id    = sig_id

    return best_id


# ╔══════════════════════════════════════════╗
# ║  2. ربط صفقة جديدة بإشارة Gemini         ║
# ╚══════════════════════════════════════════╝

def _link_position_to_signal(
    ticket:     int,
    symbol:     str,
    direction:  str,
    open_price: float,
    open_time:  datetime,
    volume:     float,
):
    sig_id = _find_matching_signal(
        symbol, direction, open_price
    )

    if sig_id:
        # ربط الصفقة بالإشارة
        db_execute("""
            UPDATE signals
            SET status            = 'OPEN',
                entry_filled_time = ?,
                notes             = ?
            WHERE id = ?
        """, (
            open_time.isoformat(),
            f"ticket={ticket}",
            sig_id,
        ))

        log.info(
            f"🔗 صفقة #{ticket} {symbol} {direction} "
            f"@ {open_price} ← مربوطة بإشارة #{sig_id}"
        )
        send_to_telegram(
            f"🔗 *تم ربط صفقتك بإشارة Gemini*\n\n"
            f"رقم الصفقة: `#{ticket}`\n"
            f"الرمز: *{symbol}* | الاتجاه: *{direction}*\n"
            f"سعر الفتح: `{open_price}`\n"
            f"الحجم: `{volume} lot`\n"
            f"إشارة Gemini: `#{sig_id}`\n\n"
            f"⏳ أتابع النتيجة تلقائياً..."
        )

    else:
        # صفقة يدوية بدون إشارة مطابقة
        save_signal(
            symbol    = symbol,
            direction = direction,
            entry     = open_price,
            tp1       = 0,
            tp2       = 0,
            sl        = 0,
        )
        # نُحدّث مباشرة إلى OPEN مع الـ ticket
        rows = db_fetchall("""
            SELECT id FROM signals
            WHERE symbol = ? AND direction = ?
              AND entry = ? AND status = 'PENDING'
            ORDER BY id DESC LIMIT 1
        """, (symbol, direction, open_price))

        if rows:
            db_execute("""
                UPDATE signals
                SET status            = 'OPEN',
                    entry_filled_time = ?,
                    notes             = ?
                WHERE id = ?
            """, (
                open_time.isoformat(),
                f"manual_trade|ticket={ticket}",
                rows[0][0],
            ))

        log.info(
            f"📌 صفقة يدوية: "
            f"#{ticket} {symbol} {direction} @ {open_price}"
        )
        send_to_telegram(
            f"📌 *صفقة يدوية — خارج إشارات Gemini*\n\n"
            f"رقم الصفقة: `#{ticket}`\n"
            f"الرمز: *{symbol}* | الاتجاه: *{direction}*\n"
            f"سعر الفتح: `{open_price}`\n"
            f"الحجم: `{volume} lot`\n\n"
            f"⏳ سأتابع النتيجة وأحفظها."
        )


# ╔══════════════════════════════════════════╗
# ║  3. معالجة إغلاق صفقة                    ║
# ╚══════════════════════════════════════════╝

def _close_position_result(
    ticket:      int,
    symbol:      str,
    direction:   str,
    open_price:  float,
    close_price: float,
    profit:      float,
    volume:      float,
):
    # حساب الـ pips الفعلية
    if direction == "BUY":
        raw_pips = close_price - open_price
    else:
        raw_pips = open_price - close_price

    result_pips = round(raw_pips, 3)
    new_status  = "TP1" if profit > 0 else "SL"
    emoji       = "✅" if profit > 0 else "❌"

    # البحث عن الإشارة المرتبطة بهذا الـ ticket
    rows = db_fetchall("""
        SELECT id FROM signals
        WHERE notes LIKE ? AND status = 'OPEN'
        LIMIT 1
    """, (f"%ticket={ticket}%",))

    if rows:
        sig_id = rows[0][0]
        db_execute("""
            UPDATE signals
            SET status        = ?,
                result_pips   = ?,
                result_profit = ?
            WHERE id = ?
        """, (
            new_status,
            result_pips,
            round(profit, 2),
            sig_id,
        ))
        log.info(
            f"{emoji} إشارة #{sig_id} ← {new_status} "
            f"| pips: {result_pips} | ${profit:.2f}"
        )
    else:
        log.warning(
            f"⚠️ لم يُعثر على إشارة مرتبطة "
            f"بصفقة #{ticket}"
        )

    send_to_telegram(
        f"{emoji} *نتيجة صفقتك*\n\n"
        f"رقم الصفقة: `#{ticket}`\n"
        f"الرمز: *{symbol}* | الاتجاه: *{direction}*\n"
        f"فتح: `{open_price}` ← إغلاق: `{close_price}`\n"
        f"النتيجة: `{result_pips:+.3f} pips`\n"
        f"الربح الفعلي: `${profit:+.2f}`\n"
        f"الحجم: `{volume} lot`\n\n"
        f"{'🏆 رابحة!' if profit > 0 else '📉 خاسرة.'}"
    )


# ╔══════════════════════════════════════════╗
# ║  4. الدالة الرئيسية للمزامنة             ║
# ╚══════════════════════════════════════════╝

def sync_with_mt5_positions():
    """
    تُشغَّل كل 60 ثانية:
    1. تجيب الصفقات المفتوحة (XAUUSD / XAGUSD)
    2. تكتشف الجديدة وتربطها
    3. تكتشف المغلقة وتسجّل نتائجها
    """
    if not ensure_mt5_connected():
        return

    # ── الصفقات المفتوحة الآن ──────────────────
    positions = mt5.positions_get(group=MT5_GROUP_FILTER)
    if positions is None:
        positions = []

    current_tickets = {p.ticket for p in positions}

    with _tracked_lock:
        known_tickets = _tracked_tickets.copy()

    # ── صفقات جديدة ───────────────────────────
    new_tickets = current_tickets - known_tickets
    for pos in positions:
        if pos.ticket not in new_tickets:
            continue

        direction = "BUY" if pos.type == 0 else "SELL"
        open_time = datetime.fromtimestamp(int(pos.time))

        # ── تحذير فوري: صفقة بدون SL ──────────
        # حماية رأس المال — صفقة بلا وقف خسارة قد
        # تتحول لخسارة غير محدودة دون رقابة.
        if pos.sl == 0:
            send_to_telegram(
                f"⚠️ *تحذير: صفقة بدون وقف خسارة!*\n\n"
                f"رقم الصفقة: `#{pos.ticket}`\n"
                f"الرمز: *{pos.symbol}* | "
                f"الاتجاه: *{direction}*\n"
                f"سعر الفتح: `{round(pos.price_open, 3)}`\n"
                f"الربح/الخسارة الحالية: "
                f"`${pos.profit:+.2f}`\n\n"
                f"🔴 يُنصح بوضع SL فوراً لحماية رأس المال."
            )
            log.warning(
                f"⚠️ صفقة #{pos.ticket} بدون SL — "
                f"تم إرسال تحذير."
            )

        _link_position_to_signal(
            ticket     = pos.ticket,
            symbol     = pos.symbol,
            direction  = direction,
            open_price = round(pos.price_open, 3),
            open_time  = open_time,
            volume     = pos.volume,
        )

    # ── صفقات مغلقة ───────────────────────────
    closed_tickets = known_tickets - current_tickets
    if closed_tickets:
        from_date = datetime.now() - timedelta(
            hours=CLOSED_DEALS_LOOKBACK_HOURS
        )
        to_date = datetime.now() + timedelta(minutes=1)

        deals = mt5.history_deals_get(from_date, to_date)

        if deals:
            # قاموس: position_id → deal الفتح (entry=0)
            open_deals = {
                d.position_id: d
                for d in deals
                if d.entry == 0
            }

            for deal in deals:
                if deal.entry != 1:
                    continue
                if deal.position_id not in closed_tickets:
                    continue

                open_deal  = open_deals.get(deal.position_id)
                open_price = (
                    round(open_deal.price, 3)
                    if open_deal
                    else round(deal.price, 3)
                )
                direction = (
                    "BUY" if deal.type == 1 else "SELL"
                )

                _close_position_result(
                    ticket      = deal.position_id,
                    symbol      = deal.symbol,
                    direction   = direction,
                    open_price  = open_price,
                    close_price = round(deal.price, 3),
                    profit      = round(deal.profit, 2),
                    volume      = deal.volume,
                )

    # ── تحديث القائمة ─────────────────────────
    with _tracked_lock:
        _tracked_tickets.clear()
        _tracked_tickets.update(current_tickets)


# ╔══════════════════════════════════════════╗
# ║  5. المجدوِل                             ║
# ╚══════════════════════════════════════════╝

def mt5_sync_scheduler(interval_seconds: int = 60):
    """يُشغّل sync_with_mt5_positions() دورياً."""
    log.info("📡 مزامنة الصفقات الفعلية مع MT5 تعمل...")
    while True:
        try:
            sync_with_mt5_positions()
        except Exception as e:
            log.error(f"⚠️ خطأ في مزامنة MT5: {e}")
        time.sleep(interval_seconds)