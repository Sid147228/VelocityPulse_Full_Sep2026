from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response, jsonify
import os, json, uuid, subprocess, threading, time, csv, secrets, sqlite3, re
from urllib.parse import urlparse

import pandas as pd
from dotenv import load_dotenv
from datetime import datetime
from werkzeug.utils import secure_filename
from werkzeug.security import generate_password_hash, check_password_hash
from version import __version__, __build__, __codename__

load_dotenv()

# Helpers
from jmeter_parser import (
    parse_jmeter_csv,
    detect_test_window,
    jmeter_percentile,
)
from jmeter_compat import build_report_chart_data
from generate_TestResult import evaluate_sla
from generate_graphs import generate_graphs
from generate_transaction_progress import generate_transaction_progress
from generate_rag_pie import generate_rag_pie
from pdf_export import build_single_report_pdf, build_compare_report_pdf

from flask_socketio import SocketIO
from collections import defaultdict, deque
import numpy as np

# Flask app and Socket.IO
app = Flask(__name__)
app.secret_key = os.getenv("FLASK_SECRET_KEY") or secrets.token_hex(32)
app.config.update(
    SESSION_COOKIE_HTTPONLY=True,
    SESSION_COOKIE_SAMESITE="Lax",
    SESSION_COOKIE_SECURE=os.getenv("SESSION_COOKIE_SECURE", "false").lower() == "true",
)
socketio = SocketIO(app)

UPLOAD_FOLDER = "uploads"
HISTORY_FILE = "static/reports/history.json"
BASELINE_FILE = "static/reports/baselines.json"
PROJECTS_FILE = "static/reports/projects.json"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("static/reports", exist_ok=True)  # ensure reports dir exists


def load_projects():
    if not os.path.exists(PROJECTS_FILE):
        return []
    try:
        with open(PROJECTS_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, list) else []
    except Exception:
        app.logger.exception("Unable to load projects")
        return []


def save_projects(projects):
    os.makedirs(os.path.dirname(PROJECTS_FILE), exist_ok=True)
    with open(PROJECTS_FILE, "w", encoding="utf-8") as handle:
        json.dump(projects, handle, indent=2)


def _current_user_id():
    profile = session.get("user_profile") or {}
    try:
        return int(profile.get("id"))
    except (TypeError, ValueError):
        return None


def projects_for_user(user_id=None):
    user_id = _current_user_id() if user_id is None else user_id
    if user_id is None:
        return []
    return [
        project
        for project in load_projects()
        if str(project.get("owner_user_id")) == str(user_id)
    ]


def find_user_project(project_id, user_id=None):
    if not project_id:
        return None
    requested = str(project_id)
    return next(
        (
            project
            for project in projects_for_user(user_id)
            if str(project.get("id")) == requested
        ),
        None,
    )


def active_project():
    return find_user_project(session.get("active_project_id"))


def _migrate_legacy_records_to_project(project):
    """Assign pre-project reports/baselines to the first project without losing data."""
    project_id = str(project.get("id"))
    project_name = str(project.get("name") or "")

    for file_path in (HISTORY_FILE, BASELINE_FILE):
        if not os.path.exists(file_path):
            continue
        try:
            with open(file_path, "r", encoding="utf-8") as handle:
                records = json.load(handle)
            if not isinstance(records, list):
                continue
            changed = False
            for record in records:
                if not isinstance(record, dict) or record.get("project_id"):
                    continue
                record["project_id"] = project_id
                record["project_name"] = project_name
                changed = True
            if changed:
                with open(file_path, "w", encoding="utf-8") as handle:
                    json.dump(records, handle, indent=2)
        except Exception:
            app.logger.exception("Unable to migrate legacy records in %s", file_path)


def create_project(name, user_id, migrate_legacy=False):
    clean_name = re.sub(r"\s+", " ", str(name or "").strip())
    if len(clean_name) < 2 or len(clean_name) > 80:
        raise ValueError("Project name must contain between 2 and 80 characters.")

    user_projects = projects_for_user(user_id)
    if any(str(item.get("name") or "").casefold() == clean_name.casefold() for item in user_projects):
        raise ValueError("A project with that name already exists.")

    project = {
        "id": uuid.uuid4().hex[:12],
        "name": clean_name,
        "owner_user_id": int(user_id),
        "created_at": datetime.utcnow().isoformat(),
    }
    projects = load_projects()
    projects.append(project)
    save_projects(projects)

    if migrate_legacy:
        _migrate_legacy_records_to_project(project)

    return project


def set_active_project(project):
    previous_project_id = str(session.get("active_project_id") or "")
    next_project_id = str((project or {}).get("id") or "")

    if previous_project_id != next_project_id:
        # Never carry an in-progress upload/analysis from one application into another.
        for key in (
            "uploaded_file",
            "uploaded_file_path",
            "test_window",
            "summary",
        ):
            session.pop(key, None)

    if not project:
        session.pop("active_project_id", None)
        session.pop("active_project_name", None)
        return

    session["active_project_id"] = next_project_id
    session["active_project_name"] = str(project.get("name") or "")


def _format_test_timestamp(timestamp_ms):
    """Format a JMeter epoch-millisecond timestamp for display and datetime-local input."""
    dt = datetime.fromtimestamp(int(timestamp_ms) / 1000.0)
    return {
        "display": dt.strftime("%d-%m-%Y %H:%M:%S"),
        "input": dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3],
    }


def _parse_datetime_local(value):
    """Convert a browser datetime-local value back to epoch milliseconds."""
    if not value:
        return None

    for fmt in (
        "%Y-%m-%dT%H:%M:%S.%f",
        "%Y-%m-%dT%H:%M:%S",
        "%Y-%m-%dT%H:%M",
    ):
        try:
            return int(datetime.strptime(value, fmt).timestamp() * 1000)
        except ValueError:
            continue
    return None


# Local PIN authentication configuration.
AUTH_DB_PATH = os.getenv(
    "PIN_AUTH_DB_PATH",
    os.path.join("instance", "velocitypulse_auth.db"),
).strip()
PIN_LENGTH = 6
PIN_MAX_FAILED_ATTEMPTS = max(1, int(os.getenv("PIN_MAX_FAILED_ATTEMPTS", "5")))
PIN_LOCKOUT_MINUTES = max(1, int(os.getenv("PIN_LOCKOUT_MINUTES", "15")))
PIN_SESSION_MINUTES = max(5, int(os.getenv("PIN_SESSION_MINUTES", "480")))
PIN_PEPPER = os.getenv("PIN_PEPPER", "").strip()
PIN_REGISTRATION_CODE = os.getenv("PIN_REGISTRATION_CODE", "").strip()
PIN_ALLOWED_EMAIL_DOMAINS = {
    domain.strip().lower().lstrip("@")
    for domain in os.getenv("PIN_ALLOWED_EMAIL_DOMAINS", "").split(",")
    if domain.strip()
}
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def _auth_db_connection():
    directory = os.path.dirname(AUTH_DB_PATH)
    if directory:
        os.makedirs(directory, exist_ok=True)
    connection = sqlite3.connect(AUTH_DB_PATH, timeout=10)
    connection.row_factory = sqlite3.Row
    return connection


def init_auth_db():
    with _auth_db_connection() as connection:
        connection.execute(
            """
            CREATE TABLE IF NOT EXISTS users (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                email TEXT NOT NULL UNIQUE COLLATE NOCASE,
                display_name TEXT NOT NULL,
                pin_hash TEXT NOT NULL,
                created_at INTEGER NOT NULL,
                last_login_at INTEGER,
                failed_attempts INTEGER NOT NULL DEFAULT 0,
                locked_until INTEGER NOT NULL DEFAULT 0,
                is_active INTEGER NOT NULL DEFAULT 1
            )
            """
        )


def _normalise_email(value):
    return str(value or "").strip().lower()


def _email_allowed(email):
    if not EMAIL_PATTERN.fullmatch(email):
        return False
    if not PIN_ALLOWED_EMAIL_DOMAINS:
        return True
    return email.rsplit("@", 1)[-1] in PIN_ALLOWED_EMAIL_DOMAINS


def _valid_pin(pin):
    return bool(re.fullmatch(rf"\d{{{PIN_LENGTH}}}", str(pin or "")))


def _pin_secret(pin):
    return f"{pin}{PIN_PEPPER}" if PIN_PEPPER else str(pin)


def _pin_hash(pin):
    return generate_password_hash(_pin_secret(pin), method="scrypt")


def _pin_matches(pin_hash, pin):
    return check_password_hash(pin_hash, _pin_secret(pin))


def _load_auth_user(email):
    with _auth_db_connection() as connection:
        return connection.execute(
            """
            SELECT id, email, display_name, pin_hash, failed_attempts,
                   locked_until, is_active
            FROM users
            WHERE email = ?
            """,
            (email,),
        ).fetchone()


def _render_register(display_name="", email="", project_name="", status=200):
    return (
        render_template(
            "register.html",
            display_name=display_name,
            email=email,
            project_name=project_name,
            registration_code_required=bool(PIN_REGISTRATION_CODE),
            allowed_domains=sorted(PIN_ALLOWED_EMAIL_DOMAINS),
        ),
        status,
    )


def _record_failed_login(user_id, current_attempts):
    attempts = int(current_attempts or 0) + 1
    locked_until = 0
    if attempts >= PIN_MAX_FAILED_ATTEMPTS:
        locked_until = int(time.time()) + (PIN_LOCKOUT_MINUTES * 60)
        attempts = 0

    with _auth_db_connection() as connection:
        connection.execute(
            """
            UPDATE users
            SET failed_attempts = ?, locked_until = ?
            WHERE id = ?
            """,
            (attempts, locked_until, user_id),
        )
    return locked_until


def _record_successful_login(user_id):
    with _auth_db_connection() as connection:
        connection.execute(
            """
            UPDATE users
            SET failed_attempts = 0, locked_until = 0, last_login_at = ?
            WHERE id = ?
            """,
            (int(time.time()), user_id),
        )


def _establish_pin_session(user):
    destination = session.pop("post_login_redirect", None)
    session.clear()
    now = int(time.time())
    session["user"] = user["display_name"]
    session["user_profile"] = {
        "id": user["id"],
        "name": user["display_name"],
        "email": user["email"],
        "auth_type": "pin",
    }
    session["authenticated_at"] = now
    session["last_activity_at"] = now

    if is_safe_local_redirect(destination):
        session["post_project_redirect"] = destination
    return url_for("project_select")


def is_safe_local_redirect(target):
    if not target:
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc and target.startswith("/")


init_auth_db()


@app.before_request
def require_pin_authentication():
    """Require a valid local PIN session for all application routes."""
    public_endpoints = {"login", "register", "logout", "static"}
    if request.endpoint in public_endpoints or request.path.startswith("/socket.io/"):
        return None

    profile = session.get("user_profile")
    last_activity = int(session.get("last_activity_at") or 0)
    now = int(time.time())
    expired = (
        not profile
        or not last_activity
        or now - last_activity > PIN_SESSION_MINUTES * 60
    )

    if expired:
        destination = None
        if request.method == "GET" and is_safe_local_redirect(request.full_path.rstrip("?")):
            destination = request.full_path.rstrip("?")
        session.clear()
        if destination:
            session["post_login_redirect"] = destination
        flash("Your session expired. Enter your PIN to continue.", "info")
        return redirect(url_for("login"))

    session["last_activity_at"] = now

    project_exempt_endpoints = {
        "project_select",
        "project_create",
        "logout",
        "about",
        "static",
    }
    if request.endpoint not in project_exempt_endpoints and not active_project():
        if request.method == "GET" and is_safe_local_redirect(request.full_path.rstrip("?")):
            session["post_project_redirect"] = request.full_path.rstrip("?")
        flash("Select the application/project you want to work with.", "info")
        return redirect(url_for("project_select"))

    return None


@socketio.on("connect")
def authenticated_socket_connection(auth=None):
    profile = session.get("user_profile")
    last_activity = int(session.get("last_activity_at") or 0)
    if not profile or not last_activity:
        return False
    if int(time.time()) - last_activity > PIN_SESSION_MINUTES * 60:
        return False
    session["last_activity_at"] = int(time.time())


# Inject version info into templates
@app.context_processor
def inject_version():
    return {
        "app_version": __version__,
        "build": __build__,
        "codename": __codename__,
        "active_project": active_project(),
        "available_projects": projects_for_user(),
    }

# History helpers
def load_history(project_id=None, include_all=False):
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            history = json.load(f)
    except Exception:
        return []

    if not isinstance(history, list):
        return []
    if include_all:
        return history

    project_id = str(project_id or session.get("active_project_id") or "")
    if not project_id:
        return []
    return [
        report
        for report in history
        if str((report or {}).get("project_id") or "") == project_id
    ]


def save_report(report_data):
    report_data = dict(report_data)
    explicit_project_id = str(report_data.get("project_id") or "").strip()
    if explicit_project_id:
        project = find_user_project(explicit_project_id)
        if not project:
            raise ValueError("The report project does not belong to the signed-in user.")
    else:
        project = active_project()

    if not project:
        raise ValueError("An active project is required before saving a report.")

    report_data["project_id"] = str(project.get("id"))
    report_data["project_name"] = str(project.get("name") or "")

    history = load_history(include_all=True)
    history.insert(0, report_data)  # newest first
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def overall_rag(summary):
    if any(row.get("RAG") == "RED" for row in summary):
        return "RED"
    if any(row.get("RAG") == "AMBER" for row in summary):
        return "AMBER"
    return "GREEN" if summary else "UNKNOWN"


