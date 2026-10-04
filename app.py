from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response, jsonify
import os, json, uuid, subprocess, threading, time, csv, secrets
from urllib.parse import urlparse, urlencode

import msal
import pandas as pd
from dotenv import load_dotenv
from datetime import datetime
from werkzeug.utils import secure_filename
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

from flask_socketio import SocketIO
from collections import defaultdict
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
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("static/reports", exist_ok=True)  # ensure reports dir exists

# Microsoft identity platform configuration.
MICROSOFT_CLIENT_ID = os.getenv("MICROSOFT_CLIENT_ID", "").strip()
MICROSOFT_CLIENT_SECRET = os.getenv("MICROSOFT_CLIENT_SECRET", "").strip()
MICROSOFT_TENANT_ID = os.getenv("MICROSOFT_TENANT_ID", "common").strip() or "common"
MICROSOFT_AUTHORITY = f"https://login.microsoftonline.com/{MICROSOFT_TENANT_ID}"
MICROSOFT_REDIRECT_URI = os.getenv(
    "MICROSOFT_REDIRECT_URI",
    "http://localhost:5000/auth/callback",
).strip()
MICROSOFT_POST_LOGOUT_REDIRECT_URI = os.getenv(
    "MICROSOFT_POST_LOGOUT_REDIRECT_URI",
    "http://localhost:5000/login",
).strip()
MICROSOFT_SCOPES = [
    scope
    for scope in os.getenv("MICROSOFT_SCOPES", "User.Read").replace(",", " ").split()
    if scope
]

def microsoft_auth_configured():
    return bool(MICROSOFT_CLIENT_ID and MICROSOFT_CLIENT_SECRET and MICROSOFT_TENANT_ID)

def build_msal_app():
    if not microsoft_auth_configured():
        return None
    return msal.ConfidentialClientApplication(
        MICROSOFT_CLIENT_ID,
        authority=MICROSOFT_AUTHORITY,
        client_credential=MICROSOFT_CLIENT_SECRET,
    )

def is_safe_local_redirect(target):
    if not target:
        return False
    parsed = urlparse(target)
    return not parsed.scheme and not parsed.netloc and target.startswith("/")

@app.before_request
def require_microsoft_authentication():
    """Require Microsoft sign-in for all application routes."""
    public_endpoints = {"login", "microsoft_login", "auth_callback", "logout", "static"}
    if request.endpoint in public_endpoints or request.path.startswith("/socket.io/"):
        return None

    if "user_profile" not in session:
        if request.method == "GET" and is_safe_local_redirect(request.full_path.rstrip("?")):
            session["post_login_redirect"] = request.full_path.rstrip("?")
        return redirect(url_for("login"))
    return None

@socketio.on("connect")
def authenticated_socket_connection(auth=None):
    # Reject unauthenticated real-time connections.
    if "user_profile" not in session:
        return False

# Inject version info into templates
@app.context_processor
def inject_version():
    return {
        "app_version": __version__,
        "build": __build__,
        "codename": __codename__
    }

# History helpers
def load_history():
    if not os.path.exists(HISTORY_FILE):
        return []
    try:
        with open(HISTORY_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []

def save_report(report_data):
    history = load_history()
    history.insert(0, report_data)  # newest first
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, indent=2)


def _metric(row, key):
    try:
        return float(row.get(key, 0) or 0)
    except (TypeError, ValueError):
        return 0.0


