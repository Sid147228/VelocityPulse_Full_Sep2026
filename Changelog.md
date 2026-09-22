
---

## 📘 `CHANGELOG.md`

```markdown
# Changelog

## [Unreleased]

### 🐞 Bug Fixes

- B-101: Fixed report.html mixing multiple reports by removing session fallback.
- B-102: Added missing `/report/latest` route to support header navigation.
- B-103: Corrected `url_for('report', report_index=...)` usage in analyze and history pages.
- B-104: Prevented silent data loss by introducing persistent `history.json` storage.

### ✨ Features

- F-201: Introduced persistent report saving via `save_report()` and `load_history()`.
- F-202: Added `/report/latest` route to always redirect to newest report.
- F-203: Implemented paginated history view with `per_page=5`.
- F-204: Added PDF export routes for saved and session reports.
- F-205: Introduced `contracts.md` and `CHANGELOG.md` for stability tracking.
- F-301: Introduced baselining of transaction response times (Avg, 90p).
- F-302: Automatic SLA derivation from baselines with configurable multipliers.

