"""
schedulers.py — المجدولات الدورية
════════════════════════════════════
- التقرير اليومي الساعة 6 صباحاً
- تنبيهات الأسعار (فحص كل دقيقة)
- توقيت دورة التحليل (رأس الساعة)
- الذاكرة التراكمية (حفظ/تحميل)
"""

import os
import shutil
import time
from datetime import datetime, timedelta

from config import (
    MEMORY_FILE, HISTORY_FOLDER,
    DAILY_REPORT_HOUR,
)
from logger import log
from database import get_win_rate, get_active_alerts, trigger_alert
from telegram_handler import send_to_telegram
from mt5_handler import ensure_mt5_connected

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False


# ╔══════════════════════════════════════════╗
# ║  1. الذاكرة التراكمية                    ║
# ╚══════════════════════════════════════════╝

def load_previous_analysis() -> str:
    """يُحمّل آخر تحليل محفوظ (آخر 1500 حرف)."""
    if not os.path.exists(MEMORY_FILE):
        return "لا يوجد تحليل سابق."
    try:
        with open(MEMORY_FILE, "r", encoding="utf-8") as f:
            content = f.read()
        return (
            content[-1500:]
            if len(content) > 1500
            else content
        )
    except Exception as e:
        log.warning(f"⚠️ فشل قراءة الذاكرة: {e}")
        return "لا يوجد تحليل سابق."


def save_current_analysis(text: str):
    """
    يحفظ التحليل الحالي:
    1. يكتب للملف الرئيسي (atomic write)
    2. يحفظ نسخة في مجلد التاريخ
    """
    tmp = MEMORY_FILE + ".tmp"
    try:
        with open(tmp, "w", encoding="utf-8") as f:
            f.write(text)
        shutil.move(tmp, MEMORY_FILE)
    except Exception as e:
        log.error(f"❌ فشل حفظ الذاكرة: {e}")
        return

    try:
        os.makedirs(HISTORY_FOLDER, exist_ok=True)
        ts   = datetime.now().strftime("%Y%m%d_%H%M%S")
        path = os.path.join(
            HISTORY_FOLDER, f"analysis_{ts}.txt"
        )
        with open(path, "w", encoding="utf-8") as f:
            f.write(text)
    except Exception as e:
        log.warning(f"⚠️ فشل حفظ النسخة التاريخية: {e}")


# ╔══════════════════════════════════════════╗
# ║  2. توقيت دورة التحليل                   ║
# ╚══════════════════════════════════════════╝

def wait_until_next_interval():
    """ينتظر حتى رأس الساعة القادمة (XX:00)."""
    now      = datetime.now()
    next_run = (now + timedelta(hours=1)).replace(
        minute=0, second=2, microsecond=0
    )
    wait = (next_run - now).total_seconds()
    log.info(
        f"⏳ الدورة القادمة: "
        f"{next_run.strftime('%H:%M')} "
        f"(بعد {wait / 60:.1f} دقيقة)"
    )
    time.sleep(max(wait, 1))


def get_next_run_time() -> str:
    """يُعيد وقت الدورة القادمة كنص (للـ /status)."""
    now = datetime.now()
    next_run = (now + timedelta(hours=1)).replace(
        minute=0, second=0, microsecond=0
    )
    return next_run.strftime("%H:%M")


# ╔══════════════════════════════════════════╗
# ║  3. التقرير اليومي                       ║
# ╚══════════════════════════════════════════╝

def daily_summary_scheduler():
    """يُرسل التقرير اليومي في الساعة المحددة."""
    while True:
        now    = datetime.now()
        target = now.replace(
            hour        = DAILY_REPORT_HOUR,
            minute      = 0,
            second      = 0,
            microsecond = 0,
        )
        if now >= target:
            target += timedelta(days=1)

        wait = (target - now).total_seconds()
        log.info(
            f"📅 التقرير اليومي بعد "
            f"{wait / 3600:.1f} ساعة"
        )
        time.sleep(wait)

        try:
            stats = get_win_rate()
            send_to_telegram(
                f"🌅 *التقرير اليومي — "
                f"{datetime.now().strftime('%Y-%m-%d')}*\n\n"
                f"📊 *الصفقات الفعلية المغلقة:*\n"
                f"إجمالي: {stats['total']}\n"
                f"رابحة: {stats['wins']} ✅\n"
                f"خاسرة: {stats['losses']} ❌\n"
                f"Win Rate: *{stats['win_rate']}%*\n"
                f"متوسط الـ pips: {stats['avg_pips']}\n"
                f"متوسط RR: {stats['avg_rr']}\n"
                f"💰 إجمالي الربح: "
                f"`${stats['total_profit']:+.2f}`\n\n"
                f"اكتب /analyze لتحليل فوري 🚀"
            )
            log.info("📅 التقرير اليومي أُرسل.")
        except Exception as e:
            log.error(f"❌ خطأ في التقرير اليومي: {e}")


# ╔══════════════════════════════════════════╗
# ║  4. تنبيهات الأسعار                      ║
# ╚══════════════════════════════════════════╝

def check_price_alerts():
    """يفحص تنبيهات الأسعار كل 60 ثانية."""
    while True:
        try:
            if not ensure_mt5_connected():
                time.sleep(60)
                continue

            rows = get_active_alerts()

            for aid, symbol, alert_price, direction, msg \
                    in rows:
                tick = mt5.symbol_info_tick(symbol)
                if not tick:
                    continue

                current = tick.bid
                hit = (
                    direction == "ABOVE"
                    and current >= alert_price
                ) or (
                    direction == "BELOW"
                    and current <= alert_price
                )

                if hit:
                    send_to_telegram(
                        f"🔔 *تنبيه سعري!*\n\n"
                        f"*{symbol}* وصل إلى `{alert_price}`\n"
                        f"السعر الحالي: `{current:.3f}`\n"
                        f"📝 {msg}"
                    )
                    trigger_alert(aid)
                    log.info(
                        f"🔔 تنبيه أُرسل: "
                        f"{symbol} @ {alert_price}"
                    )

        except Exception as e:
            log.error(f"⚠️ خطأ في التنبيهات: {e}")

        time.sleep(60)