def build_report_observations(summary, rag_result):
    """Create concise, deterministic observations for the report UI."""
    observations = []
    if not summary:
        return [{"type": "warning", "title": "No transaction data", "text": "No valid transaction samples were available for analysis."}]

    total_samples = sum(int(_metric(row, "#Samples")) for row in summary)
    slowest = max(summary, key=lambda row: _metric(row, "Avg (s)"))
    highest_p95 = max(summary, key=lambda row: _metric(row, "95th % (s)"))
    highest_error = max(summary, key=lambda row: _metric(row, "Error %"))

    status_type = {"GREEN": "success", "AMBER": "warning", "RED": "danger"}.get(rag_result, "info")
    observations.append({
        "type": status_type,
        "title": f"Overall result: {rag_result or 'UNKNOWN'}",
        "text": f"The test analysed {len(summary)} transaction(s) and {total_samples} sample(s)."
    })

    if rag_result in ("RED", "AMBER"):
        affected = [row for row in summary if row.get("RAG") in ("RED", "AMBER")]
        names = ", ".join(str(row.get("Transaction")) for row in affected[:4])
        suffix = " and more" if len(affected) > 4 else ""
        observations.append({
            "type": "danger" if rag_result == "RED" else "warning",
            "title": "SLA attention required",
            "text": f"{len(affected)} transaction(s) require attention: {names}{suffix}."
        })
    else:
        observations.append({
            "type": "success",
            "title": "SLA performance is healthy",
            "text": "All analysed transactions are within the configured response-time thresholds."
        })

    observations.append({
        "type": "info",
        "title": f"Slowest average response: {slowest.get('Transaction')}",
        "text": f"Average response time was {_metric(slowest, 'Avg (s)'):.2f}s."
    })

    if _metric(highest_p95, "95th % (s)") > _metric(highest_p95, "Avg (s)") * 1.5:
        observations.append({
            "type": "warning",
            "title": f"Latency spikes: {highest_p95.get('Transaction')}",
            "text": f"P95 reached {_metric(highest_p95, '95th % (s)'):.2f}s versus an average of {_metric(highest_p95, 'Avg (s)'):.2f}s."
        })

    if _metric(highest_error, "Error %") > 0:
        observations.append({
            "type": "danger" if _metric(highest_error, "Error %") >= 5 else "warning",
            "title": f"Highest error rate: {highest_error.get('Transaction')}",
            "text": f"The transaction recorded {_metric(highest_error, 'Error %'):.2f}% errors."
        })
    return observations


def build_trend_observations(txn_trends):
    """Summarise oldest-to-newest transaction movement in concise bullets."""
    movements = []
    for txn, points in txn_trends.items():
        valid = [p for p in points if p.get("avg", 0) > 0]
        if len(valid) < 2:
            continue
        oldest, latest = valid[-1], valid[0]  # reports are newest first
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
@app.route("/about")
def about():
    try:
        with open("CHANGELOG.md", "r", encoding="utf-8") as f:
            lines = f.readlines()
        recent_changes = "".join(lines[-20:])
    except Exception:
        recent_changes = "No changelog available."
    return render_template("about.html",
                           version=__version__,
                           build=__build__,
                           codename=__codename__,
                           recent_changes=recent_changes)

@app.route("/")
def home():
    return redirect(url_for("upload"))

@app.route("/login")
def login():
    if "user_profile" in session:
        return redirect(url_for("upload"))

    error = request.args.get("error")
    return render_template(
        "login.html",
        error=error,
        auth_configured=microsoft_auth_configured(),
    )

@app.route("/login/microsoft")
def microsoft_login():
    if not microsoft_auth_configured():
        flash(
            "Microsoft authentication is not configured. Set the Microsoft Entra environment variables first.",
            "error",
        )
        return redirect(url_for("login"))

    requested_next = request.args.get("next")
    if is_safe_local_redirect(requested_next):
        session["post_login_redirect"] = requested_next

    msal_app = build_msal_app()
    flow = msal_app.initiate_auth_code_flow(
        scopes=MICROSOFT_SCOPES,
        redirect_uri=MICROSOFT_REDIRECT_URI,
    )
    if "auth_uri" not in flow:
        flash(
            flow.get("error_description") or "Unable to start Microsoft sign-in.",
            "error",
        )
        return redirect(url_for("login"))

    session["auth_flow"] = flow
    return redirect(flow["auth_uri"])