def enrich_summary_and_kpis(df, summary):
    """Add dashboard details while preserving JMeter-compatible response-time calculations."""
    frame = df.copy()
    frame.columns = [str(column).strip().lower() for column in frame.columns]

    required = {"timestamp", "elapsed", "label"}
    if not required.issubset(frame.columns):
        return summary, {
            "total_samples": sum(int(_metric(row, "#Samples")) for row in summary),
            "successful_samples": None,
            "failed_samples": None,
            "error_pct": None,
            "avg_s": None,
            "p90_s": None,
            "p95_s": None,
            "throughput_tps": None,
        }

    frame["timestamp"] = pd.to_numeric(frame["timestamp"], errors="coerce")
    frame["elapsed"] = pd.to_numeric(frame["elapsed"], errors="coerce")
    frame["label"] = frame["label"].astype(str).str.strip()
    if "success" in frame.columns:
        frame["success"] = (
            frame["success"].astype(str).str.strip().str.lower().isin(["true", "1"])
        )
    else:
        frame["success"] = True

    frame = frame.dropna(subset=["timestamp", "elapsed"]).copy()
    frame = frame[frame["label"].ne("") & frame["label"].ne("nan")].copy()
    if frame.empty:
        return summary, {
            "total_samples": 0,
            "successful_samples": 0,
            "failed_samples": 0,
            "error_pct": 0.0,
            "avg_s": None,
            "p90_s": None,
            "p95_s": None,
            "throughput_tps": None,
        }

    frame["end_timestamp"] = frame["timestamp"] + frame["elapsed"]

    by_label = {label: group for label, group in frame.groupby("label")}
    for row in summary:
        label = str(row.get("Transaction") or "")
        group = by_label.get(label)
        if group is None or group.empty:
            row.setdefault("Min (s)", None)
            row.setdefault("Max (s)", None)
            row.setdefault("Throughput (TPS)", None)
            continue

        row["Min (s)"] = float(group["elapsed"].min()) / 1000.0
        row["Max (s)"] = float(group["elapsed"].max()) / 1000.0
        duration_ms = float(group["end_timestamp"].max() - group["timestamp"].min())
        row["Throughput (TPS)"] = (
            float(len(group)) * 1000.0 / duration_ms
            if duration_ms > 0
            else 0.0
        )

    total_samples = int(len(frame))
    successful_samples = int(frame["success"].sum())
    failed_samples = total_samples - successful_samples
    elapsed_values = frame["elapsed"].tolist()
    total_duration_ms = float(frame["end_timestamp"].max() - frame["timestamp"].min())

    kpis = {
        "total_samples": total_samples,
        "successful_samples": successful_samples,
        "failed_samples": failed_samples,
        "error_pct": (100.0 * failed_samples / total_samples) if total_samples else 0.0,
        "avg_s": float(frame["elapsed"].mean()) / 1000.0,
        "p90_s": jmeter_percentile(elapsed_values, 0.90) / 1000.0,
        "p95_s": jmeter_percentile(elapsed_values, 0.95) / 1000.0,
        "throughput_tps": (
            total_samples * 1000.0 / total_duration_ms
            if total_duration_ms > 0
            else 0.0
        ),
    }
    return summary, kpis


def report_overview(report):
    """Return comparison-friendly KPIs, with safe fallbacks for older saved reports."""
    kpis = dict(report.get("report_kpis") or {})
    summary = report.get("summary") or []

    if kpis.get("total_samples") is None:
        kpis["total_samples"] = sum(int(_metric(row, "#Samples")) for row in summary)

    if kpis.get("avg_s") is None and kpis["total_samples"]:
        weighted = sum(
            _metric(row, "Avg (s)") * int(_metric(row, "#Samples"))
            for row in summary
        )
        kpis["avg_s"] = weighted / kpis["total_samples"]

    if kpis.get("failed_samples") is None:
        estimated_failed = sum(
            round(
                int(_metric(row, "#Samples"))
                * _metric(row, "Error %")
                / 100.0
            )
            for row in summary
        )
        kpis["failed_samples"] = int(estimated_failed)

    if kpis.get("successful_samples") is None:
        kpis["successful_samples"] = max(
            0,
            int(kpis.get("total_samples") or 0) - int(kpis.get("failed_samples") or 0),
        )

    if kpis.get("error_pct") is None and kpis.get("total_samples"):
        kpis["error_pct"] = (
            100.0
            * int(kpis.get("failed_samples") or 0)
            / int(kpis["total_samples"])
        )

    return kpis


def generate_report_graph_assets(df, summary, green_sla, amber_sla):
    report_graph_id = uuid.uuid4().hex[:12]
    relative_dir = f"reports/graphs/{report_graph_id}"
    absolute_dir = os.path.join("static", relative_dir)
    os.makedirs(absolute_dir, exist_ok=True)

    generate_graphs(
        df,
        green_sla=green_sla,
        amber_sla=amber_sla,
        graph_dir=absolute_dir,
    )
    generate_transaction_progress(
        df,
        out_file=os.path.join(absolute_dir, "transaction_progress.png"),
    )
    generate_rag_pie(
        summary,
        out_file=os.path.join(absolute_dir, "rag_pie.png"),
    )

    filenames = {
        "response_distribution": "response_distribution.png",
        "error_trend": "error_trend.png",
        "sla_heatmap": "sla_heatmap.png",
        "threads_over_time": "threads_over_time.png",
        "transaction_progress": "transaction_progress.png",
        "rag_pie": "rag_pie.png",
    }
    return {
        key: f"{relative_dir}/{filename}"
        for key, filename in filenames.items()
        if os.path.exists(os.path.join(absolute_dir, filename))
    }


def _metric(row, key):
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def _mean_valid(values):
    cleaned = []
    for value in values or []:
        if value is None:
            continue
        try:
            cleaned.append(float(value))
        except (TypeError, ValueError):
            continue
    return (sum(cleaned) / len(cleaned)) if cleaned else None


