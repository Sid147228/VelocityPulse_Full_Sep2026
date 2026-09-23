from flask import Flask, render_template, request, redirect, url_for, session, flash, make_response, jsonify
import os, json, uuid, subprocess, threading, time, csv
import pandas as pd
from datetime import datetime
from werkzeug.utils import secure_filename
from version import __version__, __build__, __codename__

# Helpers
from jmeter_parser import parse_jmeter_csv
from generate_TestResult import evaluate_sla
from generate_graphs import generate_graphs
from generate_transaction_progress import generate_transaction_progress
from generate_rag_pie import generate_rag_pie

from flask_socketio import SocketIO
from collections import defaultdict
import numpy as np

# Flask app and Socket.IO
app = Flask(__name__)
app.secret_key = "velocitypulse_secret"
socketio = SocketIO(app)

UPLOAD_FOLDER = "uploads"
HISTORY_FILE = "static/reports/history.json"
os.makedirs(UPLOAD_FOLDER, exist_ok=True)
os.makedirs("static/reports", exist_ok=True)  # ensure reports dir exists

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


def build_report_chart_data(df, summary):
    """Build the chart payload expected by the shared report.html template.

    Both uploaded CSV reports and live JMeter reports use the same report
    template. Keeping this payload construction in one helper prevents live
    reports from losing RAG distribution and time-series charts.
    """
    chart_df = df.copy()
    chart_df.columns = [str(c).strip().lower() for c in chart_df.columns]

    if "timestamp" in chart_df.columns:
        chart_df["timestamp"] = pd.to_datetime(
            chart_df["timestamp"], unit="ms", errors="coerce"
        )
    elif "timestamp" not in chart_df.columns and "timestamp" in df.columns:
        chart_df["timestamp"] = pd.to_datetime(df["timestamp"], errors="coerce")

    if "elapsed" in chart_df.columns:
        chart_df["elapsed"] = pd.to_numeric(chart_df["elapsed"], errors="coerce")
    if "success" in chart_df.columns:
        chart_df["success"] = chart_df["success"].astype(str).str.lower().isin(["true", "1"])
    else:
        chart_df["success"] = True

    if "label" not in chart_df.columns or "timestamp" not in chart_df.columns:
        return {
            "rag_counts": {
                "GREEN": sum(1 for row in summary if row.get("RAG") == "GREEN"),
                "AMBER": sum(1 for row in summary if row.get("RAG") == "AMBER"),
                "RED": sum(1 for row in summary if row.get("RAG") == "RED"),
            },
            "chart_time_labels": [],
            "series_avg_by_txn": {},
            "series_p90_by_txn": {},
            "series_error_rate_by_txn": {},
            "series_throughput_over_time": [],
            "labels": [row.get("Transaction") for row in summary],
            "avg_values": [row.get("Avg (s)") for row in summary],
            "p90_values": [row.get("90th % (s)") for row in summary],
            "p95_values": [row.get("95th % (s)") for row in summary],
            "error_values": [row.get("Error %") for row in summary],
        }

    chart_df = chart_df.dropna(subset=["timestamp"]).sort_values("timestamp")
    time_index = chart_df["timestamp"].dt.floor("min")
    time_labels = sorted(time_index.dropna().unique())
    labels_fmt = [ts.strftime("%H:%M") for ts in time_labels]

    series_avg_by_txn, series_p90_by_txn, series_error_rate_by_txn = {}, {}, {}
    if "elapsed" in chart_df.columns:
        for txn, group in chart_df.groupby("label"):
            grouped = group.groupby(group["timestamp"].dt.floor("min"))
            avg_ms = grouped["elapsed"].mean()
            p90_ms = grouped["elapsed"].quantile(0.90)
            error_rate = grouped["success"].apply(lambda values: 100.0 * ((~values).sum() / len(values)))
            series_avg_by_txn[txn] = [
                round(avg_ms.get(t) / 1000.0, 3) if pd.notnull(avg_ms.get(t)) else None
                for t in time_labels
            ]
            series_p90_by_txn[txn] = [
                round(p90_ms.get(t) / 1000.0, 3) if pd.notnull(p90_ms.get(t)) else None
                for t in time_labels
            ]
            series_error_rate_by_txn[txn] = [
                round(error_rate.get(t), 3) if pd.notnull(error_rate.get(t)) else None
                for t in time_labels
            ]

    throughput = chart_df.groupby(chart_df["timestamp"].dt.floor("min")).size()
    return {
        "rag_counts": {
            "GREEN": sum(1 for row in summary if row.get("RAG") == "GREEN"),
            "AMBER": sum(1 for row in summary if row.get("RAG") == "AMBER"),
            "RED": sum(1 for row in summary if row.get("RAG") == "RED"),
        },
        "chart_time_labels": labels_fmt,
        "series_avg_by_txn": series_avg_by_txn,
        "series_p90_by_txn": series_p90_by_txn,
        "series_error_rate_by_txn": series_error_rate_by_txn,
        "series_throughput_over_time": [int(throughput.get(t, 0)) for t in time_labels],
        "labels": [row.get("Transaction") for row in summary],
        "avg_values": [row.get("Avg (s)") for row in summary],
        "p90_values": [row.get("90th % (s)") for row in summary],
        "p95_values": [row.get("95th % (s)") for row in summary],
        "error_values": [row.get("Error %") for row in summary],
    }
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