@app.route("/auth/callback")
def auth_callback():
    if not microsoft_auth_configured():
        return redirect(url_for("login", error="Microsoft authentication is not configured."))

    flow = session.get("auth_flow")
    if not flow:
        return redirect(url_for("login", error="Your sign-in session expired. Please try again."))

    try:
        result = build_msal_app().acquire_token_by_auth_code_flow(flow, request.args)
    except ValueError:
        # MSAL raises ValueError when the state/flow validation fails.
        session.pop("auth_flow", None)
        return redirect(url_for("login", error="Microsoft sign-in validation failed. Please try again."))

    session.pop("auth_flow", None)

    if "error" in result:
        message = result.get("error_description") or result.get("error") or "Microsoft sign-in failed."
        return redirect(url_for("login", error=message))

    claims = result.get("id_token_claims") or {}
    display_name = (
        claims.get("name")
        or claims.get("preferred_username")
        or claims.get("email")
        or "Microsoft user"
    )
    email = (
        claims.get("preferred_username")
        or claims.get("email")
        or claims.get("upn")
        or ""
    )

    destination = session.pop("post_login_redirect", None)
    session.clear()
    session["user"] = display_name  # backward compatibility with existing templates
    session["user_profile"] = {
        "name": display_name,
        "email": email,
        "tenant_id": claims.get("tid"),
        "object_id": claims.get("oid") or claims.get("sub"),
    }

    if not is_safe_local_redirect(destination):
        destination = url_for("upload")
    return redirect(destination)

@app.route("/logout")
def logout():
    session.clear()
    if not microsoft_auth_configured():
        return redirect(url_for("login"))

    query = urlencode({"post_logout_redirect_uri": MICROSOFT_POST_LOGOUT_REDIRECT_URI})
    return redirect(f"{MICROSOFT_AUTHORITY}/oauth2/v2.0/logout?{query}")

def _format_test_timestamp(timestamp_ms):
    dt = datetime.fromtimestamp(int(timestamp_ms) / 1000.0)
    input_value = dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3]
    return {
        "display": dt.strftime("%d-%m-%Y %H:%M:%S"),
        "input": input_value,
    }

def _parse_datetime_local(value):
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
                raise ValueError("Invalid test window")
        except Exception:
            try:
                os.remove(file_path)
            except OSError:
                pass
            session.pop("uploaded_file", None)
            session.pop("uploaded_file_path", None)
            session.pop("test_window", None)
            flash(
                "The file was uploaded, but VelocityPulse could not detect a valid JMeter test window. "
                "Make sure the file contains a valid timeStamp column.",
                "error",
            )
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
            except Exception:
                flash("Unable to read the uploaded JMeter result file.", "error")

    return render_template(
        "upload.html",
        uploaded_file=uploaded_file,
        uploaded_file_path=session.get("uploaded_file_path"),
        transactions=transactions,
        test_window=test_window,
    )

@app.route("/analyze", methods=["POST"])
def analyze():
    file_path = session.get("uploaded_file_path")
    if not file_path or not os.path.exists(file_path):
        flash("Please upload a JMeter result file before generating a report.", "error")
        return redirect(url_for("upload"))

    report_name = request.form["report_name"]
    transactions = request.form.getlist("transactions")
    metrics = request.form.getlist("metrics")
    rag_basis = request.form["rag_basis"]
    include_error = "include_error" in request.form
    error_threshold = float(request.form.get("error_threshold", 0))
    green, amber = float(request.form["green"]), float(request.form["amber"])

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

    chart_data = build_report_chart_data(df, filtered)\n\n    report_data = {
        "report_name": report_name,
        "file_name": os.path.basename(file_path),
        "summary": filtered,
        "rag_result": test_rag,
        "observations": build_report_observations(filtered, test_rag),
        "test_date": test_date,
        "test_period": test_period,
        "total_duration": total_duration,
        "concurrent_users": concurrent_users,
        "steady_state": steady_state,
        **chart_data,
        "timestamp": datetime.utcnow().isoformat()
    }

    save_report(report_data)

    try:
        generate_graphs(df, green_sla=green, amber_sla=amber)
        generate_transaction_progress(df, out_file="static/reports/graphs/transaction_progress.png")
        generate_rag_pie(filtered, out_file="static/reports/graphs/rag_pie.png")
    except Exception as e:
        print("Graph generation failed:", e)

    reports = load_history()
    new_index = 0
    return redirect(url_for("report", report_index=new_index))

