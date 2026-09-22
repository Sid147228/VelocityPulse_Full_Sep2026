# Contracts – JMeter Test Results Accelerator

This document defines the stable contracts of the system.  
Any change must be logged in CHANGELOG.md with a Bug ID (B-###) or Feature ID (F-###).

---

## Routes

| Path                             | Method | Endpoint         | Description                                  |
|----------------------------------|--------|------------------|----------------------------------------------|
| `/analyze`                       | POST   | `analyze`        | Processes uploaded JMeter CSV and saves report |
| `/report/<int:report_index>`     | GET    | `report`         | Renders a saved report by index              |
| `/report/latest`                | GET    | `report_latest`  | Redirects to the most recent report          |
| `/history`                      | GET    | `history`        | Paginated list of saved reports              |
| `/export_report_pdf/<int:report_index>` | GET | `export_report_pdf` | Exports saved report to PDF         |
| `/export_session_report_pdf`    | GET    | `export_session_report_pdf` | Exports current session report to PDF |

---

## Template Contexts

### `report.html`

- `report_name` (str)
- `summary` (list of dicts)
- `selected_metrics` (list of str)
- `rag_result` (str: GREEN/AMBER/RED)
- `test_date` (str)
- `test_period` (str)
- `total_duration` (str)
- `steady_state` (str)
- `concurrent_users` (int or None)
- `green` (float)
- `amber` (float)
- `error_threshold` (float or None)
- `report_index` (int)
- `time_labels` (list of str)
- `txn_datasets` (list of dicts)
- `is_pdf` (bool)

### `history.html`

- `reports` (list of report dicts for current page)
- `page` (int)
- `total_pages` (int)
- `offset` (int)

---

## Report Schema (`history.json`)

```json
{
  "name": "Report Name",
  "summary": [ { "Transaction": "...", "Avg (s)": 1.23, "RAG": "GREEN" } ],
  "selected_metrics": ["avg", "p90", "error"],
  "rag_basis": "avg",
  "green": 1.0,
  "amber": 2.0,
  "error_threshold": 5.0,
  "start_time": "1695657600000",
  "end_time": "1695661200000",
  "rag_result": "AMBER",
  "test_date": "25-09-2025",
  "test_period": "25-09-2025 12:00:00 to 25-09-2025 13:00:00",
  "total_duration": "1 hr",
  "steady_state": "12:15:00 — 12:45:00",
  "concurrent_users": 20,
  "timestamp": "2025-09-25 13:00:00",
  "source_file": "uploads/test.csv"
}

"baseline": {
  "avg": float,
  "p90": float,
  "sample_size": int,
  "last_updated": str
},
"sla": {
  "avg_green": float,
  "avg_amber": float,
  "p90_green": float,
  "p90_amber": float
}

