"""
config.py — الإعدادات المركزية
════════════════════════════════
كل القيم الثابتة والمفاتيح في مكان واحد.
لتغيير أي إعداد، عدّل هنا فقط.
"""

import os
from dotenv import load_dotenv

load_dotenv()

# ── Gemini ───────────────────────────────────
GEMINI_KEYS_POOL: list[str] = [
    k for k in [
        os.getenv("GEMINI_KEY_1"),
        os.getenv("GEMINI_KEY_2"),
        os.getenv("GEMINI_KEY_3"),  # جاهز للمفتاح الثالث
    ] if k
]

GEMINI_MODEL = "gemini-2.5-flash"

# ── Telegram ─────────────────────────────────
TELEGRAM_TOKEN   = os.getenv("TELEGRAM_TOKEN",   "")
TELEGRAM_CHAT_ID = os.getenv("TELEGRAM_CHAT_ID", "")

# ── ملفات النظام ─────────────────────────────
MEMORY_FILE    = "last_analysis.txt"
HISTORY_FOLDER = "analysis_history"
DB_FILE        = "metals_bot.db"
LOG_FILE       = "bot.log"

# ── توقيت التحليل ────────────────────────────
INTERVAL_MINUTES = 60   # كل ساعة على XX:00
DAILY_REPORT_HOUR = 6   # التقرير اليومي الساعة 6 صباحاً

# ── فلاتر الإشارات ───────────────────────────
# الحد الأدنى لمسافة SL (بنفس وحدة سعر MT5)
MIN_SL_DISTANCE = {
    "XAUUSD": 35.0,
    "XAGUSD": 0.25,
}

# الحد الأقصى لمسافة SL — يمنع SL البعيد غير المنطقي
# SL يجب أن يكون تحت/فوق OB مباشرة وليس من أدنى/أعلى الفترة
MAX_SL_DISTANCE = {
    "XAUUSD": 80.0,   # ذهب — أكثر من 80 نقطة غير منطقي
    "XAGUSD": 1.5,    # فضة — أكثر من 1.5 غير منطقي
}

# الحد الأدنى لنسبة Risk/Reward بناءً على TP1
MIN_RR_RATIO = 1.5

# ── رموز المتابعة ─────────────────────────────
SYMBOLS = ["XAUUSD", "XAGUSD"]

# رمز مؤشر الدولار (يُجرب بالترتيب)
DXY_SYMBOLS = ["USDIDX", "DXY", "USDX"]

# فلتر MT5 للمعادن (لـ positions_get)
MT5_GROUP_FILTER = "*XAU*,*XAG*"

# ── إعدادات المؤشرات ─────────────────────────
EMA_PERIODS   = [20, 50, 200]
RSI_PERIOD    = 14
ATR_PERIOD    = 14
BB_PERIOD     = 20     # Bollinger Bands
BB_STD        = 2.0
STOCH_K       = 14     # Stochastic
STOCH_D       = 3
STOCH_SMOOTH  = 3
VWAP_ENABLED  = True

# عدد الشموع المحمّلة لكل إطار زمني
BARS_H4  = 50
BARS_H1  = 100
BARS_M15 = 60
BARS_FVG = 35          # لفحص الفجوات
BARS_OB  = 100         # لكشف Order Blocks

# حد OB: كم شمعة نبحث فيها للخلف
OB_LOOKBACK = 15

# ── ATR كمرجع للربط بإشارات Gemini ───────────
# نقبل الصفقة إذا كان فارق السعر عن Entry < ATR × هذا الرقم
ATR_MATCH_MULTIPLIER = 2.0

# ── نافذة البحث عن صفقات مغلقة ───────────────
CLOSED_DEALS_LOOKBACK_HOURS = 2

# ── إعدادات السجلات ──────────────────────────
LOG_MAX_BYTES    = 5 * 1024 * 1024   # 5 MB
LOG_BACKUP_COUNT = 3