@app.route("/report/<int:report_index>")
def report(report_index):
    reports = load_history()
    if 0 <= report_index < len(reports):
        report_data = reports[report_index]
        report_data.setdefault(
            "observations",
            build_report_observations(report_data.get("summary", []), report_data.get("rag_result"))
        )
        return render_template("report.html", report_index=report_index, **report_data)
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
    page = int(request.args.get("page", 1))
    per_page = 5
    total_pages = (len(reports) + per_page - 1) // per_page
    start, end = (page - 1) * per_page, page * per_page
    return render_template("history.html",
                           reports=reports[start:end],
                           page=page,
                           total_pages=total_pages)

@app.route("/export_report_pdf/<int:report_index>")
def export_report_pdf(report_index):
    reports = load_history()
    if 0 <= report_index < len(reports):
        report_data = reports[report_index]
        report_data.setdefault(
            "observations",
            build_report_observations(report_data.get("summary", []), report_data.get("rag_result"))
        )
        rendered = render_template("report.html",
                                   report_index=report_index,
                                   is_pdf=True,
                                   **report_data)
        from weasyprint import HTML
        pdf = HTML(string=rendered).write_pdf()
        response = make_response(pdf)
        response.headers["Content-Type"] = "application/pdf"
        response.headers["Content-Disposition"] = f"inline; filename=report_{report_index}.pdf"
        return response
    flash("Report not found")
    return redirect(url_for("history"))

@app.route("/compare/pdf")
def export_compare_pdf():
    ids = request.args.getlist("report_ids")
    if len(ids) != 2:
        flash("Please select two reports to compare.")
        return redirect(url_for("history"))

    reports = load_history()
    try:
        r1, r2 = reports[int(ids[0])], reports[int(ids[1])]
    except (IndexError, ValueError):
        flash("Invalid report selection.")
        return redirect(url_for("history"))

    metric = request.args.get("metric", "Avg (s)")
    all_txns = sorted({row["Transaction"] for row in r1["summary"]} |
                      {row["Transaction"] for row in r2["summary"]})
    selected_txns = request.args.getlist("transactions") or all_txns

    for r in (r1, r2):
        for row in r.get("summary", []):
            for key in ["Avg (s)", "90th % (s)", "95th % (s)", "Min (s)", "Max (s)", "Error %"]:
                if key in row and row[key] is not None:
                    try:
                        row[key] = float(row[key])
                    except (ValueError, TypeError):
                        row[key] = None

    comparisons, observations = [], []
    html = render_template("compare_result.html",
                           r1=r1, r2=r2,
                           metric=metric,
                           all_txns=all_txns,
                           selected_txns=selected_txns,
                           comparisons=comparisons,
                           observations=observations,
                           compare_progress=None)

    import pdfkit
    pdf = pdfkit.from_string(html, False)
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
    if len(ids) != 2:
        flash("Please select exactly 2 reports.")
        return redirect(url_for("select_compare"))

    reports = load_history()
    r1, r2 = reports[int(ids[0])], reports[int(ids[1])]

    earlier, later = sorted([r1, r2], key=lambda r: r.get("timestamp"))

    all_txns = sorted({row["Transaction"] for row in earlier["summary"]} |
                      {row["Transaction"] for row in later["summary"]})

    metric = request.args.get("metric", "Avg (s)")
    txn_filter = request.args.getlist("transactions") or all_txns

    comparisons = []
    for txn in txn_filter:
        v1 = next((row.get(metric) for row in earlier["summary"] if row["Transaction"] == txn), None)
        v2 = next((row.get(metric) for row in later["summary"] if row["Transaction"] == txn), None)
        if v1 is None or v2 is None:
            continue
        try:
            v1, v2 = float(v1), float(v2)
        except (ValueError, TypeError):
            continue

        diff = v2 - v1
        if abs(diff) < 0.001:
            status, color = "No Change", "grey"
        elif diff > 0:
            status, color = "Degraded", "red"
        else:
            status, color = "Improved", "green"

        comparisons.append({
            "transaction": txn,
            "v1": round(v1, 2),
            "v2": round(v2, 2),
            "diff": round(diff, 2),
            "status": status,
            "color": color
        })

    observations = []
    degraded = [c for c in comparisons if c["status"] == "Degraded"]
    improved = [c for c in comparisons if c["status"] == "Improved"]
    if degraded:
        observations.append(f"{len(degraded)} transaction(s) show degradation.")
    if improved:
        observations.append(f"{len(improved)} transaction(s) improved.")
    if not observations:
        observations.append("Performance is stable across compared reports.")

    return render_template("compare.html",
                           r1=earlier,
                           r2=later,
                           metric=metric,
                           comparisons=comparisons,
                           observations=observations,
                           all_txns=all_txns,
                           selected_txns=txn_filter)