@app.route("/login", methods=["GET", "POST"])
def login():
    if request.method == "POST":
        user = request.form.get("username")
        if user:
            session["user"] = user
            return redirect(url_for("upload"))
        else:
            flash("Username required")
    return render_template("login.html")

@app.route("/logout")
def logout():
    session.clear()
    return redirect(url_for("login"))

@app.route("/upload", methods=["GET", "POST"])
def upload():
    if "user" not in session:
        return redirect(url_for("login"))

    uploaded_file = session.get("uploaded_file")

    if uploaded_file and request.method == "POST":
        pass  # fall through to existing upload flow below

    if request.method == "POST":
        if "file" in request.files:
            file = request.files["file"]
            if file.filename:
                filename = secure_filename(file.filename)
                file_path = os.path.join(UPLOAD_FOLDER, filename)
                file.save(file_path)
                session["uploaded_file"] = filename
                session["uploaded_file_path"] = file_path
                return redirect(url_for("upload"))

    transactions = []
    if uploaded_file:
        file_path = session["uploaded_file_path"]
        green, amber, rag_basis = 2.0, 5.0, "avg"
        summary, test_rag = parse_jmeter_csv(file_path, green, amber, rag_basis)
        transactions = [row.get("Transaction") for row in summary if row.get("Transaction")]
        session["summary"] = summary

    return render_template("upload.html",
                           uploaded_file=uploaded_file,
                           uploaded_file_path=session.get("uploaded_file_path"),
                           transactions=transactions)

