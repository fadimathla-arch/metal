"""
database.py — قاعدة البيانات
════════════════════════════════
كل عمليات SQLite في مكان واحد — Thread-Safe.
"""

import sqlite3
import threading
from datetime import datetime

from config import DB_FILE
from logger import log

_db_lock = threading.Lock()


# ╔══════════════════════════════════════════╗
# ║  دوال التنفيذ الأساسية                   ║
# ╚══════════════════════════════════════════╝

def db_execute(query: str, params: tuple = ()):
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=10.0)
        try:
            conn.execute(query, params)
            conn.commit()
        finally:
            conn.close()


def db_fetchall(query: str, params: tuple = ()) -> list:
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=10.0)
        try:
            return conn.execute(query, params).fetchall()
        finally:
            conn.close()


def db_fetchone(query: str, params: tuple = ()):
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=10.0)
        try:
            return conn.execute(query, params).fetchone()
        finally:
            conn.close()


# ╔══════════════════════════════════════════╗
# ║  تهيئة الجداول                           ║
# ╚══════════════════════════════════════════╝

def init_db():
    with _db_lock:
        conn = sqlite3.connect(DB_FILE, timeout=10.0)
        c    = conn.cursor()

        # جدول الإشارات الرئيسي
        c.execute("""
            CREATE TABLE IF NOT EXISTS signals (
                id                INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp         TEXT,
                symbol            TEXT,
                direction         TEXT,
                entry             REAL,
                tp1               REAL,
                tp2               REAL,
                sl                REAL,
                rr_ratio          REAL DEFAULT 0,
                status            TEXT DEFAULT 'PENDING',
                result_pips       REAL DEFAULT 0,
                result_profit     REAL DEFAULT 0,
                entry_filled_time TEXT,
                notes             TEXT
            )
        """)

        # جدول التحليلات
        c.execute("""
            CREATE TABLE IF NOT EXISTS analyses (
                id           INTEGER PRIMARY KEY AUTOINCREMENT,
                timestamp    TEXT,
                content      TEXT,
                dxy          TEXT,
                gold_price   REAL,
                silver_price REAL
            )
        """)

        # جدول تنبيهات الأسعار
        c.execute("""
            CREATE TABLE IF NOT EXISTS price_alerts (
                id        INTEGER PRIMARY KEY AUTOINCREMENT,
                symbol    TEXT,
                price     REAL,
                direction TEXT,
                message   TEXT,
                triggered INTEGER DEFAULT 0,
                created   TEXT
            )
        """)

        # إضافة أعمدة جديدة بأمان للقواعد القديمة
        _safe_add_columns(c, "signals", [
            ("entry_filled_time", "TEXT"),
            ("result_profit",     "REAL DEFAULT 0"),
            ("rr_ratio",          "REAL DEFAULT 0"),
        ])

        conn.commit()
        conn.close()
    log.info("✅ قاعدة البيانات جاهزة.")


def _safe_add_columns(cursor, table: str, columns: list):
    """يضيف أعمدة جديدة بأمان — يتجاهل الخطأ لو موجودة."""
    for col_name, col_def in columns:
        try:
            cursor.execute(
                f"ALTER TABLE {table} "
                f"ADD COLUMN {col_name} {col_def}"
            )
        except sqlite3.OperationalError:
            pass


# ╔══════════════════════════════════════════╗
# ║  عمليات الإشارات                         ║
# ╚══════════════════════════════════════════╝