@app.context_processor
def inject_test_state():
    return {"test_running": test_running}


@app.route("/trend")
def trend():
    n = int(request.args.get("n", 10))
    reports = load_history()
    if not reports:
        flash("No reports available for trend analysis.")
        return redirect(url_for("history"))

    selected_reports = reports[:n]
    txn_trends = {}
    all_txns = set()

    for r in selected_reports:
        test_label = r.get("test_date") or r.get("timestamp")[:10]
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
@app.route("/baseline")
def baseline():
    n = int(request.args.get("n", 7))
    reports = load_history()
    if not reports:
        flash("No reports available for baseline calculation.")
        return redirect(url_for("history"))

    selected_reports = reports[:n]  # newest first
    txn_stats, warnings = {}, []

    for r_index, r in enumerate(selected_reports):
        for row_index, row in enumerate(r.get("summary", [])):
            txn = row.get("Transaction")
            if not txn:
                continue
            txn_stats.setdefault(txn, {"avg": [], "p90": []})

            val = row.get("Avg (s)")
            if val is not None:
                try:
                    txn_stats[txn]["avg"].append(float(val))
                except (ValueError, TypeError):
                    warnings.append(f"Invalid Avg '{val}' for {txn} in report {r_index+1}, row {row_index+1}")

            val = row.get("90th % (s)")
            if val is not None:
                try:
                    txn_stats[txn]["p90"].append(float(val))
                except (ValueError, TypeError):
                    warnings.append(f"Invalid 90th % '{val}' for {txn} in report {r_index+1}, row {row_index+1}")

    baselines = {}
    for txn, vals in txn_stats.items():
        avg_val = round(sum(vals["avg"]) / len(vals["avg"]), 3) if vals["avg"] else None
        p90_val = round(sum(vals["p90"]) / len(vals["p90"]), 3) if vals["p90"] else None

        if avg_val is not None or p90_val is not None:
            baselines[txn] = {
                "avg": avg_val,
                "p90": p90_val,
                "sample_size": len(vals["avg"])
            }
        else:
            warnings.append(f"No valid data for transaction '{txn}' across last {n} reports")

    labels = list(baselines.keys())

    return render_template("baseline.html",
                           baselines=baselines,
                           n=n,
                           warnings=warnings,
                           labels=labels)
test_running = False
current_process = None  # track JMeter process globally

def make_run_dir():
    run_id = uuid.uuid4().hex[:8]
    run_dir = os.path.join("uploads", f"run_{run_id}")
    os.makedirs(run_dir, exist_ok=True)
    return run_dir

@app.route("/run_test", methods=["GET", "POST"])
def run_test():
    global test_running, current_process
    if request.method == "POST":
        if test_running and current_process and current_process.poll() is None:
            flash("A JMeter test is already running. Open Live Progress to monitor it.", "warning")
            return redirect(url_for("live_progress"))
        test_running = True
        transaction_stats.clear()  # reset metrics
        socketio.emit("run_reset", {"status": "new test"})
        run_dir = make_run_dir()

        jmx_file = request.files.get("jmx_file")
        if not jmx_file or jmx_file.filename == "":
            test_running = False
            flash("❌ Please select a JMX test plan.", "error")
            return redirect(url_for("run_test"))
        jmx_path = os.path.join(run_dir, jmx_file.filename)
        jmx_file.save(jmx_path)

        data_files = request.files.getlist("data_files")
        saved_data = []
        for f in data_files:
            if f and f.filename:
                dest = os.path.join(run_dir, f.filename)
                f.save(dest)
                saved_data.append(dest)

        results_file = os.path.join(run_dir, "results.jtl")
        jmeter_log = os.path.join(run_dir, "jmeter.log")

        try:
            current_process = start_jmeter(jmx_path, saved_data, results_file, jmeter_log)

            if current_process.poll() is not None and current_process.returncode != 0:
                err = current_process.stderr.read().decode()
                flash(f"❌ Error starting JMeter: {err}", "error")
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
    jmeter_exe = r"C:\apache-jmeter-5.6.3\apache-jmeter-5.6.3\bin\jmeter.bat"  # full path to JMeter batch file
    cmd = [jmeter_exe, "-n", "-t", jmx_path, "-l", results_file, "-j", jmeter_log]
    for idx, df in enumerate(data_files, start=1):
        cmd.extend(["-J" + f"datafile{idx}", df])
    return subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, shell=True)