@app.route("/analyze", methods=["POST"])
def analyze():
    file_path = request.form["file_path"]
    report_name = request.form["report_name"]
    transactions = request.form.getlist("transactions")
    metrics = request.form.getlist("metrics")
    rag_basis = request.form["rag_basis"]
    include_error = "include_error" in request.form
    error_threshold = float(request.form.get("error_threshold", 0))
    green, amber = float(request.form["green"]), float(request.form["amber"])
    start_time, end_time = request.form.get("start_time"), request.form.get("end_time")

    summary, test_rag = parse_jmeter_csv(file_path, green, amber, rag_basis, start_time, end_time)
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
        concurrent_users = int(df["allThreads"].max()) if "allThreads" in df.columns else None
        steady_state = "Yes" if len(df) > 100 else "No"
    else:
        test_date = test_period = total_duration = "Not Available"
        concurrent_users = None
        steady_state = "Unknown"

    for row in filtered:
        for key in ["Avg (s)", "90th % (s)", "95th % (s)", "Error %"]:
            val = row.get(key)
            if val is not None:
                try:
                    row[key] = float(val)
                except (ValueError, TypeError):
                    row[key] = None

    rag_counts = {
        "GREEN": sum(1 for r in filtered if r.get("RAG") == "GREEN"),
        "AMBER": sum(1 for r in filtered if r.get("RAG") == "AMBER"),
        "RED":   sum(1 for r in filtered if r.get("RAG") == "RED"),
    }

    df.columns = [c.strip().lower() for c in df.columns]
    if "timestamp" in df.columns:
        df["timestamp"] = pd.to_datetime(df["timestamp"], unit="ms", errors="coerce")
    if "elapsed" in df.columns:
        df["elapsed"] = pd.to_numeric(df["elapsed"], errors="coerce")
    if "success" in df.columns:
        df["success"] = df["success"].astype(str).str.lower().isin(["true", "1"])
    else:
        df["success"] = True
    if "label" not in df.columns:
        print("⚠️ No 'label' column found in CSV, charts may be empty")

    df = df.dropna(subset=["timestamp"]).sort_values("timestamp")

    time_index = df["timestamp"].dt.floor("min")
    time_labels = sorted(time_index.dropna().unique())
    labels_fmt = [ts.strftime("%H:%M") for ts in time_labels]

    series_avg_by_txn, series_p90_by_txn, series_error_rate_by_txn = {}, {}, {}
    for txn, g in df.groupby("label"):
        gb = g.groupby(g["timestamp"].dt.floor("min"))
        avg_ms = gb["elapsed"].mean()
        p90_ms = gb["elapsed"].quantile(0.90)
        err_pct = gb.apply(lambda x: 100.0 * ((~x["success"]).sum() / len(x)))
        series_avg_by_txn[txn] = [round(avg_ms.get(t, None)/1000.0, 3) if pd.notnull(avg_ms.get(t, None)) else None for t in time_labels]
        series_p90_by_txn[txn] = [round(p90_ms.get(t, None)/1000.0, 3) if pd.notnull(p90_ms.get(t, None)) else None for t in time_labels]
        series_error_rate_by_txn[txn] = [round(err_pct.get(t, None), 3) if pd.notnull(err_pct.get(t, None)) else None for t in time_labels]

    throughput_over_time = df.groupby(df["timestamp"].dt.floor("min")).size()
    series_throughput_over_time = [int(throughput_over_time.get(t, 0)) for t in time_labels]

    chart_labels = [row["Transaction"] for row in filtered]
    avg_values = [row["Avg (s)"] for row in filtered]
    p90_values = [row["90th % (s)"] for row in filtered]
    p95_values = [row["95th % (s)"] for row in filtered]
    error_values = [row["Error %"] for row in filtered]

    report_data = {
        "report_name": report_name,
        "file_name": os.path.basename(file_path),
        "summary": filtered,
        "rag_result": test_rag,
        "test_date": test_date,
        "test_period": test_period,
        "total_duration": total_duration,
        "concurrent_users": concurrent_users,
        "steady_state": steady_state,
        "rag_counts": rag_counts,
        "chart_time_labels": labels_fmt,
        "series_avg_by_txn": series_avg_by_txn,
        "series_p90_by_txn": series_p90_by_txn,
        "series_error_rate_by_txn": series_error_rate_by_txn,
        "series_throughput_over_time": series_throughput_over_time,
        "labels": chart_labels,
        "avg_values": avg_values,
        "p90_values": p90_values,
        "p95_values": p95_values,
        "error_values": error_values,
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
                "p90": float(row.get("90th % (s)", 0))
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

    return render_template(
        "trend.html",
        summary_table=summary_table,
        txn_trends=txn_trends,
        all_txns=sorted(all_txns),
        selected_metric=selected_metric,
        selected_txns=selected_txns,
        n=n
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
        times = [r[0] for r in records]
        successes = [r[1] for r in records]
        samples = len(times)
        if samples == 0:
            continue
        avg = round(sum(times) / samples, 2)
        p90 = round(np.percentile(times, 90), 2)
        p95 = round(np.percentile(times, 95), 2)
        error_pct = round(100 * (1 - sum(successes)/samples), 2)
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
