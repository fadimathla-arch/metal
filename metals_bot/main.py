"""
main.py — نقطة التشغيل
════════════════════════
يبدأ من هنا فقط.
كل المنطق موزّع على الوحدات الأخرى.
"""

import sys
import os

# ── إصلاح ModuleNotFoundError ────────────────
# يضيف مجلد البوت لمسار البحث حتى لو شُغّل
# من مجلد مختلف أو عبر VS Code
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import threading

from datetime import datetime
from config   import (
    GEMINI_KEYS_POOL,
    TELEGRAM_TOKEN,
    TELEGRAM_CHAT_ID,
)
from logger   import log
from database import init_db
from telegram_handler import (
    send_to_telegram,
    handle_telegram_commands,
    set_analysis_event,
)
from analysis_engine  import analyze_metals_with_memory
from signal_tracker   import mt5_sync_scheduler
from schedulers       import (
    daily_summary_scheduler,
    check_price_alerts,
    wait_until_next_interval,
    get_next_run_time,
)

try:
    import MetaTrader5 as mt5
    MT5_AVAILABLE = True
except ImportError:
    MT5_AVAILABLE = False

# ── وقت بدء التشغيل ──────────────────────────
_start_time = datetime.now().strftime("%Y-%m-%d %H:%M")

# ── Event لمعرفة حالة التحليل ─────────────────
_analysis_running = threading.Event()


def _run_analysis_safe():
    """يُشغّل دورة التحليل مع حماية Event."""
    _analysis_running.set()
    try:
        analyze_metals_with_memory()
    finally:
        _analysis_running.clear()


def main():
    log.info("═" * 45)
    log.info("🤖 بوت المعادن V4.2 — بدء التشغيل")
    log.info(f"⏰ {_start_time}")
    log.info("═" * 45)

    # ── التحقق من الإعدادات ──────────────────
    if not GEMINI_KEYS_POOL:
        log.error("❌ لا توجد مفاتيح Gemini في .env")
        sys.exit(1)

    if not TELEGRAM_TOKEN or not TELEGRAM_CHAT_ID:
        log.error("❌ Telegram غير مضبوط في .env")
        sys.exit(1)

    # ── تهيئة قاعدة البيانات ─────────────────
    init_db()

    # ── الاتصال بـ MT5 ────────────────────────
    if MT5_AVAILABLE:
        if not mt5.initialize():
            log.error("❌ فشل الاتصال بـ MT5.")
            sys.exit(1)
        log.info("✅ MT5 متصل.")
    else:
        log.warning("⚠️ MT5 غير متاح — وضع محدود.")

    # ── تسجيل الـ Event في telegram_handler ───
    set_analysis_event(_analysis_running)

    # ── رسالة البداية ─────────────────────────
    send_to_telegram(
        f"🟢 *البوت يعمل الآن — V4.2*\n"
        f"⏰ {_start_time}\n"
        f"📊 يحلل كل ساعة على XX:00\n"
        f"📡 يراقب صفقاتك الفعلية في MT5\n"
        f"🔔 BB + Stochastic + VWAP مفعّلة\n"
        f"اكتب /help للأوامر"
    )

    # ── تشغيل الـ Threads ─────────────────────
    threads = [
        threading.Thread(
            target = lambda: handle_telegram_commands(
                analyze_callback  = _run_analysis_safe,
                next_run_callback = get_next_run_time,
            ),
            daemon = True,
            name   = "TelegramCommands",
        ),
        threading.Thread(
            target = check_price_alerts,
            daemon = True,
            name   = "PriceAlerts",
        ),
        threading.Thread(
            target = daily_summary_scheduler,
            daemon = True,
            name   = "DailySummary",
        ),
        threading.Thread(
            target = mt5_sync_scheduler,
            daemon = True,
            name   = "MT5Sync",
        ),
    ]

    for t in threads:
        t.start()
        log.info(f"🧵 Thread '{t.name}' يعمل.")

    # ── الحلقة الرئيسية ──────────────────────
    try:
        while True:
            _run_analysis_safe()
            wait_until_next_interval()

    except KeyboardInterrupt:
        log.info("⏹ إيقاف يدوي.")
        send_to_telegram("🔴 *البوت أُوقف يدوياً.*")
        if MT5_AVAILABLE:
            mt5.shutdown()
        sys.exit(0)

    except Exception as e:
        log.error(f"🚨 خطأ حرج: {e}")
        send_to_telegram(
            f"🚨 خطأ حرج:\n`{str(e)[:200]}`"
        )
        if MT5_AVAILABLE:
            mt5.shutdown()
        sys.exit(1)


if __name__ == "__main__":
    main()