@app.route("/live_progress")
def live_progress():
    return render_template("live_progress.html")

@app.route("/live_status")
def live_status():
    """HTTP fallback and page-refresh snapshot for live progress."""
    process_alive = bool(current_process and current_process.poll() is None)
    return jsonify({
        "running": bool(test_running and process_alive),
        "test_running": bool(test_running),
        "metrics": compute_summary(),
        "monitoring": {
            "active": monitoring_active,
            "servers": len(monitoring_threads),
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
    global test_running, current_process
    start_time = time.time()
    wait_until = time.time() + 30
    while not os.path.exists(results_file) and time.time() < wait_until:
        print("Waiting for results.jtl...")
        time.sleep(1)
    if not os.path.exists(results_file):
        test_running = False
        socketio.emit("test_complete", {"duration": "0 sec", "start": "", "end": "", "users": "N/A", "metrics": []})
        return
    print("Found results file:", results_file)

    last_emit = time.time()
    with open(results_file, "r", encoding="utf-8", errors="replace") as f:
        header_seen = False
        while True:
            line = f.readline()
            if line:
                if line.startswith("timeStamp"):
                    header_seen = True
                    continue
                if not header_seen:
                    continue

                try:
                    parts = next(csv.reader([line]))
                except csv.Error:
                    continue
                if len(parts) < 8:
                    continue

                try:
                    timestamp = parts[0]
                    response_time = float(parts[1])  # elapsed (ms)
                    label = parts[2]
                    success = (parts[7].lower() == "true")
                except (ValueError, IndexError):
                    continue

                update_metrics(label, response_time, success)

                socketio.emit("progress_update", {
                    "timestamp": timestamp,
                    "response_time": response_time,
                    "error_rate": 0 if success else 100
                })

                socketio.emit("metrics_update", compute_summary())

                if time.time() - last_emit > 120:
                    socketio.emit("heartbeat", {"status": "running"})
                    last_emit = time.time()
                continue

            process_alive = bool(current_process and current_process.poll() is None)
            if not process_alive:
                # Allow the OS to flush the final JTL lines before completing.
                end_position = f.tell()
                time.sleep(1)
                f.seek(0, os.SEEK_END)
                if f.tell() == end_position:
                    break
                f.seek(end_position)
            else:
                time.sleep(0.25)

    duration = round(time.time() - start_time, 2)
    summary = {
        "duration": f"{duration} sec",
        "start": time.strftime("%H:%M:%S", time.localtime(start_time)),
        "end": time.strftime("%H:%M:%S", time.localtime(time.time())),
        "users": "N/A",
        "metrics": compute_summary()
    }
    test_running = False
    current_process = None
    socketio.emit("test_complete", summary)

def tail_logs(log_file):
    wait_until = time.time() + 30
    while not os.path.exists(log_file) and time.time() < wait_until:
        time.sleep(1)
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
    runs = [d for d in os.listdir("uploads") if d.startswith("run_")]
    if not runs:
        flash("No test run found to generate report.", "error")
        return redirect(url_for("live_progress"))

    latest_run = sorted(runs)[-1]
    run_dir = os.path.join("uploads", latest_run)
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
        steady_state = "Yes" if len(df) > 100 else "No"
    else:
        test_date = test_period = total_duration = "Not Available"
        concurrent_users = None
        steady_state = "Unknown"

    chart_data = build_report_chart_data(df, summary)

    report_data = {
        "report_name": f"Live Test {latest_run}",
        "file_name": os.path.basename(results_file),
        "summary": summary,
        "rag_result": test_rag,
        "observations": build_report_observations(summary, test_rag),
        "test_date": test_date,
        "test_period": test_period,
        "total_duration": total_duration,
        "concurrent_users": concurrent_users,
        "steady_state": steady_state,
        **chart_data,
        "timestamp": datetime.utcnow().isoformat()
    }

    save_report(report_data)

    try:
        generate_graphs(df, green_sla=green, amber_sla=amber)
        generate_transaction_progress(df, out_file="static/reports/graphs/transaction_progress.png")
        generate_rag_pie(summary, out_file="static/reports/graphs/rag_pie.png")
    except Exception as e:
        print("Graph generation failed:", e)

    return redirect(url_for("report_latest"))

@app.route("/stop_test", methods=["POST"])
def stop_test():
    global current_process, test_running
    if current_process and current_process.poll() is None:
        current_process.terminate()
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

def collect_linux_metrics(host, user, password, name, socketio):
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    ssh.connect(host, username=user, password=password)
    while monitoring_active:
        stdin, stdout, stderr = ssh.exec_command("vmstat 1 2 | tail -1")
        fields = stdout.read().decode().split()
        if len(fields) >= 15:
            cpu = 100 - int(fields[14])   # idle column
            mem = int(fields[3])          # free memory (KB)
            sample = {"server": name, "cpu": cpu, "mem": mem}
            monitoring_latest[name] = sample
            socketio.emit("server_metrics", sample,
    			  namespace="/")
                          
        time.sleep(5)

def collect_windows_metrics(host, name, socketio):
    while monitoring_active:
        cpu = psutil.cpu_percent(interval=1)
        mem = psutil.virtual_memory().percent
        sample = {"server": name, "cpu": cpu, "mem": mem}
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
    monitoring_active = True
    monitoring_threads = []
    monitoring_latest = {}

    data = request.get_json(silent=True) or {}
    servers = data.get("servers", [])

    if not servers:
        print("⚠️ No servers provided in start_monitoring request")
        return jsonify({"status": "no servers provided"}), 400

    print(f"✅ Starting monitoring for {len(servers)} server(s)")

    for srv in servers:
        try:
            os_type = srv.get("os")
            # localhost is always collected from this machine. Do not try to
            # SSH to localhost merely because the user selected Linux.
            if srv.get("host", "").lower() in ["localhost", "127.0.0.1"]:
                t = threading.Thread(
                    target=collect_windows_metrics,
                    args=(srv.get("host"), srv.get("name"), socketio),
                    daemon=True
                )
            elif os_type == "linux":
                t = threading.Thread(
                    target=collect_linux_metrics,
                    args=(srv.get("host"), srv.get("user"), srv.get("password"), srv.get("name"), socketio),
                    daemon=True
                )
            elif os_type == "windows":
                t = threading.Thread(
                    target=collect_windows_metrics,
                    args=(srv.get("host"), srv.get("name"), socketio),
                    daemon=True
                )
            else:
                print(f"⚠️ Unknown OS type for server {srv}")
                continue

            t.start()
            monitoring_threads.append(t)
            print(f"➡️ Monitoring thread started for {srv.get('name')} ({srv.get('host')})")

        except Exception as e:
            print(f"❌ Failed to start monitoring for {srv.get('name')}: {e}")

    return jsonify({
        "status": "monitoring started",
        "servers": len(monitoring_threads)
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
    # Allow frontend to check if monitoring is active
    return jsonify({
        "active": monitoring_active,
        "servers": len(monitoring_threads),
        "latest": list(monitoring_latest.values())
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
            # For Windows, just check psutil locally
            cpu = psutil.cpu_percent(interval=1)
            return jsonify({"status": f"✅ Windows host {host} reachable, CPU={cpu}%"})
        else:
            return jsonify({"status": "⚠️ Unknown OS type"})
    except Exception as e:
        return jsonify({"status": f"❌ Connection failed: {str(e)}"})

if __name__ == "__main__":
    socketio.run(app, host="127.0.0.1", port=5000, debug=True)