def _series_first_last_third(values):
    cleaned = []
    for value in values or []:
        if value is None:
            continue
        try:
            cleaned.append(float(value))
        except (TypeError, ValueError):
            continue
    if len(cleaned) < 3:
        return None, None
    width = max(1, len(cleaned) // 3)
    return _mean_valid(cleaned[:width]), _mean_valid(cleaned[-width:])


def _coefficient_of_variation(values):
    cleaned = []
    for value in values or []:
        if value is None:
            continue
        try:
            cleaned.append(float(value))
        except (TypeError, ValueError):
            continue
    if len(cleaned) < 2:
        return None
    mean = float(np.mean(cleaned))
    if mean == 0:
        return 0.0
    return float(np.std(cleaned, ddof=0) / abs(mean))


def _series_correlation(values1, values2):
    pairs = []
    for left, right in zip(values1 or [], values2 or []):
        if left is None or right is None:
            continue
        try:
            pairs.append((float(left), float(right)))
        except (TypeError, ValueError):
            continue
    if len(pairs) < 4:
        return None
    left = np.array([pair[0] for pair in pairs], dtype=float)
    right = np.array([pair[1] for pair in pairs], dtype=float)
    if np.std(left) == 0 or np.std(right) == 0:
        return None
    return float(np.corrcoef(left, right)[0, 1])


def build_report_observations(
    summary,
    rag_result,
    chart_data=None,
    report_kpis=None,
    green_sla=None,
    amber_sla=None,
    monitoring_stats=None,
    max_observations=12,
):
    """Create deterministic, evidence-based performance observations."""
    if not summary:
        return [{
            "type": "warning",
            "category": "Data quality",
            "priority": 100,
            "title": "No transaction data",
            "text": "No valid transaction samples were available for analysis.",
        }]

    chart_data = chart_data or {}
    report_kpis = report_kpis or {}
    findings = []

    def add(priority, obs_type, category, title, text):
        findings.append({
            "type": obs_type,
            "category": category,
            "priority": priority,
            "title": title,
            "text": text,
        })

    total_samples = int(
        report_kpis.get("total_samples")
        if report_kpis.get("total_samples") is not None
        else sum(int(_metric(row, "#Samples")) for row in summary)
    )
    failed_samples = report_kpis.get("failed_samples")
    if failed_samples is None:
        failed_samples = int(round(sum(
            int(_metric(row, "#Samples")) * _metric(row, "Error %") / 100.0
            for row in summary
        )))
    failed_samples = int(failed_samples)
    error_pct = (
        float(report_kpis.get("error_pct"))
        if report_kpis.get("error_pct") is not None
        else (100.0 * failed_samples / total_samples if total_samples else 0.0)
    )
    overall_avg = report_kpis.get("avg_s")
    overall_p95 = report_kpis.get("p95_s")

    status_type = {
        "GREEN": "success",
        "AMBER": "warning",
        "RED": "danger",
    }.get(rag_result, "info")

    verdict_text = (
        f"The steady-state analysis covered {len(summary)} transaction(s) and "
        f"{total_samples:,} sample(s), with {failed_samples:,} failure(s) "
        f"({error_pct:.2f}%)."
    )
    if overall_avg is not None:
        verdict_text += f" Overall average response time was {float(overall_avg):.3f}s."
    if overall_p95 is not None:
        verdict_text += f" P95 was {float(overall_p95):.3f}s."
    add(
        100,
        status_type,
        "Executive verdict",
        f"Overall test result: {rag_result or 'UNKNOWN'}",
        verdict_text,
    )

    affected = [
        row for row in summary
        if row.get("RAG") in ("RED", "AMBER")
    ]
    if affected:
        severity = {"RED": 0, "AMBER": 1}
        affected_sorted = sorted(
            affected,
            key=lambda row: (
                severity.get(row.get("RAG"), 2),
                -_metric(row, "95th % (s)"),
                -_metric(row, "Error %"),
            ),
        )
        names = ", ".join(
            f"{row.get('Transaction')} ({row.get('RAG')})"
            for row in affected_sorted[:4]
        )
        suffix = " and more" if len(affected_sorted) > 4 else ""
        add(
            95,
            "danger" if any(row.get("RAG") == "RED" for row in affected) else "warning",
            "Critical findings",
            "SLA attention required",
            f"{len(affected)} transaction(s) require attention: {names}{suffix}.",
        )
    else:
        add(
            50,
            "success",
            "Healthy behaviour",
            "All analysed transactions are within SLA",
            "No transaction breached the configured response-time thresholds during the analysed period.",
        )

    slowest = max(summary, key=lambda row: _metric(row, "Avg (s)"))
    slowest_avg = _metric(slowest, "Avg (s)")
    slowest_p95 = _metric(slowest, "95th % (s)")
    slowest_error = _metric(slowest, "Error %")
    add(
        90 if slowest.get("RAG") in ("RED", "AMBER") else 62,
        "danger" if slowest.get("RAG") == "RED" else ("warning" if slowest.get("RAG") == "AMBER" else "info"),
        "Critical findings" if slowest.get("RAG") in ("RED", "AMBER") else "Supporting findings",
        f"Primary latency bottleneck: {slowest.get('Transaction')}",
        (
            f"{slowest.get('Transaction')} recorded the highest average response time at "
            f"{slowest_avg:.3f}s, with P95 at {slowest_p95:.3f}s and "
            f"{slowest_error:.2f}% errors."
        ),
    )

    highest_error = max(summary, key=lambda row: _metric(row, "Error %"))
    highest_error_pct = _metric(highest_error, "Error %")
    if highest_error_pct > 0:
        add(
            92 if highest_error_pct >= 5 else 75,
            "danger" if highest_error_pct >= 5 else "warning",
            "Critical findings" if highest_error_pct >= 5 else "Supporting findings",
            f"Error hotspot: {highest_error.get('Transaction')}",
            (
                f"{highest_error.get('Transaction')} recorded the highest error rate at "
                f"{highest_error_pct:.2f}% across {int(_metric(highest_error, '#Samples'))} samples."
            ),
        )

    failure_rows = []
    for row in summary:
        samples = int(_metric(row, "#Samples"))
        estimated_failures = samples * _metric(row, "Error %") / 100.0
        if estimated_failures > 0:
            failure_rows.append((estimated_failures, row))
    failure_rows.sort(key=lambda item: item[0], reverse=True)
    if failed_samples > 0 and len(failure_rows) >= 2:
        top_two_failures = failure_rows[0][0] + failure_rows[1][0]
        concentration = 100.0 * top_two_failures / failed_samples
        if concentration >= 45:
            names = (
                f"{failure_rows[0][1].get('Transaction')} and "
                f"{failure_rows[1][1].get('Transaction')}"
            )
            add(
                84,
                "warning",
                "Supporting findings",
                "Failures are concentrated",
                (
                    f"Approximately {concentration:.0f}% of all failures are concentrated in "
                    f"{names}. Investigation should prioritise these transactions first."
                ),
            )

    tail_candidates = []
    for row in summary:
        avg = _metric(row, "Avg (s)")
        p95 = _metric(row, "95th % (s)")
        if avg > 0 and p95 > 0:
            tail_candidates.append((p95 / avg, row))
    if tail_candidates:
        tail_ratio, tail_row = max(tail_candidates, key=lambda item: item[0])
        if tail_ratio >= 1.5:
            add(
                76 if tail_ratio >= 2.0 else 68,
                "warning",
                "Supporting findings",
                f"Elevated tail latency: {tail_row.get('Transaction')}",
                (
                    f"P95 is {tail_ratio:.2f}× the average for {tail_row.get('Transaction')} "
                    f"({_metric(tail_row, 'Avg (s)'):.3f}s average vs "
                    f"{_metric(tail_row, '95th % (s)'):.3f}s P95), indicating a meaningful "
                    f"slow-response tail."
                ),
            )

    green_rows = [
        row for row in summary
        if row.get("RAG") == "GREEN"
    ]
    if green_rows:
        healthiest = min(
            green_rows,
            key=lambda row: (
                _metric(row, "Error %"),
                _metric(row, "95th % (s)"),
            ),
        )
        add(
            35,
            "success",
            "Healthy behaviour",
            f"Healthy transaction: {healthiest.get('Transaction')}",
            (
                f"{healthiest.get('Transaction')} remained comparatively healthy at "
                f"{_metric(healthiest, 'Avg (s)'):.3f}s average, "
                f"{_metric(healthiest, '95th % (s)'):.3f}s P95 and "
                f"{_metric(healthiest, 'Error %'):.2f}% errors."
            ),
        )

    avg_series = chart_data.get("series_avg_by_txn") or {}
    error_series = chart_data.get("series_error_rate_by_txn") or {}
    throughput_series = chart_data.get("series_throughput_over_time") or []

    degradation_candidates = []
    recovery_candidates = []
    spike_candidates = []

    for txn, values in avg_series.items():
        first_third, last_third = _series_first_last_third(values)
        if first_third and last_third:
            change_pct = 100.0 * (last_third - first_third) / first_third
            if change_pct >= 20.0:
                degradation_candidates.append((change_pct, txn, first_third, last_third))
            elif change_pct <= -20.0:
                recovery_candidates.append((abs(change_pct), txn, first_third, last_third))

        cleaned = [
            float(value) for value in values
            if value is not None
        ]
        if len(cleaned) >= 4:
            median = float(np.median(cleaned))
            peak = max(cleaned)
            if median > 0 and peak / median >= 1.5:
                peak_index = cleaned.index(peak)
                spike_candidates.append((peak / median, txn, peak, median, peak_index))

    degradation_candidates.sort(reverse=True)
    if degradation_candidates:
        change_pct, txn, first_value, last_value = degradation_candidates[0]
        add(
            88 if change_pct >= 35 else 78,
            "danger" if change_pct >= 35 else "warning",
            "Time-series findings",
            f"Progressive degradation detected: {txn}",
            (
                f"Average response time increased by {change_pct:.0f}% from the first third "
                f"to the final third of steady state "
                f"({first_value/1000.0:.3f}s to {last_value/1000.0:.3f}s)."
            ),
        )

    if spike_candidates:
        spike_candidates.sort(reverse=True)
        ratio, txn, peak, median, peak_index = spike_candidates[0]
        labels = chart_data.get("chart_time_labels") or []
        when = (
            f" around {labels[peak_index]}"
            if peak_index < len(labels)
            else ""
        )
        add(
            72,
            "warning",
            "Time-series findings",
            f"Latency spike detected: {txn}",
            (
                f"{txn} peaked at {peak/1000.0:.3f}s{when}, approximately "
                f"{ratio:.2f}× its median over-time response level "
                f"({median/1000.0:.3f}s)."
            ),
        )

    if recovery_candidates and not degradation_candidates:
        recovery_candidates.sort(reverse=True)
        change_pct, txn, first_value, last_value = recovery_candidates[0]
        add(
            45,
            "success",
            "Healthy behaviour",
            f"Latency recovery observed: {txn}",
            (
                f"Average response time improved by {change_pct:.0f}% from the first third "
                f"to the final third of steady state "
                f"({first_value/1000.0:.3f}s to {last_value/1000.0:.3f}s)."
            ),
        )

    correlation_candidates = []
    for txn, latency_values in avg_series.items():
        correlation = _series_correlation(
            latency_values,
            error_series.get(txn) or [],
        )
        if correlation is not None and correlation >= 0.65:
            max_error = max([
                float(value) for value in (error_series.get(txn) or [])
                if value is not None
            ] or [0.0])
            if max_error > 0:
                correlation_candidates.append((correlation, txn, max_error))
    if correlation_candidates:
        correlation_candidates.sort(reverse=True)
        correlation, txn, max_error = correlation_candidates[0]
        add(
            74,
            "warning",
            "Time-series findings",
            f"Latency and errors move together: {txn}",
            (
                f"Response-time and error-rate movements show a positive correlation "
                f"(r={correlation:.2f}) for {txn}; peak bucket error rate reached "
                f"{max_error:.2f}%. This is correlation, not proof of a shared root cause."
            ),
        )

    throughput_cv = _coefficient_of_variation(throughput_series)
    if throughput_cv is not None:
        if throughput_cv <= 0.10:
            if degradation_candidates:
                add(
                    80,
                    "warning",
                    "Capacity behaviour",
                    "Throughput remained stable while latency increased",
                    (
                        f"Aggregate throughput varied by only about {throughput_cv*100:.1f}% "
                        f"(coefficient of variation) while response time degraded. "
                        f"The slowdown therefore occurred without an obvious reduction in offered throughput."
                    ),
                )
            else:
                add(
                    38,
                    "success",
                    "Healthy behaviour",
                    "Throughput remained stable",
                    (
                        f"Aggregate throughput was stable throughout steady state "
                        f"(coefficient of variation {throughput_cv*100:.1f}%)."
                    ),
                )
        elif throughput_cv >= 0.25:
            add(
                66,
                "warning",
                "Capacity behaviour",
                "Throughput was unstable",
                (
                    f"Aggregate throughput varied materially during steady state "
                    f"(coefficient of variation {throughput_cv*100:.1f}%). "
                    f"Check whether load generation, pacing, errors or application capacity changed over time."
                ),
            )

    if green_sla is not None:
        near_sla = []
        try:
            green_value = float(green_sla)
        except (TypeError, ValueError):
            green_value = None
        if green_value and green_value > 0:
            for row in summary:
                avg = _metric(row, "Avg (s)")
                if row.get("RAG") == "GREEN" and 0.85 * green_value <= avg < green_value:
                    near_sla.append(row)
        if near_sla:
            row = max(near_sla, key=lambda item: _metric(item, "Avg (s)"))
            add(
                58,
                "warning",
                "Supporting findings",
                f"Approaching SLA threshold: {row.get('Transaction')}",
                (
                    f"Average response time is {_metric(row, 'Avg (s)'):.3f}s, within 15% "
                    f"of the configured green threshold ({green_value:.3f}s)."
                ),
            )

    monitoring_stats = monitoring_stats or []
    if monitoring_stats:
        worst_status = {"GREEN": 0, "AMBER": 1, "RED": 2}
        worst_server = max(
            monitoring_stats,
            key=lambda item: worst_status.get(str(item.get("status") or "GREEN"), 0),
        )
        red_servers = [item for item in monitoring_stats if item.get("status") == "RED"]
        amber_servers = [item for item in monitoring_stats if item.get("status") == "AMBER"]

        if red_servers:
            item = max(
                red_servers,
                key=lambda row: max(
                    float(row.get("cpu_high_pct") or 0),
                    float(row.get("mem_high_pct") or 0),
                    float(row.get("p95_cpu") or 0),
                    float(row.get("p95_mem") or 0),
                ),
            )
            pressure = []
            if (
                float(item.get("avg_cpu") or 0) >= 85
                or float(item.get("p95_cpu") or 0) >= 90
                or float(item.get("cpu_high_pct") or 0) >= 20
            ):
                pressure.append(
                    f"CPU averaged {float(item.get('avg_cpu') or 0):.1f}% "
                    f"(P95 {float(item.get('p95_cpu') or 0):.1f}%, "
                    f"peak {float(item.get('max_cpu') or 0):.1f}%)"
                )
            if (
                float(item.get("avg_mem") or 0) >= 85
                or float(item.get("p95_mem") or 0) >= 90
                or float(item.get("mem_high_pct") or 0) >= 20
            ):
                pressure.append(
                    f"memory averaged {float(item.get('avg_mem') or 0):.1f}% "
                    f"(P95 {float(item.get('p95_mem') or 0):.1f}%, "
                    f"peak {float(item.get('max_mem') or 0):.1f}%)"
                )
            add(
                89,
                "danger",
                "Infrastructure monitoring",
                f"Resource pressure detected: {item.get('server')}",
                (
                    "; ".join(pressure)
                    + ". Review this server alongside the application latency/error timeline."
                ),
            )
        elif amber_servers:
            item = max(
                amber_servers,
                key=lambda row: max(
                    float(row.get("max_cpu") or 0),
                    float(row.get("max_mem") or 0),
                ),
            )
            add(
                67,
                "warning",
                "Infrastructure monitoring",
                f"Resource utilisation requires attention: {item.get('server')}",
                (
                    f"CPU averaged {float(item.get('avg_cpu') or 0):.1f}% "
                    f"(peak {float(item.get('max_cpu') or 0):.1f}%) and memory averaged "
                    f"{float(item.get('avg_mem') or 0):.1f}% "
                    f"(peak {float(item.get('max_mem') or 0):.1f}%). "
                    f"Pressure was not sustained enough to classify as critical."
                ),
            )
        else:
            add(
                44,
                "success",
                "Infrastructure monitoring",
                "Monitored infrastructure remained within thresholds",
                (
                    f"{len(monitoring_stats)} monitored server(s) remained within the "
                    f"configured CPU and memory pressure thresholds during the captured interval."
                ),
            )

        peak_cpu = max(monitoring_stats, key=lambda row: float(row.get("max_cpu") or 0))
        peak_mem = max(monitoring_stats, key=lambda row: float(row.get("max_mem") or 0))
        add(
            40,
            "info",
            "Infrastructure monitoring",
            "Monitoring coverage summary",
            (
                f"Peak CPU was {float(peak_cpu.get('max_cpu') or 0):.1f}% on "
                f"{peak_cpu.get('server')}; peak memory was "
                f"{float(peak_mem.get('max_mem') or 0):.1f}% on {peak_mem.get('server')}. "
                f"Monitoring statistics are based on captured samples during the test run."
            ),
        )

    # Keep one executive verdict, then the most actionable unique findings.
    findings.sort(key=lambda observation: observation["priority"], reverse=True)
    selected = []
    seen_titles = set()
    for observation in findings:
        if observation["title"] in seen_titles:
            continue
        selected.append(observation)
        seen_titles.add(observation["title"])
        if len(selected) >= max_observations:
            break

    if monitoring_stats and not any(
        observation.get("category") == "Infrastructure monitoring"
        for observation in selected
    ):
        monitoring_candidates = [
            observation
            for observation in findings
            if observation.get("category") == "Infrastructure monitoring"
        ]
        if monitoring_candidates:
            best_monitoring = monitoring_candidates[0]
            if len(selected) >= max_observations:
                selected[-1] = best_monitoring
            else:
                selected.append(best_monitoring)

    return selected

def bounded_query_int(name, default, minimum=1, maximum=50):
    try:
        value = int(request.args.get(name, default))
    except (TypeError, ValueError):
        value = default
    return min(max(value, minimum), maximum)


def build_trend_observations(txn_trends):
    """Summarise oldest-to-newest transaction movement in concise bullets."""
    movements = []
    for txn, points in txn_trends.items():
        valid = [p for p in points if p.get("avg", 0) > 0]
        if len(valid) < 2:
            continue
        oldest, latest = valid[0], valid[-1]  # trend points are oldest to newest
        baseline = oldest["avg"]
        change = ((latest["avg"] - baseline) / baseline * 100) if baseline else 0
        movements.append((txn, change, baseline, latest["avg"], len(valid)))

    observations = []
    if not movements:
        return [{"type": "info", "title": "Insufficient trend history", "text": "At least two tests with valid data are needed to identify transaction trends."}]

    degrading = sorted([m for m in movements if m[1] > 10], key=lambda m: m[1], reverse=True)
    improving = sorted([m for m in movements if m[1] < -10], key=lambda m: m[1])
    stable = [m for m in movements if -10 <= m[1] <= 10]

    if degrading:
        txn, change, old, new, count = degrading[0]
        observations.append({"type": "danger", "title": f"Regression detected: {txn}", "text": f"Average response time increased by {change:.1f}% ({old:.2f}s to {new:.2f}s) across {count} tests."})
        if len(degrading) > 1:
            observations.append({"type": "warning", "title": "Additional degrading transactions", "text": ", ".join(f"{m[0]} (+{m[1]:.1f}%)" for m in degrading[1:4])})
    if improving:
        txn, change, old, new, count = improving[0]
        observations.append({"type": "success", "title": f"Improvement detected: {txn}", "text": f"Average response time reduced by {abs(change):.1f}% ({old:.2f}s to {new:.2f}s) across {count} tests."})
    if stable:
        observations.append({"type": "info", "title": "Stable transactions", "text": f"{len(stable)} transaction(s) changed by no more than 10% between the oldest and latest test."})
    return observations
ALLOWED_COMPARE_METRICS = {"Avg (s)", "90th % (s)", "95th % (s)", "Error %"}


def build_comparison_data(r1, r2, metric="Avg (s)", selected_txns=None):
    if metric not in ALLOWED_COMPARE_METRICS:
        metric = "Avg (s)"

    summary1 = r1.get("summary", [])
    summary2 = r2.get("summary", [])
    all_txns = sorted(
        {row.get("Transaction") for row in summary1 if row.get("Transaction")}
        | {row.get("Transaction") for row in summary2 if row.get("Transaction")}
    )
    txn_filter = selected_txns or all_txns

    comparisons = []
    for txn in txn_filter:
        v1 = next(
            (row.get(metric) for row in summary1 if row.get("Transaction") == txn),
            None,
        )
        v2 = next(
            (row.get(metric) for row in summary2 if row.get("Transaction") == txn),
            None,
        )
        if v1 is None or v2 is None:
            continue
        try:
            v1, v2 = float(v1), float(v2)
        except (TypeError, ValueError):
            continue

        diff = v2 - v1
        if abs(diff) < 0.001:
            status, color = "No Change", "grey"
        elif diff > 0:
            status, color = "Degraded", "red"
        else:
            status, color = "Improved", "green"

        change_pct = (diff / abs(v1) * 100.0) if v1 else None
        comparisons.append({
            "transaction": txn,
            "v1": round(v1, 4),
            "v2": round(v2, 4),
            "diff": round(diff, 4),
            "change_pct": round(change_pct, 2) if change_pct is not None else None,
            "status": status,
            "color": color,
        })

    observations = []
    degraded = [row for row in comparisons if row["status"] == "Degraded"]
    improved = [row for row in comparisons if row["status"] == "Improved"]
    if degraded:
        observations.append(f"{len(degraded)} transaction(s) show degradation.")
    if improved:
        observations.append(f"{len(improved)} transaction(s) improved.")
    if not observations:
        observations.append("Performance is stable across compared reports.")

    return metric, all_txns, txn_filter, comparisons, observations


def _resolve_report_pair(reports, ids):
    if len(ids) != 2:
        raise ValueError("Exactly two reports are required")
    try:
        indexes = [int(value) for value in ids]
    except (TypeError, ValueError) as exc:
        raise ValueError("Report IDs must be integers") from exc
    if any(index < 0 or index >= len(reports) for index in indexes):
        raise IndexError("Report selection is out of range")
    first, second = reports[indexes[0]], reports[indexes[1]]
    return tuple(sorted(
        (first, second),
        key=lambda report: report.get("timestamp") or "",
    ))


@app.route("/about")
def about():
    recent_changes = "No changelog available."
    for changelog_path in ("CHANGELOG.md", "Changelog.md"):
        try:
            with open(changelog_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()
            recent_changes = "".join(lines[-20:])
            break
        except OSError:
            continue
    return render_template("about.html",
                           version=__version__,
                           build=__build__,
                           codename=__codename__,
                           recent_changes=recent_changes)

@app.route("/")
def home():
    return redirect(url_for("upload"))

@app.route("/login", methods=["GET", "POST"])
def login():
    if "user_profile" in session:
        return redirect(url_for("upload"))

    requested_next = request.args.get("next")
    if request.method == "GET" and is_safe_local_redirect(requested_next):
        session["post_login_redirect"] = requested_next

    if request.method == "POST":
        email = _normalise_email(request.form.get("email"))
        pin = str(request.form.get("pin") or "").strip()

        if not EMAIL_PATTERN.fullmatch(email) or not _valid_pin(pin):
            flash("Enter a valid work email and 6-digit PIN.", "error")
            return render_template("login.html", email=email), 400

        user = _load_auth_user(email)
        if not user or not user["is_active"]:
            flash("Invalid email or PIN.", "error")
            return render_template("login.html", email=email), 401

        now = int(time.time())
        locked_until = int(user["locked_until"] or 0)
        if locked_until > now:
            remaining_minutes = max(1, (locked_until - now + 59) // 60)
            flash(
                f"Too many failed attempts. Try again in {remaining_minutes} minute(s).",
                "error",
            )
            return render_template("login.html", email=email), 429

        if not _pin_matches(user["pin_hash"], pin):
            new_lockout = _record_failed_login(user["id"], user["failed_attempts"])
            if new_lockout:
                flash(
                    f"Too many failed attempts. Login is locked for "
                    f"{PIN_LOCKOUT_MINUTES} minute(s).",
                    "error",
                )
                return render_template("login.html", email=email), 429

            flash("Invalid email or PIN.", "error")
            return render_template("login.html", email=email), 401

        _record_successful_login(user["id"])
        return redirect(_establish_pin_session(user))

    return render_template("login.html", email="")


@app.route("/register", methods=["GET", "POST"])
def register():
    if "user_profile" in session:
        return redirect(url_for("upload"))

    if request.method == "POST":
        display_name = str(request.form.get("display_name") or "").strip()
        project_name = re.sub(r"\s+", " ", str(request.form.get("project_name") or "").strip())
        email = _normalise_email(request.form.get("email"))
        pin = str(request.form.get("pin") or "").strip()
        confirm_pin = str(request.form.get("confirm_pin") or "").strip()
        registration_code = str(request.form.get("registration_code") or "").strip()

        if len(display_name) < 2 or len(display_name) > 80:
            flash("Enter your name.", "error")
            return _render_register(display_name, email, project_name, 400)

        if len(project_name) < 2 or len(project_name) > 80:
            flash("Enter the application/project name for your performance results.", "error")
            return _render_register(display_name, email, project_name, 400)

        if not _email_allowed(email):
            if PIN_ALLOWED_EMAIL_DOMAINS:
                flash("Use an approved organization email address.", "error")
            else:
                flash("Enter a valid work email address.", "error")
            return _render_register(display_name, email, project_name, 400)

        if PIN_REGISTRATION_CODE and not secrets.compare_digest(
            registration_code,
            PIN_REGISTRATION_CODE,
        ):
            flash("The registration code is invalid.", "error")
            return _render_register(display_name, email, project_name, 403)

        if not _valid_pin(pin):
            flash(f"PIN must contain exactly {PIN_LENGTH} digits.", "error")
            return _render_register(display_name, email, project_name, 400)

        if pin != confirm_pin:
            flash("PIN and confirmation do not match.", "error")
            return _render_register(display_name, email, project_name, 400)

        if pin in {"000000", "111111", "123456", "654321", "999999"}:
            flash("Choose a less predictable PIN.", "error")
            return _render_register(display_name, email, project_name, 400)

        try:
            with _auth_db_connection() as connection:
                cursor = connection.execute(
                    """
                    INSERT INTO users (
                        email, display_name, pin_hash, created_at,
                        failed_attempts, locked_until, is_active
                    )
                    VALUES (?, ?, ?, ?, 0, 0, 1)
                    """,
                    (email, display_name, _pin_hash(pin), int(time.time())),
                )
                user_id = cursor.lastrowid
        except sqlite3.IntegrityError:
            flash("An account with that email already exists. Sign in instead.", "error")
            return _render_register(display_name, email, project_name, 409)

        user = {
            "id": user_id,
            "display_name": display_name,
            "email": email,
        }
        migrate_legacy = not load_projects()
        project = create_project(project_name, user_id, migrate_legacy=migrate_legacy)
        destination = _establish_pin_session(user)
        set_active_project(project)
        flash(f"Registration complete. Active project: {project['name']}.", "success")
        return redirect(url_for("upload"))

    return _render_register()[0]


@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))


@app.route("/projects/select", methods=["GET", "POST"])
def project_select():
    user_id = _current_user_id()
    projects = projects_for_user(user_id)

    requested_next = request.args.get("next")
    if request.method == "GET" and is_safe_local_redirect(requested_next):
        session["post_project_redirect"] = requested_next

    if request.method == "POST":
        project_id = str(request.form.get("project_id") or "").strip()
        project = find_user_project(project_id, user_id)
        if not project:
            flash("Select a valid project.", "error")
        else:
            set_active_project(project)
            destination = session.pop("post_project_redirect", None)
            if not is_safe_local_redirect(destination):
                destination = url_for("upload")
            return redirect(destination)

    return render_template(
        "projects.html",
        projects=projects,
        active_project=active_project(),
    )


@app.route("/projects/create", methods=["POST"])
def project_create():
    user_id = _current_user_id()
    project_name = str(request.form.get("project_name") or "").strip()
    try:
        migrate_legacy = not load_projects()
        project = create_project(project_name, user_id, migrate_legacy=migrate_legacy)
    except ValueError as exc:
        flash(str(exc), "error")
        return redirect(url_for("project_select"))

    set_active_project(project)
    flash(f"Project '{project['name']}' created and selected.", "success")
    destination = session.pop("post_project_redirect", None)
    if not is_safe_local_redirect(destination):
        destination = url_for("upload")
    return redirect(destination)


@app.route("/upload", methods=["GET", "POST"])
def upload():
    if "user" not in session:
        return redirect(url_for("login"))

    if request.method == "POST":
        file = request.files.get("file")
        if not file or not file.filename:
            flash("Please select a JMeter CSV or JTL file.", "error")
            return redirect(url_for("upload"))

        filename = secure_filename(file.filename)
        if not filename.lower().endswith((".csv", ".jtl")):
            flash("Unsupported file type. Please upload a .csv or .jtl JMeter result file.", "error")
            return redirect(url_for("upload"))

        file_path = os.path.join(UPLOAD_FOLDER, filename)
        file.save(file_path)

        try:
            test_start_ms, test_end_ms = detect_test_window(file_path)
            test_start_ms, test_end_ms = int(test_start_ms), int(test_end_ms)
            if test_end_ms < test_start_ms:
                raise ValueError("Invalid JMeter test window.")
        except ValueError as exc:
            try:
                os.remove(file_path)
            except OSError:
                pass
            session.pop("uploaded_file", None)
            session.pop("uploaded_file_path", None)
            session.pop("test_window", None)
            flash(f"Unable to use this JMeter result file: {exc}", "error")
            return redirect(url_for("upload"))
        except Exception:
            app.logger.exception("Unexpected error while validating uploaded JMeter result: %s", file_path)
            try:
                os.remove(file_path)
            except OSError:
                pass
            session.pop("uploaded_file", None)
            session.pop("uploaded_file_path", None)
            session.pop("test_window", None)
            flash("Unable to read the uploaded JMeter result file.", "error")
            return redirect(url_for("upload"))

        session["uploaded_file"] = filename
        session["uploaded_file_path"] = file_path
        session["test_window"] = {
            "start_ms": test_start_ms,
            "end_ms": test_end_ms,
        }
        flash(f"{filename} uploaded successfully. Test start and end times were detected.", "success")
        return redirect(url_for("upload"))

    uploaded_file = session.get("uploaded_file")
    transactions = []
    test_window = None

    if uploaded_file:
        file_path = session.get("uploaded_file_path")
        raw_window = session.get("test_window") or {}

        if file_path and os.path.exists(file_path):
            try:
                green, amber, rag_basis = 2.0, 5.0, "avg"
                summary, _ = parse_jmeter_csv(file_path, green, amber, rag_basis)
                transactions = [
                    row.get("Transaction")
                    for row in summary
                    if row.get("Transaction")
                ]
                session["summary"] = summary

                start_ms = raw_window.get("start_ms")
                end_ms = raw_window.get("end_ms")
                if start_ms is None or end_ms is None:
                    start_ms, end_ms = detect_test_window(file_path)
                    start_ms, end_ms = int(start_ms), int(end_ms)
                    session["test_window"] = {
                        "start_ms": start_ms,
                        "end_ms": end_ms,
                    }

                start_fmt = _format_test_timestamp(start_ms)
                end_fmt = _format_test_timestamp(end_ms)
                test_window = {
                    "start_ms": int(start_ms),
                    "end_ms": int(end_ms),
                    "start_display": start_fmt["display"],
                    "end_display": end_fmt["display"],
                    "start_input": start_fmt["input"],
                    "end_input": end_fmt["input"],
                }
            except ValueError as exc:
                session.pop("uploaded_file", None)
                session.pop("uploaded_file_path", None)
                session.pop("test_window", None)
                session.pop("summary", None)
                uploaded_file = None
                transactions = []
                test_window = None
                flash(f"Unable to read the uploaded JMeter result file: {exc}", "error")
            except Exception:
                app.logger.exception("Unexpected error while preparing uploaded JMeter result: %s", file_path)
                session.pop("uploaded_file", None)
                session.pop("uploaded_file_path", None)
                session.pop("test_window", None)
                session.pop("summary", None)
                uploaded_file = None
                transactions = []
                test_window = None
                flash("Unable to read the uploaded JMeter result file.", "error")

    return render_template(
        "upload.html",
        uploaded_file=uploaded_file,
        uploaded_file_path=session.get("uploaded_file_path"),
        transactions=transactions,
        test_window=test_window,
        baseline_profiles=load_baseline_profiles(),
        selected_baseline_profile=request.args.get("baseline_profile"),
    )

@app.route("/analyze", methods=["POST"])
def analyze():
    file_path = session.get("uploaded_file_path")
    if not file_path or not os.path.exists(file_path):
        flash("Please upload a JMeter result file before generating a report.", "error")
        return redirect(url_for("upload"))

    report_name = request.form["report_name"]
    transactions = request.form.getlist("transactions")
    metrics = request.form.getlist("metrics") or ["avg", "p90", "p95", "error", "samples"]
    rag_basis = request.form["rag_basis"]
    include_error = "include_error" in request.form
    error_threshold = float(request.form.get("error_threshold", 0))
    green, amber = float(request.form["green"]), float(request.form["amber"])
    baseline_profile_id = str(request.form.get("baseline_profile_id") or "").strip()
    baseline_profile = find_baseline_profile(baseline_profile_id)

    steady_start_raw = request.form.get("steady_state_start", "").strip()
    steady_end_raw = request.form.get("steady_state_end", "").strip()
    steady_start_ms = _parse_datetime_local(steady_start_raw) if steady_start_raw else None
    steady_end_ms = _parse_datetime_local(steady_end_raw) if steady_end_raw else None

    test_window = session.get("test_window") or {}
    test_start_ms = test_window.get("start_ms")
    test_end_ms = test_window.get("end_ms")

    if bool(steady_start_raw) != bool(steady_end_raw):
        flash("Please provide both steady-state start and end times, or leave both blank.", "error")
        return redirect(url_for("upload"))

    if steady_start_raw and (steady_start_ms is None or steady_end_ms is None):
        flash("The steady-state period contains an invalid date/time.", "error")
        return redirect(url_for("upload"))

    if steady_start_ms is not None and steady_end_ms is not None:
        if steady_end_ms <= steady_start_ms:
            flash("Steady-state end time must be later than the start time.", "error")
            return redirect(url_for("upload"))
        if test_start_ms is not None and steady_start_ms < int(test_start_ms):
            flash("Steady-state start time cannot be earlier than the detected test start time.", "error")
            return redirect(url_for("upload"))
        if test_end_ms is not None and steady_end_ms > int(test_end_ms):
            flash("Steady-state end time cannot be later than the detected test end time.", "error")
            return redirect(url_for("upload"))

    summary, test_rag = parse_jmeter_csv(
        file_path,
        green,
        amber,
        rag_basis,
        steady_start_ms,
        steady_end_ms,
    )
    if not summary:
        flash("No JMeter samples were found inside the selected steady-state period.", "error")
        return redirect(url_for("upload"))

    summary, test_rag = evaluate_sla(summary, green, amber, rag_basis, include_error, error_threshold)
    filtered = [row for row in summary if row.get("Transaction") in transactions] if transactions else summary
    baseline_applied_count = 0
    if baseline_profile:
        filtered, test_rag, baseline_applied_count = apply_baseline_profile(
            filtered,
            baseline_profile,
        )
    else:
        test_rag = overall_rag(filtered)

    df = pd.read_csv(file_path)
    df['timeStamp'] = pd.to_numeric(df['timeStamp'], errors='coerce').fillna(0).astype(int)
    if not df.empty:
        start_ts, end_ts = df['timeStamp'].min(), df['timeStamp'].max()
        start_dt, end_dt = datetime.fromtimestamp(start_ts/1000.0), datetime.fromtimestamp(end_ts/1000.0)
        test_date = start_dt.strftime("%d-%m-%Y")
        test_period = f"{start_dt.strftime('%d-%m-%Y %H:%M:%S')} to {end_dt.strftime('%d-%m-%Y %H:%M:%S')}"
        total_duration = str(end_dt - start_dt)
        if steady_start_ms is not None and steady_end_ms is not None:
            steady_start_dt = datetime.fromtimestamp(steady_start_ms / 1000.0)
            steady_end_dt = datetime.fromtimestamp(steady_end_ms / 1000.0)
            steady_state = (
                f"{steady_start_dt.strftime('%d-%m-%Y %H:%M:%S')} to "
                f"{steady_end_dt.strftime('%d-%m-%Y %H:%M:%S')}"
            )
        else:
            steady_state = "Full test period"
    else:
        test_date = test_period = total_duration = "Not Available"
        concurrent_users = None
        steady_state = "Not Available"

    if steady_start_ms is not None and steady_end_ms is not None:
        df = df[
            (df["timeStamp"] >= steady_start_ms)
            & (df["timeStamp"] <= steady_end_ms)
        ].copy()

    if transactions and "label" in df.columns:
        df = df[df["label"].astype(str).isin(transactions)].copy()

    concurrent_users = (
        int(df["allThreads"].max())
        if "allThreads" in df.columns and not df.empty
        else None
    )

    for row in filtered:
        for key in ["Avg (s)", "90th % (s)", "95th % (s)", "Error %"]:
            val = row.get(key)
            if val is not None:
                try:
                    row[key] = float(val)
                except (ValueError, TypeError):
                    row[key] = None

    filtered, report_kpis = enrich_summary_and_kpis(df, filtered)
    chart_data = build_report_chart_data(df, filtered)
    try:
        graph_paths = generate_report_graph_assets(df, filtered, green, amber)
    except Exception as exc:
        print("Graph generation failed:", exc)
        graph_paths = {}

    observations = build_report_observations(
        filtered,
        test_rag,
        chart_data=chart_data,
        report_kpis=report_kpis,
        green_sla=green,
        amber_sla=amber,
    )
    if baseline_profile:
        observations.insert(1, {
            "type": "info",
            "category": "Baseline SLA",
            "priority": 98,
            "title": f"Historical baseline applied: {baseline_profile.get('name')}",
            "text": (
                f"Per-transaction P90 thresholds from the saved baseline were applied to "
                f"{baseline_applied_count} transaction(s). Transactions not present in the "
                f"baseline used the configured fallback SLA."
            ),
        })
        observations = observations[:12]

    project = active_project()
    report_data = {
        "report_name": report_name,
        "project_id": str(project.get("id")),
        "project_name": str(project.get("name") or ""),
        "file_name": os.path.basename(file_path),
        "summary": filtered,
        "rag_result": test_rag,
        "observations": observations,
        "observation_engine_version": 3,
        "test_date": test_date,
        "test_period": test_period,
        "total_duration": total_duration,
        "concurrent_users": concurrent_users,
        "steady_state": steady_state,
        "selected_metrics": metrics,
        "green": green,
        "amber": amber,
        "rag_basis": rag_basis,
        "include_error": include_error,
        "error_threshold": error_threshold if include_error else None,
        "baseline_profile_id": baseline_profile.get("id") if baseline_profile else None,
        "baseline_profile_name": baseline_profile.get("name") if baseline_profile else None,
        "baseline_applied_count": baseline_applied_count,
        "graph_paths": graph_paths,
        "report_kpis": report_kpis,
        "monitoring_stats": [],
        **chart_data,
        "timestamp": datetime.utcnow().isoformat()
    }

    save_report(report_data)

    reports = load_history()
    new_index = 0
    return redirect(url_for("report", report_index=new_index))

@app.route("/report/<int:report_index>")
def report(report_index):
    reports = load_history()
    if 0 <= report_index < len(reports):
        report_data = reports[report_index]
        if int(report_data.get("observation_engine_version") or 0) < 3:
            report_data["observations"] = build_report_observations(
                report_data.get("summary", []),
                report_data.get("rag_result"),
                chart_data=report_data,
                report_kpis=report_overview(report_data),
                green_sla=report_data.get("green"),
                amber_sla=report_data.get("amber"),
                monitoring_stats=report_data.get("monitoring_stats") or [],
            )
            report_data["observation_engine_version"] = 3
        return render_template(
            "report.html",
            report_index=report_index,
            overview=report_overview(report_data),
            **report_data,
        )
    flash("Report not found")
    return redirect(url_for("history"))

@app.route("/report/latest")
def report_latest():
    reports = load_history()
    if reports:
        return redirect(url_for("report", report_index=0))
    flash("No reports available")
    return redirect(url_for("history"))

@app.route("/history")
def history():
    reports = load_history()
    per_page = 5
    total_pages = max(1, (len(reports) + per_page - 1) // per_page)
    try:
        requested_page = int(request.args.get("page", 1))
    except (TypeError, ValueError):
        requested_page = 1
    page = min(max(requested_page, 1), total_pages)
    start, end = (page - 1) * per_page, page * per_page
    return render_template(
        "history.html",
        reports=reports[start:end],
        page=page,
        total_pages=total_pages,
        start_index=start,
    )

@app.route("/export_report_pdf/<int:report_index>")
def export_report_pdf(report_index):
    reports = load_history()
    if 0 <= report_index < len(reports):
        report_data = reports[report_index]
        if int(report_data.get("observation_engine_version") or 0) < 3:
            report_data["observations"] = build_report_observations(
                report_data.get("summary", []),
                report_data.get("rag_result"),
                chart_data=report_data,
                report_kpis=report_overview(report_data),
                green_sla=report_data.get("green"),
                amber_sla=report_data.get("amber"),
                monitoring_stats=report_data.get("monitoring_stats") or [],
            )
            report_data["observation_engine_version"] = 3
        overview = report_overview(report_data)
        pdf = build_single_report_pdf(
            report_data,
            overview,
            static_root=app.static_folder or "static",
        )
        response = make_response(pdf)
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = f"inline; filename=report_{report_index}.pdf"
        return response
    flash("Report not found")
    return redirect(url_for("history"))

@app.route("/compare/pdf")
def export_compare_pdf():
    ids = request.args.getlist("report_ids")
    reports = load_history()
    try:
        earlier, later = _resolve_report_pair(reports, ids)
    except (ValueError, IndexError):
        flash("Please select two valid reports to compare.")
        return redirect(url_for("select_compare"))

    metric, all_txns, selected_txns, comparisons, observations = build_comparison_data(
        earlier,
        later,
        request.args.get("metric", "Avg (s)"),
        request.args.getlist("transactions") or None,
    )

    overview1 = report_overview(earlier)
    overview2 = report_overview(later)
    pdf = build_compare_report_pdf(
        earlier,
        later,
        overview1,
        overview2,
        metric,
        comparisons,
        observations,
    )
    response = make_response(pdf)
    response.headers["Content-Type"] = "application/pdf"
    response.headers["Content-Disposition"] = "inline; filename=compare_report.pdf"
    return response

@app.route("/compare/select")
def select_compare():
    reports = load_history()
    return render_template("select_compare.html", reports=reports)

@app.route("/compare")
def compare():
    ids = request.args.getlist("report_ids")
    reports = load_history()
    try:
        earlier, later = _resolve_report_pair(reports, ids)
    except (ValueError, IndexError):
        flash("Please select exactly two valid reports.")
        return redirect(url_for("select_compare"))

    metric, all_txns, txn_filter, comparisons, observations = build_comparison_data(
        earlier,
        later,
        request.args.get("metric", "Avg (s)"),
        request.args.getlist("transactions") or None,
    )

    return render_template(
        "compare.html",
        r1=earlier,
        r2=later,
        metric=metric,
        comparisons=comparisons,
        observations=observations,
        all_txns=all_txns,
        selected_txns=txn_filter,
        overview1=report_overview(earlier),
        overview2=report_overview(later),
    )

@app.context_processor
def inject_test_state():
    process_alive = bool(current_process and current_process.poll() is None)
    return {"test_running": bool(test_running and process_alive)}


@app.route("/trend")
def trend():
    n = bounded_query_int("n", 10)
    reports = load_history()
    if not reports:
        flash("No reports available for trend analysis.")
        return redirect(url_for("history"))

    selected_reports = list(reversed(reports[:n]))  # oldest to newest for charting
    txn_trends = {}
    all_txns = set()

    for r in selected_reports:
        test_label = r.get("test_date") or str(r.get("timestamp") or "Unknown")[:10]
        for row in r.get("summary", []):
            txn = row.get("Transaction")
            if not txn:
                continue
            all_txns.add(txn)
            txn_trends.setdefault(txn, []).append({
                "label": test_label,
                "avg": float(row.get("Avg (s)", 0)),
                "p90": float(row.get("90th % (s)", 0)),
                "error": float(row.get("Error %", 0)),
                "rag": row.get("RAG", "UNKNOWN")
            })

    # Build summary table
    summary_table = []
    for txn, data in txn_trends.items():
        avg_vals = [d["avg"] for d in data if d["avg"] > 0]
        p90_vals = [d["p90"] for d in data if d["p90"] > 0]
        summary_table.append({
            "transaction": txn,
            "tests_executed": len(data),
            "avg_of_avg": round(sum(avg_vals)/len(avg_vals), 3) if avg_vals else None,
            "avg_of_p90": round(sum(p90_vals)/len(p90_vals), 3) if p90_vals else None
        })

    # Get selected metric and transactions from query params
    selected_metric = request.args.get("metric", "avg")
    selected_txns = request.args.getlist("transactions")

    # Default to all if none selected
    if not selected_txns:
        selected_txns = sorted(all_txns)

    trend_observations = build_trend_observations(txn_trends)

    return render_template(
        "trend.html",
        summary_table=summary_table,
        txn_trends=txn_trends,
        all_txns=sorted(all_txns),
        selected_metric=selected_metric,
        selected_txns=selected_txns,
        n=n,
        trend_observations=trend_observations
    )
def load_baseline_profiles(project_id=None, include_all=False):
    if not os.path.exists(BASELINE_FILE):
        return []
    try:
        with open(BASELINE_FILE, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        profiles = data if isinstance(data, list) else []
    except Exception:
        app.logger.exception("Unable to load baseline profiles")
        return []

    if include_all:
        return profiles

    project_id = str(project_id or session.get("active_project_id") or "")
    if not project_id:
        return []
    return [
        profile
        for profile in profiles
        if str((profile or {}).get("project_id") or "") == project_id
    ]


def save_baseline_profiles(profiles):
    os.makedirs(os.path.dirname(BASELINE_FILE), exist_ok=True)
    with open(BASELINE_FILE, "w", encoding="utf-8") as handle:
        json.dump(profiles, handle, indent=2)


def baseline_profile_number(profile):
    """Return the immutable numeric baseline identifier when available."""
    try:
        number = int(profile.get("number"))
        if number > 0:
            return number
    except (TypeError, ValueError, AttributeError):
        pass

    profile_id = str((profile or {}).get("id") or "").strip()
    if profile_id.isdigit():
        return int(profile_id)

    match = re.fullmatch(r"(?:BL-|B)?(\d+)", profile_id, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))

    name_match = re.match(r"Baseline\s+(\d+)\b", str((profile or {}).get("name") or ""), flags=re.IGNORECASE)
    return int(name_match.group(1)) if name_match else None


def next_baseline_number(profiles):
    numbers = [baseline_profile_number(profile) for profile in profiles or []]
    valid = [number for number in numbers if number is not None]
    return (max(valid) + 1) if valid else 1


def baseline_profile_label(profile):
    number = baseline_profile_number(profile)
    name = str((profile or {}).get("name") or "").strip()
    prefix = f"Baseline {number}" if number is not None else "Saved Baseline"
    if name and name.lower() != prefix.lower():
        return f"{prefix} — {name}"
    return prefix


def build_baseline_comparison(profile_a, profile_b, stable_tolerance_pct=5.0):
    """Compare two immutable baseline snapshots using P90 as the primary signal."""
    if not profile_a or not profile_b:
        return None

    txns_a = profile_a.get("transactions") or {}
    txns_b = profile_b.get("transactions") or {}
    all_transactions = sorted(set(txns_a) | set(txns_b))
    rows = []
    counts = {"improved": 0, "degraded": 0, "stable": 0, "not_comparable": 0}

    def numeric(rule, key):
        try:
            value = float((rule or {}).get(key))
        except (TypeError, ValueError):
            return None
        return value if np.isfinite(value) else None

    for transaction in all_transactions:
        left = txns_a.get(transaction)
        right = txns_b.get(transaction)
        row = {
            "transaction": transaction,
            "avg_a": numeric(left, "baseline_avg_s"),
            "avg_b": numeric(right, "baseline_avg_s"),
            "p90_a": numeric(left, "baseline_p90_s"),
            "p90_b": numeric(right, "baseline_p90_s"),
            "p95_a": numeric(left, "baseline_p95_s"),
            "p95_b": numeric(right, "baseline_p95_s"),
            "green_a": numeric(left, "green_p90_s"),
            "green_b": numeric(right, "green_p90_s"),
            "amber_a": numeric(left, "amber_p90_s"),
            "amber_b": numeric(right, "amber_p90_s"),
            "delta_p90_s": None,
            "change_pct": None,
            "status": "NOT COMPARABLE",
        }

        if row["p90_a"] is not None and row["p90_b"] is not None:
            delta = row["p90_b"] - row["p90_a"]
            change_pct = (delta / row["p90_a"]) * 100.0 if row["p90_a"] != 0 else None
            row["delta_p90_s"] = round(delta, 3)
            row["change_pct"] = round(change_pct, 2) if change_pct is not None else None

            if change_pct is None or abs(change_pct) <= stable_tolerance_pct:
                row["status"] = "STABLE"
                counts["stable"] += 1
            elif change_pct < 0:
                row["status"] = "IMPROVED"
                counts["improved"] += 1
            else:
                row["status"] = "DEGRADED"
                counts["degraded"] += 1
        else:
            counts["not_comparable"] += 1
        rows.append(row)

    return {
        "baseline_a": profile_a,
        "baseline_b": profile_b,
        "label_a": baseline_profile_label(profile_a),
        "label_b": baseline_profile_label(profile_b),
        "rows": rows,
        "counts": counts,
        "stable_tolerance_pct": stable_tolerance_pct,
    }


def _baseline_metric_stats(values):
    cleaned = []
    for value in values or []:
        try:
            numeric = float(value)
        except (TypeError, ValueError):
            continue
        if np.isfinite(numeric):
            cleaned.append(numeric)
    if not cleaned:
        return None
    arr = np.array(cleaned, dtype=float)
    mean = float(np.mean(arr))
    std = float(np.std(arr, ddof=0))
    cv_pct = (std / abs(mean) * 100.0) if mean else 0.0
    return {
        "count": len(cleaned),
        "mean": round(mean, 4),
        "median": round(float(np.median(arr)), 4),
        "min": round(float(np.min(arr)), 4),
        "max": round(float(np.max(arr)), 4),
        "std": round(std, 4),
        "cv_pct": round(cv_pct, 2),
    }


def build_baseline_analysis(reports, n=10, min_runs=3):
    """Build a deterministic baseline from recent comparable historical reports."""
    selected_reports = list(reports[: max(1, int(n))])
    total_reports = len(selected_reports)
    if not selected_reports:
        return {
            "reports_used": 0,
            "transactions": [],
            "eligible_count": 0,
            "stable_count": 0,
            "warnings": ["No historical reports are available."],
        }

    by_transaction = {}
    # Oldest-to-newest series for trend charts.
    for report in reversed(selected_reports):
        label = (
            report.get("test_date")
            or str(report.get("timestamp") or "Unknown")[:10]
            or "Unknown"
        )
        seen_this_report = set()
        for row in report.get("summary") or []:
            txn = str(row.get("Transaction") or "").strip()
            if not txn or txn in seen_this_report:
                continue
            seen_this_report.add(txn)
            bucket = by_transaction.setdefault(
                txn,
                {"avg": [], "p90": [], "p95": [], "error": [], "trend": []},
            )
            point = {"label": label}
            for key, metric_name in (
                ("Avg (s)", "avg"),
                ("90th % (s)", "p90"),
                ("95th % (s)", "p95"),
                ("Error %", "error"),
            ):
                raw = row.get(key)
                try:
                    value = float(raw)
                except (TypeError, ValueError):
                    value = None
                if value is not None and np.isfinite(value):
                    bucket[metric_name].append(value)
                    point[metric_name] = value
                else:
                    point[metric_name] = None
            bucket["trend"].append(point)

    results = []
    warnings = []
    for txn in sorted(by_transaction):
        values = by_transaction[txn]
        avg_stats = _baseline_metric_stats(values["avg"])
        p90_stats = _baseline_metric_stats(values["p90"])
        p95_stats = _baseline_metric_stats(values["p95"])
        error_stats = _baseline_metric_stats(values["error"])
        run_count = max(
            len(values["avg"]),
            len(values["p90"]),
            len(values["p95"]),
        )
        coverage_pct = (100.0 * run_count / total_reports) if total_reports else 0.0
        cvs = [
            stats["cv_pct"]
            for stats in (avg_stats, p90_stats, p95_stats)
            if stats is not None
        ]
        max_cv = max(cvs) if cvs else None

        if max_cv is None:
            stability = "INSUFFICIENT"
        elif max_cv <= 10.0:
            stability = "STABLE"
        elif max_cv <= 20.0:
            stability = "VARIABLE"
        else:
            stability = "UNSTABLE"

        eligible = bool(
            run_count >= min_runs
            and coverage_pct >= 60.0
            and max_cv is not None
            and max_cv <= 15.0
            and p90_stats is not None
        )

        proposed_green = proposed_amber = None
        if p90_stats is not None:
            mean = float(p90_stats["mean"])
            std = float(p90_stats["std"])
            # Statistical headroom based on observed run-to-run variation, with
            # small minimum buffers so perfectly flat histories do not create a
            # zero-tolerance SLA.
            proposed_green = max(mean + std, mean * 1.05)
            proposed_amber = max(mean + (2.0 * std), mean * 1.15)
            if proposed_amber <= proposed_green:
                proposed_amber = proposed_green * 1.10

        if eligible and run_count >= 7 and coverage_pct >= 80.0 and max_cv <= 10.0:
            confidence = "HIGH"
        elif eligible:
            confidence = "MODERATE"
        else:
            confidence = "LOW"

        if run_count < min_runs:
            reason = f"Needs at least {min_runs} runs; only {run_count} available."
        elif coverage_pct < 60.0:
            reason = f"Appears in only {coverage_pct:.0f}% of selected reports."
        elif max_cv is None:
            reason = "Not enough valid response-time data."
        elif max_cv > 15.0:
            reason = f"Run-to-run variation is too high ({max_cv:.1f}% CV)."
        else:
            reason = "History is sufficiently consistent for baseline use."

        results.append({
            "transaction": txn,
            "run_count": run_count,
            "coverage_pct": round(coverage_pct, 1),
            "avg": avg_stats,
            "p90": p90_stats,
            "p95": p95_stats,
            "error": error_stats,
            "max_cv_pct": round(max_cv, 2) if max_cv is not None else None,
            "stability": stability,
            "eligible": eligible,
            "confidence": confidence,
            "proposed_green_p90_s": round(proposed_green, 3) if proposed_green is not None else None,
            "proposed_amber_p90_s": round(proposed_amber, 3) if proposed_amber is not None else None,
            "reason": reason,
            "trend": values["trend"],
        })

    eligible_count = sum(1 for row in results if row["eligible"])
    stable_count = sum(1 for row in results if row["stability"] == "STABLE")
    if total_reports < min_runs:
        warnings.append(
            f"Only {total_reports} report(s) are available. At least {min_runs} are required "
            "before any transaction can be promoted to a baseline SLA."
        )
    if not eligible_count and total_reports >= min_runs:
        warnings.append(
            "No transaction currently has enough stable, consistent history to be promoted."
        )

    return {
        "reports_used": total_reports,
        "transactions": results,
        "eligible_count": eligible_count,
        "stable_count": stable_count,
        "warnings": warnings,
    }


def find_baseline_profile(profile_id):
    if not profile_id:
        return None
    requested = str(profile_id)
    return next(
        (profile for profile in load_baseline_profiles() if str(profile.get("id")) == requested),
        None,
    )


def apply_baseline_profile(summary, profile):
    """Apply per-transaction P90 baseline thresholds to a report summary."""
    if not profile:
        return summary, overall_rag(summary), 0

    thresholds = profile.get("transactions") or {}
    updated = []
    applied = 0
    for row in summary:
        new_row = dict(row)
        txn = str(new_row.get("Transaction") or "")
        rule = thresholds.get(txn)
        if rule:
            p90 = _metric(new_row, "90th % (s)")
            green = float(rule.get("green_p90_s") or 0)
            amber = float(rule.get("amber_p90_s") or 0)
            if amber > 0 and green > 0:
                if p90 > amber:
                    new_row["RAG"] = "RED"
                elif p90 > green:
                    new_row["RAG"] = "AMBER"
                else:
                    new_row["RAG"] = "GREEN"
                new_row["Baseline Green P90 (s)"] = green
                new_row["Baseline Amber P90 (s)"] = amber
                new_row["Baseline SLA Applied"] = True
                applied += 1
        updated.append(new_row)
    return updated, overall_rag(updated), applied


@app.route("/baseline", methods=["GET", "POST"])
def baseline():
    try:
        n = int(request.values.get("n", 10))
    except (TypeError, ValueError):
        n = 10
    n = min(max(n, 3), 50)

    reports = load_history()
    if not reports:
        flash("No reports available for baseline calculation.")
        return redirect(url_for("history"))

    analysis = build_baseline_analysis(reports, n=n)

    if request.method == "POST":
        profile_name = str(request.form.get("profile_name") or "").strip()

        eligible = [
            row for row in analysis["transactions"]
            if row.get("eligible")
            and row.get("proposed_green_p90_s") is not None
            and row.get("proposed_amber_p90_s") is not None
        ]
        if not eligible:
            flash(
                "No stable transactions are eligible for SLA promotion yet.",
                "error",
            )
            return redirect(url_for("baseline", n=n))

        profiles = load_baseline_profiles()
        baseline_number = next_baseline_number(profiles)
        if not profile_name:
            profile_name = f"Baseline {baseline_number}"

        source_reports = [
            {
                "report_name": report.get("report_name"),
                "file_name": report.get("file_name"),
                "test_date": report.get("test_date"),
                "timestamp": report.get("timestamp"),
            }
            for report in reports[:n]
        ]

        project = active_project()
        profile = {
            "id": str(baseline_number),
            "number": baseline_number,
            "name": profile_name,
            "project_id": str(project.get("id")),
            "project_name": str(project.get("name") or ""),
            "created_at": datetime.utcnow().isoformat(),
            "source_report_count": analysis["reports_used"],
            "source_reports": source_reports,
            "baseline_engine_version": 1,
            "method": "P90 mean + observed run-to-run variation",
            "transactions": {
                row["transaction"]: {
                    "green_p90_s": row["proposed_green_p90_s"],
                    "amber_p90_s": row["proposed_amber_p90_s"],
                    "baseline_avg_s": row["avg"]["mean"] if row.get("avg") else None,
                    "baseline_p90_s": row["p90"]["mean"] if row.get("p90") else None,
                    "baseline_p95_s": row["p95"]["mean"] if row.get("p95") else None,
                    "run_count": row["run_count"],
                    "coverage_pct": row["coverage_pct"],
                    "max_cv_pct": row["max_cv_pct"],
                    "confidence": row["confidence"],
                }
                for row in eligible
            },
        }
        all_profiles = load_baseline_profiles(include_all=True)
        all_profiles.insert(0, profile)
        save_baseline_profiles(all_profiles)
        flash(
            f"Baseline {baseline_number} saved with {len(eligible)} transaction(s).",
            "success",
        )
        return redirect(url_for("baseline", n=n, saved=profile["id"]))

    profiles = load_baseline_profiles()
    compare_a_id = str(request.args.get("compare_a") or "").strip()
    compare_b_id = str(request.args.get("compare_b") or "").strip()
    comparison = None
    comparison_error = None

    if compare_a_id or compare_b_id:
        if not compare_a_id or not compare_b_id:
            comparison_error = "Select both baselines before comparing."
        elif compare_a_id == compare_b_id:
            comparison_error = "Choose two different baselines to compare."
        else:
            profile_a = next((item for item in profiles if str(item.get("id")) == compare_a_id), None)
            profile_b = next((item for item in profiles if str(item.get("id")) == compare_b_id), None)
            if not profile_a or not profile_b:
                comparison_error = "One of the selected baselines could not be found."
            else:
                comparison = build_baseline_comparison(profile_a, profile_b)

    return render_template(
        "baseline.html",
        analysis=analysis,
        n=n,
        profiles=profiles,
        saved_profile_id=request.args.get("saved"),
        compare_a_id=compare_a_id,
        compare_b_id=compare_b_id,
        comparison=comparison,
        comparison_error=comparison_error,
        baseline_profile_label=baseline_profile_label,
    )


test_running = False
current_process = None  # track JMeter process globally
current_run_dir = None
last_test_summary = None
live_progress_points = deque(maxlen=500)

def _latest_run_dir():
    candidates = []
    if not os.path.isdir(UPLOAD_FOLDER):
        return None
    for name in os.listdir(UPLOAD_FOLDER):
        path = os.path.join(UPLOAD_FOLDER, name)
        if not name.startswith("run_") or not os.path.isdir(path):
            continue
        results_path = os.path.join(path, "results.jtl")
        sort_path = results_path if os.path.exists(results_path) else path
        candidates.append((os.path.getmtime(sort_path), path))
    return max(candidates, default=(None, None), key=lambda item: item[0])[1]

def _write_run_project_metadata(run_dir, project):
    if not run_dir or not project:
        return
    metadata = {
        "project_id": str(project.get("id")),
        "project_name": str(project.get("name") or ""),
    }
    with open(os.path.join(run_dir, "project.json"), "w", encoding="utf-8") as handle:
        json.dump(metadata, handle, indent=2)


def _read_run_project_metadata(run_dir):
    if not run_dir:
        return {}
    path = os.path.join(run_dir, "project.json")
    if not os.path.isfile(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as handle:
            data = json.load(handle)
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def make_run_dir():
    run_id = uuid.uuid4().hex[:8]
    run_dir = os.path.join("uploads", f"run_{run_id}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir


def _read_recent_lines(path, max_lines=200):
    if not path or not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            return [line.rstrip("\r\n") for line in deque(handle, maxlen=max_lines)]
    except OSError:
        return []


def _current_results_available():
    if not current_run_dir:
        return False
    results_path = os.path.join(current_run_dir, "results.jtl")
    try:
        return os.path.isfile(results_path) and os.path.getsize(results_path) > 0
    except OSError:
        return False


def _current_log_snapshot():
    if not current_run_dir:
        return []
    jmeter_log = os.path.join(current_run_dir, "jmeter.log")
    process_log = os.path.join(current_run_dir, "jmeter_process.log")
    lines = _read_recent_lines(jmeter_log, max_lines=200)
    if lines:
        return lines
    return _read_recent_lines(process_log, max_lines=200)


@app.route("/run_test", methods=["GET", "POST"])
def run_test():
    global test_running, current_process, current_run_dir, last_test_summary
    if request.method == "POST":
        if test_running and current_process and current_process.poll() is None:
            flash("A JMeter test is already running. Open Live Progress to monitor it.", "warning")
            return redirect(url_for("live_progress"))

        jmx_file = request.files.get("jmx_file")
        if not jmx_file or jmx_file.filename == "":
            flash("Please select a JMX test plan.", "error")
            return redirect(url_for("run_test"))

        jmx_name = secure_filename(jmx_file.filename)
        if not jmx_name.lower().endswith(".jmx"):
            flash("Please upload a valid .jmx test plan.", "error")
            return redirect(url_for("run_test"))

        data_files = request.files.getlist("data_files")
        prepared_data_files = []
        allowed_data_extensions = {".csv", ".tsv", ".txt"}
        for data_file in data_files:
            if not data_file or not data_file.filename:
                continue
            data_name = secure_filename(data_file.filename)
            if not data_name:
                continue
            extension = os.path.splitext(data_name)[1].lower()
            if extension not in allowed_data_extensions:
                flash(
                    f"Unsupported data file '{data_name}'. "
                    "Only CSV, TSV and TXT files are allowed.",
                    "error",
                )
                return redirect(url_for("run_test"))
            prepared_data_files.append((data_file, data_name))

        # Only create a run directory after the request has passed validation.
        test_running = True
        last_test_summary = None
        transaction_stats.clear()
        live_progress_points.clear()
        _clear_test_monitoring_history()
        socketio.emit("run_reset", {"status": "new test"})
        run_dir = make_run_dir()
        current_run_dir = run_dir
        _write_run_project_metadata(run_dir, active_project())

        jmx_path = os.path.join(run_dir, jmx_name)
        jmx_file.save(jmx_path)

        saved_data = []
        for data_file, data_name in prepared_data_files:
            dest = os.path.join(run_dir, data_name)
            data_file.save(dest)
            saved_data.append(dest)

        results_file = os.path.join(run_dir, "results.jtl")
        jmeter_log = os.path.join(run_dir, "jmeter.log")

        try:
            current_process = start_jmeter(jmx_path, saved_data, results_file, jmeter_log)

            if current_process.poll() is not None and current_process.returncode != 0:
                process_log = getattr(current_process, "velocitypulse_output_log", None)
                error_lines = _read_recent_lines(process_log, max_lines=40)
                if not error_lines:
                    error_lines = _read_recent_lines(jmeter_log, max_lines=40)
                test_running = False
                current_process = None
                flash(
                    "JMeter failed to start: "
                    + ("\n".join(error_lines) if error_lines else "Unknown JMeter error"),
                    "error",
                )
                return redirect(url_for("run_test"))

            threading.Thread(target=tail_results, args=(results_file,), daemon=True).start()
            threading.Thread(target=tail_logs, args=(jmeter_log,), daemon=True).start()

            return redirect(url_for("live_progress"))

        except Exception as e:
            test_running = False
            current_process = None
            flash(f"❌ Unexpected error: {str(e)}", "error")
            return redirect(url_for("run_test"))

    return render_template("run_test.html")

def start_jmeter(jmx_path, data_files, results_file, jmeter_log):
    jmeter_exe = os.getenv(
        "JMETER_EXECUTABLE",
        r"C:\apache-jmeter-5.6.3\apache-jmeter-5.6.3\bin\jmeter.bat",
    )
    cmd = [jmeter_exe, "-n", "-t", jmx_path, "-l", results_file, "-j", jmeter_log]
    for idx, data_file in enumerate(data_files, start=1):
        cmd.extend([f"-Jdatafile{idx}", data_file])

    process_log = os.path.join(os.path.dirname(jmeter_log), "jmeter_process.log")
    output_handle = open(process_log, "ab", buffering=0)

    is_windows_batch = (
        os.name == "nt"
        and str(jmeter_exe).lower().endswith((".bat", ".cmd"))
    )
    launch_cmd = subprocess.list2cmdline(cmd) if is_windows_batch else cmd

    try:
        process = subprocess.Popen(
            launch_cmd,
            stdout=output_handle,
            stderr=subprocess.STDOUT,
            shell=is_windows_batch,
        )
    finally:
        output_handle.close()

    process.velocitypulse_output_log = process_log
    return process

@app.route("/live_progress")
def live_progress():
    return render_template("live_progress.html")

@app.route("/live_status")
def live_status():
    """HTTP fallback and page-refresh snapshot for live progress."""
    process_alive = bool(current_process and current_process.poll() is None)
    running = bool(test_running and process_alive)
    return jsonify({
        "running": running,
        "test_running": running,
        "completed": bool(last_test_summary and not running),
        "summary": last_test_summary,
        "results_available": _current_results_available(),
        "metrics": compute_summary(),
        "progress": list(live_progress_points),
        "logs": _current_log_snapshot(),
        "monitoring": {
            "active": bool(monitoring_active and any(thread.is_alive() for thread in monitoring_threads)),
            "servers": sum(1 for thread in monitoring_threads if thread.is_alive()),
            "latest": list(monitoring_latest.values())
        }
    })

transaction_stats = defaultdict(list)

def update_metrics(label, response_time, success):
    transaction_stats[label].append((response_time, success))

def compute_summary():
    summary = []
    for label, records in transaction_stats.items():
        times = [record[0] for record in records]
        successes = [record[1] for record in records]
        samples = len(times)
        if samples == 0:
            continue
        avg = round(sum(times) / samples, 4)
        p90 = round(jmeter_percentile(times, 0.90), 4)
        p95 = round(jmeter_percentile(times, 0.95), 4)
        error_pct = round(100 * (1 - sum(successes) / samples), 4)
        summary.append({
            "label": label,
            "samples": samples,
            "avg": avg,
            "p90": p90,
            "p95": p95,
            "error_pct": error_pct
        })
    return summary

def follow_file(f):
    while True:
        line = f.readline()
        if not line:
            time.sleep(0.5)
            continue
        yield line

def tail_results(results_file):
    global test_running, current_process, last_test_summary
    start_time = time.time()
    wait_until = time.time() + 30
    while not os.path.exists(results_file) and time.time() < wait_until:
        if current_process and current_process.poll() is not None:
            time.sleep(0.5)
            break
        if current_process is None and not test_running:
            break
        time.sleep(0.5)

    if not os.path.exists(results_file):
        test_running = False
        current_process = None
        last_test_summary = {
            "duration": "0 sec",
            "start": "",
            "end": "",
            "users": "N/A",
            "metrics": [],
            "error": "JMeter did not create results.jtl.",
        }
        socketio.emit("test_complete", last_test_summary)
        return

    last_emit = 0.0
    header_map = None
    max_users = 0
    first_sample_ms = None
    last_sample_end_ms = None

    with open(results_file, "r", encoding="utf-8", errors="replace", newline="") as handle:
        while True:
            line = handle.readline()
            if line:
                try:
                    parts = next(csv.reader([line]))
                except csv.Error:
                    continue

                if header_map is None:
                    headers = [str(value).strip() for value in parts]
                    header_map = {name: index for index, name in enumerate(headers)}
                    required = {"timeStamp", "elapsed", "label", "success"}
                    if not required.issubset(header_map):
                        error_message = (
                            "VelocityPulse live parser: JTL header is missing "
                            + ", ".join(sorted(required - set(header_map)))
                        )
                        socketio.emit("log_update", {"line": error_message})
                        while current_process and current_process.poll() is None:
                            time.sleep(0.5)
                        test_running = False
                        current_process = None
                        last_test_summary = {
                            "duration": f"{round(time.time() - start_time, 2)} sec",
                            "start": time.strftime("%H:%M:%S", time.localtime(start_time)),
                            "end": time.strftime("%H:%M:%S", time.localtime(time.time())),
                            "users": "N/A",
                            "metrics": [],
                            "error": error_message,
                        }
                        socketio.emit("test_complete", last_test_summary)
                        return
                    continue

                try:
                    response_time = float(parts[header_map["elapsed"]])
                    label = parts[header_map["label"]]
                    success = (
                        str(parts[header_map["success"]]).strip().lower() == "true"
                    )
                    timestamp = parts[header_map["timeStamp"]]
                    sample_start_ms = float(timestamp)
                    sample_end_ms = sample_start_ms + response_time
                    first_sample_ms = (
                        sample_start_ms
                        if first_sample_ms is None
                        else min(first_sample_ms, sample_start_ms)
                    )
                    last_sample_end_ms = (
                        sample_end_ms
                        if last_sample_end_ms is None
                        else max(last_sample_end_ms, sample_end_ms)
                    )

                    if "allThreads" in header_map:
                        try:
                            max_users = max(
                                max_users,
                                int(float(parts[header_map["allThreads"]])),
                            )
                        except (TypeError, ValueError, IndexError):
                            pass
                except (ValueError, IndexError):
                    continue

                update_metrics(label, response_time, success)

                total_samples = sum(len(records) for records in transaction_stats.values())
                failed_samples = sum(
                    1
                    for records in transaction_stats.values()
                    for _, record_success in records
                    if not record_success
                )
                cumulative_error_rate = (
                    100.0 * failed_samples / total_samples
                    if total_samples
                    else 0.0
                )

                progress_point = {
                    "timestamp": timestamp,
                    "response_time": response_time,
                    "error_rate": round(cumulative_error_rate, 4),
                }
                live_progress_points.append(progress_point)

                now = time.time()
                if now - last_emit >= 0.5:
                    socketio.emit("progress_update", progress_point)
                    socketio.emit("metrics_update", compute_summary())
                    last_emit = now
                continue

            process_alive = bool(current_process and current_process.poll() is None)
            if not process_alive:
                # Allow the OS to flush final JTL lines before completing.
                end_position = handle.tell()
                time.sleep(1)
                handle.seek(0, os.SEEK_END)
                if handle.tell() == end_position:
                    break
                handle.seek(end_position)
            else:
                time.sleep(0.25)

    if first_sample_ms is not None and last_sample_end_ms is not None:
        actual_start = datetime.fromtimestamp(first_sample_ms / 1000.0)
        actual_end = datetime.fromtimestamp(last_sample_end_ms / 1000.0)
        duration_seconds = max(0.0, (last_sample_end_ms - first_sample_ms) / 1000.0)
        summary_start = actual_start.strftime("%H:%M:%S")
        summary_end = actual_end.strftime("%H:%M:%S")
    else:
        duration_seconds = max(0.0, time.time() - start_time)
        summary_start = time.strftime("%H:%M:%S", time.localtime(start_time))
        summary_end = time.strftime("%H:%M:%S", time.localtime(time.time()))

    summary = {
        "duration": f"{duration_seconds:.2f} sec",
        "start": summary_start,
        "end": summary_end,
        "users": max_users if max_users else "N/A",
        "metrics": compute_summary(),
    }
    test_running = False
    current_process = None
    last_test_summary = summary
    socketio.emit("metrics_update", summary["metrics"])
    socketio.emit("test_complete", summary)

def tail_logs(log_file):
    wait_until = time.time() + 30
    while not os.path.exists(log_file) and time.time() < wait_until:
        if current_process and current_process.poll() is not None:
            time.sleep(0.5)
            break
        if current_process is None and not test_running:
            break
        time.sleep(0.5)
    if not os.path.exists(log_file):
        return
    with open(log_file, "r", encoding="utf-8", errors="replace") as f:
        while True:
            line = f.readline()
            if line:
                socketio.emit("log_update", {"line": line.strip()})
            elif current_process and current_process.poll() is None:
                time.sleep(0.5)
            else:
                break

@app.route("/generate_report")
def generate_report():
    global current_run_dir
    run_dir = current_run_dir if current_run_dir and os.path.isdir(current_run_dir) else _latest_run_dir()
    if not run_dir:
        flash("No test run found to generate report.", "error")
        return redirect(url_for("live_progress"))

    latest_run = os.path.basename(run_dir)
    run_project = _read_run_project_metadata(run_dir)
    results_file = os.path.join(run_dir, "results.jtl")

    if not os.path.exists(results_file):
        flash("Results file not found.", "error")
        return redirect(url_for("live_progress"))

    # Use the same defaults as the Home-page report form.
    green, amber, rag_basis = 1.5, 3.5, "avg"
    summary, test_rag = parse_jmeter_csv(results_file, green, amber, rag_basis)
    summary, test_rag = evaluate_sla(summary, green, amber, rag_basis)

    df = pd.read_csv(results_file)
    df['timeStamp'] = pd.to_numeric(df['timeStamp'], errors='coerce').fillna(0).astype(int)
    if not df.empty:
        start_ts, end_ts = df['timeStamp'].min(), df['timeStamp'].max()
        start_dt, end_dt = datetime.fromtimestamp(start_ts/1000.0), datetime.fromtimestamp(end_ts/1000.0)
        test_date = start_dt.strftime("%d-%m-%Y")
        test_period = f"{start_dt.strftime('%d-%m-%Y %H:%M:%S')} to {end_dt.strftime('%d-%m-%Y %H:%M:%S')}"
        total_duration = str(end_dt - start_dt)
        concurrent_users = int(df["allThreads"].max()) if "allThreads" in df.columns else None
        steady_state = "Full test period"
    else:
        test_date = test_period = total_duration = "Not Available"
        concurrent_users = None
        steady_state = "Not Available"

    summary, report_kpis = enrich_summary_and_kpis(df, summary)
    chart_data = build_report_chart_data(df, summary)
    monitoring_stats = summarize_monitoring_history()
    try:
        graph_paths = generate_report_graph_assets(df, summary, green, amber)
    except Exception as exc:
        print("Graph generation failed:", exc)
        graph_paths = {}

    report_data = {
        "report_name": f"Live Test {latest_run}",
        "project_id": run_project.get("project_id"),
        "project_name": run_project.get("project_name"),
        "file_name": os.path.basename(results_file),
        "summary": summary,
        "rag_result": test_rag,
        "observations": build_report_observations(
            summary,
            test_rag,
            chart_data=chart_data,
            report_kpis=report_kpis,
            green_sla=green,
            amber_sla=amber,
            monitoring_stats=monitoring_stats,
        ),
        "observation_engine_version": 3,
        "test_date": test_date,
        "test_period": test_period,
        "total_duration": total_duration,
        "concurrent_users": concurrent_users,
        "steady_state": steady_state,
        "selected_metrics": ["avg", "p90", "p95", "error", "samples"],
        "green": green,
        "amber": amber,
        "rag_basis": rag_basis,
        "include_error": False,
        "error_threshold": None,
        "graph_paths": graph_paths,
        "report_kpis": report_kpis,
        "monitoring_stats": monitoring_stats,
        **chart_data,
        "timestamp": datetime.utcnow().isoformat()
    }

    save_report(report_data)

    return redirect(url_for("report_latest"))

@app.route("/stop_test", methods=["POST"])
def stop_test():
    global current_process, test_running
    if current_process and current_process.poll() is None:
        current_process.terminate()
        try:
            current_process.wait(timeout=3)
        except subprocess.TimeoutExpired:
            current_process.kill()
            current_process.wait(timeout=2)
        except Exception:
            try:
                current_process.kill()
            except Exception:
                pass
        current_process = None
        test_running = False
        socketio.emit("test_stopped", {"status": "stopped", "metrics": compute_summary()})
        flash("🛑 Test stopped successfully", "info")
    else:
        flash("No active test to stop", "warning")
    return redirect(url_for("live_progress"))

import threading, time, psutil, paramiko
from flask import request, jsonify, render_template

# Monitoring globals
monitoring_active = False
monitoring_threads = []
monitoring_latest = {}
monitoring_history = defaultdict(lambda: deque(maxlen=5000))
monitoring_history_lock = threading.Lock()


def _record_monitoring_sample(sample):
    enriched = dict(sample)
    enriched["timestamp_ms"] = int(time.time() * 1000)
    server_name = str(enriched.get("server") or "Unnamed server")
    with monitoring_history_lock:
        monitoring_history[server_name].append(enriched)
    return enriched


def _clear_test_monitoring_history():
    with monitoring_history_lock:
        monitoring_history.clear()


def summarize_monitoring_history():
    """Return per-server monitoring statistics captured during the current test."""
    with monitoring_history_lock:
        snapshot = {
            server: list(samples)
            for server, samples in monitoring_history.items()
            if samples
        }

    summaries = []
    for server, samples in sorted(snapshot.items()):
        cpu_values = [
            float(sample["cpu"])
            for sample in samples
            if sample.get("cpu") is not None
        ]
        mem_values = [
            float(sample["mem"])
            for sample in samples
            if sample.get("mem") is not None
        ]
        if not cpu_values and not mem_values:
            continue

        cpu_high_pct = (
            100.0 * sum(value >= 85.0 for value in cpu_values) / len(cpu_values)
            if cpu_values else 0.0
        )
        mem_high_pct = (
            100.0 * sum(value >= 85.0 for value in mem_values) / len(mem_values)
            if mem_values else 0.0
        )

        avg_cpu = float(np.mean(cpu_values)) if cpu_values else None
        p95_cpu = float(np.percentile(cpu_values, 95)) if cpu_values else None
        max_cpu = max(cpu_values) if cpu_values else None
        avg_mem = float(np.mean(mem_values)) if mem_values else None
        p95_mem = float(np.percentile(mem_values, 95)) if mem_values else None
        max_mem = max(mem_values) if mem_values else None

        red = (
            (avg_cpu is not None and avg_cpu >= 85)
            or (p95_cpu is not None and p95_cpu >= 90)
            or cpu_high_pct >= 20
            or (avg_mem is not None and avg_mem >= 85)
            or (p95_mem is not None and p95_mem >= 90)
            or mem_high_pct >= 20
        )
        amber = (
            (avg_cpu is not None and avg_cpu >= 70)
            or (p95_cpu is not None and p95_cpu >= 80)
            or (max_cpu is not None and max_cpu >= 90)
            or (avg_mem is not None and avg_mem >= 75)
            or (p95_mem is not None and p95_mem >= 85)
            or (max_mem is not None and max_mem >= 90)
        )

        timestamps = [
            int(sample.get("timestamp_ms"))
            for sample in samples
            if sample.get("timestamp_ms") is not None
        ]
        duration_seconds = (
            max(0.0, (max(timestamps) - min(timestamps)) / 1000.0)
            if len(timestamps) >= 2
            else 0.0
        )

        sample0 = samples[0]
        summaries.append({
            "server": server,
            "host": sample0.get("host"),
            "os": sample0.get("os"),
            "samples": max(len(cpu_values), len(mem_values)),
            "duration_seconds": round(duration_seconds, 2),
            "avg_cpu": round(avg_cpu, 2) if avg_cpu is not None else None,
            "p95_cpu": round(p95_cpu, 2) if p95_cpu is not None else None,
            "max_cpu": round(max_cpu, 2) if max_cpu is not None else None,
            "cpu_high_pct": round(cpu_high_pct, 2),
            "avg_mem": round(avg_mem, 2) if avg_mem is not None else None,
            "p95_mem": round(p95_mem, 2) if p95_mem is not None else None,
            "max_mem": round(max_mem, 2) if max_mem is not None else None,
            "mem_high_pct": round(mem_high_pct, 2),
            "status": "RED" if red else ("AMBER" if amber else "GREEN"),
        })

    return summaries

def collect_linux_metrics(host, user, password, name, socketio):
    ssh = paramiko.SSHClient()
    ssh.load_system_host_keys()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    try:
        ssh.connect(host, username=user, password=password, timeout=5)
        while monitoring_active:
            _, cpu_out, _ = ssh.exec_command("vmstat 1 2 | tail -1")
            cpu_fields = cpu_out.read().decode().split()

            _, mem_out, _ = ssh.exec_command(
                "free | awk '/Mem:/ { if ($2 > 0) printf \"%.2f\", ($3/$2)*100; else print 0 }'"
            )
            mem_text = mem_out.read().decode().strip()

            if len(cpu_fields) >= 15:
                cpu = 100.0 - float(cpu_fields[14])
                mem = float(mem_text) if mem_text else 0.0
                sample = _record_monitoring_sample({
                    "server": name,
                    "host": host,
                    "os": "linux",
                    "cpu": round(cpu, 2),
                    "mem": round(mem, 2),
                })
                monitoring_latest[name] = sample
                socketio.emit("server_metrics", sample, namespace="/")
            time.sleep(5)
    except Exception as exc:
        socketio.emit(
            "server_metrics_error",
            {"server": name, "error": str(exc)},
            namespace="/",
        )
    finally:
        ssh.close()

def collect_windows_metrics(host, name, socketio):
    while monitoring_active:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory().percent
        sample = _record_monitoring_sample({
            "server": name,
            "host": host,
            "os": "local",
            "cpu": round(float(cpu), 2),
            "mem": round(float(mem), 2),
        })
        monitoring_latest[name] = sample
        socketio.emit("server_metrics", sample,
    			  namespace="/")
        time.sleep(5)

@app.route("/monitor")
def monitor():
    # Render the server setup page (no JMeter content)
    return render_template("monitor.html")

@app.route("/start_monitoring", methods=["POST"])
def start_monitoring():
    global monitoring_active, monitoring_threads, monitoring_latest

    data = request.get_json(silent=True) or {}
    servers = data.get("servers", [])
    if not servers:
        monitoring_active = False
        monitoring_threads = []
        monitoring_latest = {}
        return jsonify({"status": "no servers provided", "servers": 0}), 400

    monitoring_active = True
    monitoring_threads = []
    monitoring_latest = {}
    errors = []

    for server in servers:
        host = str(server.get("host") or "").strip()
        name = str(server.get("name") or host or "Unnamed server").strip()
        os_type = str(server.get("os") or "").strip().lower()

        if not host:
            errors.append(f"{name}: host is required")
            continue

        try:
            if host.lower() in {"localhost", "127.0.0.1", "::1"}:
                thread = threading.Thread(
                    target=collect_windows_metrics,
                    args=(host, name, socketio),
                    daemon=True,
                )
            elif os_type == "linux":
                thread = threading.Thread(
                    target=collect_linux_metrics,
                    args=(
                        host,
                        server.get("user"),
                        server.get("password"),
                        name,
                        socketio,
                    ),
                    daemon=True,
                )
            elif os_type == "windows":
                # psutil can only inspect the machine on which VelocityPulse runs.
                # Do not label local metrics as a remote Windows host.
                errors.append(
                    f"{name}: remote Windows monitoring is not configured; "
                    "use localhost or configure a remote metrics transport."
                )
                continue
            else:
                errors.append(f"{name}: unsupported OS type '{os_type or 'missing'}'")
                continue

            thread.start()
            monitoring_threads.append(thread)
        except Exception as exc:
            errors.append(f"{name}: {exc}")

    if not monitoring_threads:
        monitoring_active = False
        return jsonify({
            "status": "monitoring not started",
            "servers": 0,
            "errors": errors,
        }), 400

    return jsonify({
        "status": "monitoring started",
        "servers": len(monitoring_threads),
        "errors": errors,
    })

@app.route("/stop_monitoring", methods=["POST"])
def stop_monitoring():
    global monitoring_active, monitoring_threads, monitoring_latest
    monitoring_active = False

    print("🛑 Stopping monitoring...")

    for t in monitoring_threads:
        try:
            if t.is_alive():
                t.join(timeout=1)
        except Exception as e:
            print(f"⚠️ Error stopping thread: {e}")

    monitoring_threads.clear()
    monitoring_latest = {}
    print("✅ Monitoring stopped")

    return jsonify({"status": "monitoring stopped"})

@app.route("/monitor_status", methods=["GET"])
def monitor_status():
    live_threads = [thread for thread in monitoring_threads if thread.is_alive()]
    return jsonify({
        "active": bool(monitoring_active and live_threads),
        "servers": len(live_threads),
        "latest": list(monitoring_latest.values()),
    })


@app.route("/test_connection", methods=["POST"])
def test_connection():
    data = request.get_json(silent=True) or {}
    host = data.get("host")
    os_type = data.get("os")
    user = data.get("user")
    password = data.get("password")

    # Localhost shortcut
    if host in ["localhost", "127.0.0.1"]:
        return jsonify({"status": "✅ Localhost reachable without credentials"})

    try:
        if os_type == "linux":
            ssh = paramiko.SSHClient()
            ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
            ssh.connect(host, username=user, password=password, timeout=5)
            ssh.close()
            return jsonify({"status": f"✅ Connected to {host}"})
        elif os_type == "windows":
            return jsonify({
                "status": (
                    "Remote Windows monitoring is not configured. "
                    "VelocityPulse cannot validate a remote Windows host using local psutil."
                )
            }), 501
        else:
            return jsonify({"status": "⚠️ Unknown OS type"})
    except Exception as e:
        return jsonify({"status": f"❌ Connection failed: {str(e)}"})

if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=5000, debug=True)
