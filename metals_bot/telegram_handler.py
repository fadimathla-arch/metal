"""
telegram_handler.py — كل شيء يخص Telegram
════════════════════════════════════════════
- إرسال الرسائل مع Rate Limiting
- معالجة الأوامر
- الأوامر: /analyze /status /winrate /trades /alerts /help
"""

import time
import threading
from collections import deque

import requests

from config import TELEGRAM_TOKEN, TELEGRAM_CHAT_ID
from logger import log
from database import (
    get_win_rate, get_recent_trades,
    get_open_signals_count, get_active_alerts,
    trigger_alert,
)


# ╔══════════════════════════════════════════╗
# ║  1. Rate Limiting + إرسال               ║
# ╚══════════════════════════════════════════╝

_tg_timestamps: deque = deque(maxlen=20)
_tg_lock = threading.Lock()

# يُضبط من main.py بعد إنشاء Event
_analysis_running: threading.Event = threading.Event()


def set_analysis_event(event: threading.Event):
    """يُسجّل الـ Event الخارجي لمعرفة حالة التحليل."""
    global _analysis_running
    _analysis_running = event


def send_to_telegram(
    message:  str,
    chat_id:  str = "",
) -> bool:
    if not TELEGRAM_TOKEN:
        return False

    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid:
        return False

    # حساب وقت الانتظار داخل Lock
    wait_seconds = 0.0
    with _tg_lock:
        now = time.time()
        while (
            _tg_timestamps
            and now - _tg_timestamps[0] > 60
        ):
            _tg_timestamps.popleft()

        if len(_tg_timestamps) >= 20:
            wait_seconds = 60 - (now - _tg_timestamps[0])

        _tg_timestamps.append(time.time())

    # sleep خارج Lock
    if wait_seconds > 0:
        log.info(
            f"⏳ Rate limit — انتظار {wait_seconds:.1f}s"
        )
        time.sleep(wait_seconds)

    url    = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )
    chunks = [
        message[i: i + 3800]
        for i in range(0, len(message), 3800)
    ]
    success = True

    for chunk in chunks:
        for retry in range(3):
            try:
                resp = requests.post(
                    url,
                    json={
                        "chat_id":    cid,
                        "text":       chunk,
                        "parse_mode": "Markdown",
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    break
                log.warning(
                    f"⚠️ Telegram {resp.status_code} "
                    f"— محاولة {retry + 1}/3"
                )
                time.sleep(2)
            except requests.RequestException as e:
                log.error(f"❌ Telegram: {e}")
                time.sleep(2)
        else:
            success = False

    return success


def _send_plain(message: str, chat_id: str = "") -> bool:
    """
    يُرسل رسالة بدون Markdown — للردود التي قد تحتوي
    على أحرف خاصة تكسر التنسيق.
    """
    if not TELEGRAM_TOKEN:
        return False

    cid = chat_id or TELEGRAM_CHAT_ID
    if not cid:
        return False

    url    = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/sendMessage"
    )
    chunks = [
        message[i: i + 3800]
        for i in range(0, len(message), 3800)
    ]

    for chunk in chunks:
        for retry in range(3):
            try:
                resp = requests.post(
                    url,
                    json={
                        "chat_id": cid,
                        "text":    chunk,
                        # بدون parse_mode — نص عادي
                    },
                    timeout=10,
                )
                if resp.status_code == 200:
                    break
                log.warning(
                    f"⚠️ _send_plain {resp.status_code} "
                    f"— محاولة {retry + 1}/3"
                )
                time.sleep(2)
            except requests.RequestException as e:
                log.error(f"❌ _send_plain: {e}")
                time.sleep(2)

    return True


# ╔══════════════════════════════════════════╗
# ║  2. معالج الأوامر                        ║
# ╚══════════════════════════════════════════╝

def handle_telegram_commands(
    analyze_callback,
    next_run_callback,
):
    """
    يستمع لأوامر Telegram في حلقة لا نهائية.
    analyze_callback: دالة تُشغّل تحليلاً فورياً
    next_run_callback: دالة تُعيد وقت الدورة القادمة
    """
    offset   = 0
    url      = (
        f"https://api.telegram.org/"
        f"bot{TELEGRAM_TOKEN}/getUpdates"
    )
    commands = {
        "/analyze": lambda cid: _cmd_analyze(cid, analyze_callback),
        "/status":  lambda cid: _cmd_status(cid, next_run_callback),
        "/winrate": _cmd_winrate,
        "/trades":  _cmd_trades,
        "/alerts":  _cmd_alerts,
        "/chart":   _cmd_chart,
        "/help":    _cmd_help,
    }

    log.info("🤖 مستمع أوامر Telegram يعمل...")

    while True:
        try:
            resp = requests.get(
                url,
                params={"offset": offset, "timeout": 30},
                timeout=35,
            )
            if resp.status_code != 200:
                time.sleep(5)
                continue

            for update in resp.json().get("result", []):
                offset = update["update_id"] + 1
                msg    = update.get("message", {})
                text   = msg.get("text", "").strip()
                cid    = str(
                    msg.get("chat", {}).get("id", "")
                )

                if cid != TELEGRAM_CHAT_ID:
                    continue

                # ── صورة مرفقة مع /chart ─────────────
                caption = msg.get("caption", "").strip()
                photos  = msg.get("photo", [])
                if photos and caption.lower().startswith("/chart"):
                    log.info("📸 صورة شارت واردة")
                    try:
                        # أكبر دقة متاحة
                        file_id = photos[-1]["file_id"]
                        threading.Thread(
                            target = _cmd_chart_with_photo,
                            args   = (cid, file_id),
                            daemon = True,
                        ).start()
                    except Exception as e:
                        log.error(f"⚠️ خطأ في معالجة الصورة: {e}")
                    continue

                # ── أوامر نصية عادية ─────────────────
                raw_cmd = text.split()[0] if text else ""
                cmd     = raw_cmd.split("@")[0].lower()

                if cmd in commands:
                    log.info(f"📩 أمر وارد: {cmd}")
                    try:
                        commands[cmd](cid)
                    except Exception as e:
                        log.error(
                            f"⚠️ خطأ في تنفيذ {cmd}: {e}"
                        )

        except requests.exceptions.RequestException as e:
            log.warning(f"⚠️ انقطاع شبكة: {e}")
            time.sleep(10)
        except Exception as e:
            log.error(f"⚠️ خطأ في مستمع الأوامر: {e}")
            time.sleep(10)


# ╔══════════════════════════════════════════╗
# ║  3. تنفيذ الأوامر                        ║
# ╚══════════════════════════════════════════╝

def _cmd_analyze(cid: str, analyze_callback):
    if _analysis_running.is_set():
        send_to_telegram(
            "⏳ تحليل جارٍ بالفعل، انتظر قليلاً...",
            cid,
        )
        return

    send_to_telegram("🔄 جاري تنفيذ تحليل فوري...", cid)
    threading.Thread(
        target=analyze_callback,
        daemon=True,
    ).start()


def _cmd_status(cid: str, next_run_callback):
    stats      = get_win_rate()
    open_count = get_open_signals_count()
    next_run   = next_run_callback()
    status     = (
        "🔄 تحليل يعمل الآن"
        if _analysis_running.is_set()
        else "✅ في الانتظار"
    )

    msg = (
        "📊 *حالة البوت*\n\n"
        f"الحالة: {status}\n"
        f"⏭ الدورة القادمة: `{next_run}`\n\n"
        f"📂 *الصفقات الفعلية:*\n"
        f"مفتوحة الآن: {open_count}\n"
        f"إجمالي مغلقة: {stats['total']}\n"
        f"🟢 رابحة: {stats['wins']}\n"
        f"🔴 خاسرة: {stats['losses']}\n"
        f"🎯 Win Rate: *{stats['win_rate']}%*\n"
        f"💰 إجمالي الربح: `${stats['total_profit']:+.2f}`\n"
        f"📊 متوسط الـ pips: {stats['avg_pips']}\n"
        f"📐 متوسط RR: {stats['avg_rr']}"
    )
    send_to_telegram(msg, cid)


def _cmd_winrate(cid: str):
    stats = get_win_rate()
    if stats["total"] == 0:
        send_to_telegram(
            "📭 لا توجد صفقات مغلقة بعد.\n"
            "سأحسب النتائج من صفقاتك الفعلية في MT5.",
            cid,
        )
        return

    filled = int(stats["win_rate"] / 10)
    bar    = "🟩" * filled + "⬜" * (10 - filled)
    msg    = (
        f"🏆 *Win Rate التفصيلي*\n\n"
        f"{bar}\n"
        f"النسبة: *{stats['win_rate']}%*\n\n"
        f"إجمالي: {stats['total']} صفقة فعلية\n"
        f"رابحة: {stats['wins']} ✅\n"
        f"خاسرة: {stats['losses']} ❌\n"
        f"متوسط الـ pips: {stats['avg_pips']}\n"
        f"إجمالي الربح: `${stats['total_profit']:+.2f}`\n"
        f"متوسط RR: {stats['avg_rr']}"
    )
    send_to_telegram(msg, cid)


def _cmd_trades(cid: str):
    rows = get_recent_trades(limit=5)
    if not rows:
        send_to_telegram(
            "📭 لا توجد صفقات مغلقة بعد.", cid
        )
        return

    lines = ["📋 *آخر 5 صفقات فعلية:*\n"]
    for r in rows:
        symbol, direction, entry, status, \
            pips, profit, rr, ts = r
        emoji = "✅" if status != "SL" else "❌"
        date  = ts[:16] if ts else "—"
        lines.append(
            f"{emoji} *{symbol}* {direction} @ {entry}\n"
            f"   النتيجة: {status} | "
            f"{pips:+.2f} pips | `${profit:+.2f}`\n"
            f"   RR: {rr:.2f} | {date}"
        )

    send_to_telegram("\n\n".join(lines), cid)


def _cmd_alerts(cid: str):
    rows = get_active_alerts()
    if not rows:
        send_to_telegram(
            "🔔 لا توجد تنبيهات نشطة.", cid
        )
        return

    lines = ["🔔 *التنبيهات النشطة:*\n"]
    for r in rows:
        _, symbol, price, direction, message = r
        lines.append(
            f"• *{symbol}* @ `{price}` "
            f"({direction}): {message}"
        )
    send_to_telegram("\n".join(lines), cid)


def _cmd_chart(cid: str):
    """يُرسل تعليمات استخدام أمر /chart."""
    send_to_telegram(
        "📸 *تحليل الشارت بصرياً*\n\n"
        "أرسل صورة شارت H1 مع كتابة `/chart` كتعليق.\n\n"
        "مثال:\n"
        "1️⃣ التقط screenshot للشارت على H1\n"
        "2️⃣ أرسل الصورة في Telegram\n"
        "3️⃣ اكتب `/chart` في خانة التعليق\n\n"
        "⏳ سيحلل Gemini الشارت مع بيانات MT5.",
        cid,
    )


def _cmd_chart_with_photo(cid: str, file_id: str):
    """
    يستقبل صورة الشارت، يجلبها من Telegram،
    يرسلها لـ Gemini Vision مع بيانات MT5 الحالية،
    ويُعيد التحليل البصري.
    """
    send_to_telegram(
        "🔍 جاري تحليل الشارت بصرياً...", cid
    )

    # ── جلب الصورة من Telegram ───────────────
    image_b64 = _download_telegram_photo(file_id)
    if not image_b64:
        send_to_telegram(
            "❌ فشل تحميل الصورة — حاول مجدداً.", cid
        )
        return

    # ── جلب بيانات MT5 الحالية ───────────────
    context = _get_chart_context()

    # ── إرسال لـ Gemini Vision ────────────────
    analysis = _analyze_chart_with_gemini(
        image_b64, context
    )
    if not analysis:
        send_to_telegram(
            "❌ فشل تحليل الصورة — حاول مجدداً.", cid
        )
        return

    log.info(f"📊 رد Gemini Vision ({len(analysis)} حرف)")

    # إرسال بدون Markdown لتجنب أخطاء التنسيق
    _send_plain(
        f"📊 تحليل الشارت البصري\n\n{analysis}", cid
    )
    log.info("✅ تحليل الشارت البصري أُرسل.")


def _download_telegram_photo(file_id: str) -> str | None:
    """يجلب الصورة من Telegram ويحولها لـ base64."""
    try:
        # جلب مسار الملف
        resp = requests.get(
            f"https://api.telegram.org/"
            f"bot{TELEGRAM_TOKEN}/getFile",
            params={"file_id": file_id},
            timeout=10,
        )
        if resp.status_code != 200:
            return None

        file_path = resp.json()["result"]["file_path"]

        # تحميل الصورة
        img_resp = requests.get(
            f"https://api.telegram.org/file/"
            f"bot{TELEGRAM_TOKEN}/{file_path}",
            timeout=15,
        )
        if img_resp.status_code != 200:
            return None

        import base64
        return base64.b64encode(img_resp.content).decode()

    except Exception as e:
        log.error(f"❌ فشل تحميل الصورة: {e}")
        return None


def _get_chart_context() -> str:
    """يجلب ملخصاً سريعاً من MT5 كسياق للتحليل البصري."""
    try:
        from mt5_handler import (
            ensure_mt5_connected,
            get_symbol_summary,
            analyze_dxy_trend,
            get_current_price,
            get_open_positions_summary,
        )
        import MetaTrader5 as mt5

        if not ensure_mt5_connected():
            return "MT5 غير متصل."

        dxy_text, _ = analyze_dxy_trend()
        gold_now    = get_current_price("XAUUSD")
        gold_h1     = get_symbol_summary(
            "XAUUSD", mt5.TIMEFRAME_H1, 50
        )

        if "error" in gold_h1:
            return f"السعر الحالي: {gold_now}"

        ind = gold_h1.get("indicators", {})

        # ── صفقات مفتوحة فعلياً — لتجنّب توصية متعارضة
        # بصمت دون تنبيه واضح للمستخدم
        positions = get_open_positions_summary("XAUUSD")
        if positions:
            pos_lines = ["⚠️ صفقات XAUUSD مفتوحة فعلياً الآن:"]
            for p in positions:
                pos_lines.append(
                    f"  #{p['ticket']} {p['direction']} "
                    f"@ {p['price_open']} "
                    f"(${p['profit']:+.2f})"
                )
            pos_lines.append(
                "  إذا كانت توصيتك معاكسة لهذه الصفقة/"
                "الصفقات، اذكر ذلك صراحةً في أول سطر "
                "من ردك ووضّح أن هذا يعني هيدج."
            )
            positions_block = "\n".join(pos_lines) + "\n"
        else:
            positions_block = ""

        return (
            f"{positions_block}"
            f"السعر الحالي: {gold_now}\n"
            f"الدولار: {dxy_text}\n"
            f"اتجاه H1: {gold_h1.get('trend')}\n"
            f"RSI: {ind.get('rsi_state')}\n"
            f"EMA: {ind.get('ema_trend')}\n"
            f"Stoch: {ind.get('stoch_signal')}\n"
            f"OB:\n{gold_h1.get('order_blocks')}\n"
            f"فيبوناتشي:\n{gold_h1.get('fibonacci')}"
        )
    except Exception as e:
        log.error(f"⚠️ خطأ في جلب السياق: {e}")
        return "تعذّر جلب بيانات MT5."


def _analyze_chart_with_gemini(
    image_b64: str,
    context:   str,
) -> str | None:
    """يُرسل الصورة + السياق لـ Gemini Vision ويُعيد التحليل."""
    try:
        from gemini_handler import get_gemini_client
        import base64

        client = get_gemini_client()

        prompt = f"""
أنت محلل SMC متخصص في الذهب XAUUSD.

البيانات الحالية من MT5:
{context}

المطلوب — حلّل الشارت بصرياً وأجب:
1. ما الاتجاه الواضح على الشارت؟
2. هل توجد مناطق OB أو FVG واضحة؟
3. أين مستويات فيبوناتشي الرئيسية؟
4. ما أفضل نقطة دخول ترى؟ مع SL وTP
5. هل التوصية تتوافق مع بيانات MT5 أعلاه؟

كن مختصراً ومباشراً — 5 نقاط فقط.
"""
        from google.genai import types

        response = client.models.generate_content(
            model    = "gemini-2.5-flash",
            contents = [
                types.Content(
                    role  = "user",
                    parts = [
                        types.Part(
                            inline_data = types.Blob(
                                mime_type = "image/png",
                                data      = image_b64,
                            )
                        ),
                        types.Part(text=prompt),
                    ],
                )
            ],
        )
        return response.text if response.text else None

    except Exception as e:
        log.error(f"❌ خطأ في Gemini Vision: {e}")
        return None


def _cmd_help(cid: str):
    send_to_telegram(
        "🤖 *أوامر البوت:*\n\n"
        "/analyze — تحليل فوري الآن\n"
        "/status  — حالة البوت والإحصائيات\n"
        "/winrate — نسبة الربح من صفقاتك الفعلية\n"
        "/trades  — آخر 5 صفقات مغلقة\n"
        "/alerts  — التنبيهات النشطة\n"
        "/chart   — تحليل صورة الشارت بصرياً 🆕\n"
        "/help    — هذه القائمة",
        cid,
    )