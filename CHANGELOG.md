# CHANGELOG.md

## Unreleased

- Feature: Advanced level detection (major/recent highs & lows, support/resistance) via `advanced_levels.py`.
- Feature: Order Block detection and OB-aware SL validation.
- Feature: Programmatic signal suggestions based on detected levels (analysis_engine).
- Improvement: Gemini signal extraction validates SL vs OB and uses liquidity context.
- Improvement: Confidence engine computes liquidity_score + Signal Quality Score (0-100).
- Tests: Added unit test for `detect_levels`.

---
