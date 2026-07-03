# metal

هذا المستودع يحتوي على بوت تحليلي للمعادن (XAUUSD, XAGUSD) يعتمد على SMC وGemini لتحليل السوق وإصدار إشارات.

## الميزات المضافة في الفرع feature/level-detection

- كشف مستويات متقدّم عبر `advanced_levels.py`:
  - major_low / recent_low / major_high / recent_high
  - support / resistance
  - order_block_buy / order_block_sell

- سياق السيولة (liquidity) مع حقول:
  - liquidity_score
  - near_major_low / near_major_high
  - bullish_divergence / bearish_divergence
  - distance_atr

- تحسين `mt5_handler.get_symbol_summary()` ليرجع الحقول الجديدة ضمن `levels` و`liquidity`.

- محرك الثقة `confidence_engine.py` يحسب قرار متدرّج (ALLOW / WARN / REJECT) ويُرجع `liquidity_score` وسبب القرار.

- `analysis_engine.py` يُنتج اقتراحات برمجية (BUY/SELL أو SIGNAL: WAIT) بناءً على المستويات، ويجمع ��قتراحات النموذج (Gemini) مع الاقتراح البرمجي.

- `gemini_handler.py` صار يتحقق من توافق SL مع الـ Order Block (OB) ويستخدم سياق السيولة عند تقييم الإشارات.

## كيف تختبر محلياً

1. جلب الفرع:  
   git fetch && git checkout feature/level-detection

2. تثبيت المتطلبات:  
   pip install -r requirements.txt

3. تشغيل اختبارات الوحدة:  
   pytest -q tests/test_advanced_levels.py

4. تجربة ملخّص رمز (يتطلب MT5 متصلاً):
   - افتح REPL أو سكربت:
     ```py
     from metals_bot.mt5_handler import get_symbol_summary
     import MetaTrader5 as mt5
     print(get_symbol_summary("XAUUSD", mt5.TIMEFRAME_H1, 200))
     ```

> ملاحظات:
> - بعض الوظائف تعتمد على وجود مكتبة `MetaTrader5` ووجود بيانات `tick_volume` لاحتساب VWAP والحجم بدقّة.
> - لتشغيل جزء Gemini من التحليل تحتاج مفاتيح صالحة في المتغيرات البيئية (GEMINI_KEYS_POOL).

## CI
ملف عمل GitHub Actions أضيف في `.github/workflows/ci.yml` ليشغّل الاختبارات تلقائياً عند فتح PR.

## CHANGELOG
راجع `CHANGELOG.md` في جذور المشروع للوصف التفصيلي للتغييرات.