def save_signal(
    symbol:    str,
    direction: str,
    entry:     float,
    tp1:       float,
    tp2:       float,
    sl:        float,
    rr_ratio:  float = 0.0,
) -> bool:
    """
    يحفظ الإشارة بعد التحقق من صحة الأرقام.
    يُعيد True لو نجح الحفظ.
    """
    if entry <= 0 or sl <= 0 or tp1 <= 0:
        log.warning(
            f"⚠️ أرقام غير منطقية — تجاهل "
            f"({symbol} entry={entry})"
        )
        return False

    db_execute("""
        INSERT INTO signals
            (timestamp, symbol, direction,
             entry, tp1, tp2, sl,
             rr_ratio, status)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?, 'PENDING')
    """, (
        datetime.now().isoformat(),
        symbol, direction,
        entry, tp1, tp2, sl,
        round(rr_ratio, 2),
    ))
    log.info(
        f"💾 إشارة محفوظة (PENDING): "
        f"{direction} {symbol} @ {entry} "
        f"| RR: {rr_ratio:.2f}"
    )
    return True


def get_win_rate() -> dict:
    """
    إحصائيات الصفقات الفعلية المغلقة فقط.
    لا تشمل PENDING أو OPEN.
    """
    row = db_fetchone("""
        SELECT
            COUNT(*),
            SUM(CASE WHEN status IN ('TP1','TP2')
                THEN 1 ELSE 0 END),
            SUM(CASE WHEN status = 'SL'
                THEN 1 ELSE 0 END),
            AVG(CASE WHEN result_pips != 0
                THEN result_pips END),
            SUM(result_profit),
            AVG(rr_ratio)
        FROM signals
        WHERE status NOT IN ('OPEN', 'PENDING')
    """)

    total   = row[0] or 0
    wins    = row[1] or 0
    losses  = row[2] or 0
    avg_pip = round(row[3] or 0, 1)
    profit  = round(row[4] or 0, 2)
    avg_rr  = round(row[5] or 0, 2)
    rate    = round(
        (wins / total * 100) if total > 0 else 0, 1
    )

    return {
        "total":        total,
        "wins":         wins,
        "losses":       losses,
        "avg_pips":     avg_pip,
        "win_rate":     rate,
        "total_profit": profit,
        "avg_rr":       avg_rr,
    }


def get_recent_trades(limit: int = 5) -> list:
    """يُعيد آخر N صفقة مغلقة."""
    return db_fetchall("""
        SELECT symbol, direction, entry,
               status, result_pips, result_profit,
               rr_ratio, entry_filled_time
        FROM signals
        WHERE status NOT IN ('OPEN', 'PENDING')
        ORDER BY id DESC
        LIMIT ?
    """, (limit,))


def get_open_signals_count() -> int:
    row = db_fetchone(
        "SELECT COUNT(*) FROM signals WHERE status='OPEN'"
    )
    return row[0] if row else 0


def get_pending_signals(symbol: str, direction: str) -> list:
    """إشارات PENDING لرمز واتجاه محددين."""
    return db_fetchall("""
        SELECT id, entry, tp1, tp2, sl
        FROM signals
        WHERE symbol    = ?
          AND direction = ?
          AND status    = 'PENDING'
        ORDER BY timestamp DESC
        LIMIT 5
    """, (symbol, direction))


# ╔══════════════════════════════════════════╗
# ║  عمليات التحليلات                        ║
# ╚══════════════════════════════════════════╝

def save_analysis(
    content:      str,
    dxy:          str,
    gold_price:   float,
    silver_price: float,
):
    db_execute("""
        INSERT INTO analyses
            (timestamp, content, dxy,
             gold_price, silver_price)
        VALUES (?, ?, ?, ?, ?)
    """, (
        datetime.now().isoformat(),
        content, dxy,
        gold_price, silver_price,
    ))


# ╔══════════════════════════════════════════╗
# ║  عمليات التنبيهات                        ║
# ╚══════════════════════════════════════════╝

def get_active_alerts() -> list:
    return db_fetchall("""
        SELECT id, symbol, price, direction, message
        FROM price_alerts
        WHERE triggered = 0
        ORDER BY symbol
    """)


def trigger_alert(alert_id: int):
    db_execute("""
        UPDATE price_alerts
        SET triggered = 1
        WHERE id = ?
    """, (alert_id,))