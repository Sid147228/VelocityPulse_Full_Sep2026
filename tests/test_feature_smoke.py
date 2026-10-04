import io
import os
import tempfile
import time
import types
import sys
import unittest
from unittest.mock import patch

import app as velocity_app


def report(name, timestamp, avg=1.0, p90=1.2, error=0.0):
    return {
        "report_name": name,
        "file_name": name + ".jtl",
        "timestamp": timestamp,
        "test_date": "01-10-2026",
        "test_period": "01-10-2026 10:00:00 to 01-10-2026 10:05:00",
        "total_duration": "0:05:00",
        "concurrent_users": 10,
        "rag_result": "GREEN",
        "summary": [
            {
                "Transaction": "Login",
                "#Samples": 100,
                "Avg (s)": avg,
                "90th % (s)": p90,
                "95th % (s)": p90 + 0.1,
                "Error %": error,
                "RAG": "GREEN",
            }
        ],
    }


class FakeThread:
    def __init__(self, alive):
        self._alive = alive

    def is_alive(self):
        return self._alive


class FailedProcess:
    returncode = 1

    def __init__(self):
        self.stderr = io.BytesIO(b"synthetic JMeter startup failure")

    def poll(self):
        return 1


class RunningProcess:
    returncode = None

    def poll(self):
        return None


class StoppableProcess:
    returncode = None

    def __init__(self):
        self.terminated = False
        self.killed = False
        self.wait_calls = 0

    def poll(self):
        return None if not self.terminated and not self.killed else 0

    def terminate(self):
        self.terminated = True

    def kill(self):
        self.killed = True

    def wait(self, timeout=None):
        self.wait_calls += 1
        return 0


class FeatureSmokeTests(unittest.TestCase):
    def setUp(self):
        velocity_app.app.config.update(TESTING=True)
        self.client = velocity_app.app.test_client()
        self.authenticate()

        velocity_app.test_running = False
        velocity_app.current_process = None
        velocity_app.current_run_dir = None
        velocity_app.last_test_summary = None
        velocity_app.live_progress_points.clear()
        velocity_app.monitoring_active = False
        velocity_app.monitoring_threads = []
        velocity_app.monitoring_latest = {}
        velocity_app.transaction_stats.clear()

    def authenticate(self):
        with self.client.session_transaction() as session:
            now = int(time.time())
            session["user"] = "Regression User"
            session["user_profile"] = {
                "id": 999,
                "name": "Regression User",
                "email": "regression@example.invalid",
                "auth_type": "pin",
            }
            session["authenticated_at"] = now
            session["last_activity_at"] = now

    def test_authentication_gate_redirects_anonymous_user(self):
        anonymous = velocity_app.app.test_client()
        response = anonymous.get("/history")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

    def test_history_page_two_links_to_global_report_indexes(self):
        reports = [
            report(f"Report {index}", f"2026-10-{10-index:02d}T10:00:00")
            for index in range(7)
        ]
        with patch.object(velocity_app, "load_history", return_value=reports):
            response = self.client.get("/history?page=2")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("/report/5", html)
        self.assertIn("/report/6", html)
        self.assertNotIn('href="/report/0"', html)

    def test_history_invalid_page_is_clamped(self):
        reports = [report("Only Report", "2026-10-01T10:00:00")]
        with patch.object(velocity_app, "load_history", return_value=reports):
            response = self.client.get("/history?page=999")
        self.assertEqual(response.status_code, 200)
        self.assertIn("Page 1 of 1", response.get_data(as_text=True))

    def test_compare_invalid_report_id_redirects_instead_of_500(self):
        reports = [
            report("Earlier", "2026-10-01T10:00:00"),
            report("Later", "2026-10-02T10:00:00"),
        ]
        with patch.object(velocity_app, "load_history", return_value=reports):
            response = self.client.get("/compare?report_ids=0&report_ids=999")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/compare/select", response.headers["Location"])

    def test_compare_calculation_is_shared_and_nonempty(self):
        earlier = report("Earlier", "2026-10-01T10:00:00", avg=1.0)
        later = report("Later", "2026-10-02T10:00:00", avg=1.4)
        metric, txns, selected, comparisons, observations = velocity_app.build_comparison_data(
            earlier,
            later,
            "Avg (s)",
        )

        self.assertEqual(metric, "Avg (s)")
        self.assertEqual(txns, ["Login"])
        self.assertEqual(selected, ["Login"])
        self.assertEqual(len(comparisons), 1)
        self.assertEqual(comparisons[0]["status"], "Degraded")
        self.assertAlmostEqual(comparisons[0]["diff"], 0.4)
        self.assertTrue(observations)

    def test_baseline_route_populates_chart_arrays(self):
        reports = [
            report("Newer", "2026-10-02T10:00:00", avg=2.0, p90=2.5),
            report("Older", "2026-10-01T10:00:00", avg=1.0, p90=1.5),
        ]
        with patch.object(velocity_app, "load_history", return_value=reports):
            response = self.client.get("/baseline?n=2")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("data: [1.5]", html)
        self.assertIn("data: [2.0]", html)
        self.assertIn("data: [1.8]", html)
        self.assertIn("data: [2.25]", html)

    def test_latest_run_uses_modification_time_not_uuid_sort_order(self):
        with tempfile.TemporaryDirectory() as root:
            older = os.path.join(root, "run_zzzzzzzz")
            newer = os.path.join(root, "run_aaaaaaaa")
            os.makedirs(older)
            os.makedirs(newer)

            old_result = os.path.join(older, "results.jtl")
            new_result = os.path.join(newer, "results.jtl")
            open(old_result, "w", encoding="utf-8").close()
            open(new_result, "w", encoding="utf-8").close()

            now = time.time()
            os.utime(old_result, (now - 100, now - 100))
            os.utime(new_result, (now, now))

            with patch.object(velocity_app, "UPLOAD_FOLDER", root):
                selected = velocity_app._latest_run_dir()

            self.assertEqual(selected, newer)

    def test_run_test_page_idle_shows_upload_form(self):
        response = self.client.get("/run_test")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="runForm"', html)
        self.assertIn('name="jmx_file"', html)
        self.assertIn('name="data_files"', html)
        self.assertIn("if (!status || !form) return;", html)

    def test_run_test_page_active_shows_progress_and_stop_without_upload_form(self):
        velocity_app.test_running = True
        velocity_app.current_process = RunningProcess()

        response = self.client.get("/run_test")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)

        self.assertIn("Test in progress", html)
        self.assertIn("View live progress", html)
        self.assertIn("Stop Test", html)
        self.assertNotIn('id="runForm"', html)

    def test_invalid_run_test_upload_does_not_create_run_state(self):
        with patch.object(velocity_app, "make_run_dir") as make_run_dir_mock:
            response = self.client.post(
                "/run_test",
                data={
                    "jmx_file": (io.BytesIO(b"not jmx"), "load.txt"),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(velocity_app.test_running)
        self.assertIsNone(velocity_app.current_run_dir)
        make_run_dir_mock.assert_not_called()

    def test_unsupported_run_data_file_is_rejected_before_run_directory(self):
        with patch.object(velocity_app, "make_run_dir") as make_run_dir_mock:
            response = self.client.post(
                "/run_test",
                data={
                    "jmx_file": (io.BytesIO(b"<jmeterTestPlan/>"), "load.jmx"),
                    "data_files": (io.BytesIO(b"bad"), "payload.exe"),
                },
                content_type="multipart/form-data",
            )

        self.assertEqual(response.status_code, 302)
        self.assertFalse(velocity_app.test_running)
        self.assertIsNone(velocity_app.current_run_dir)
        make_run_dir_mock.assert_not_called()

    def test_start_jmeter_redirects_process_output_instead_of_unread_pipes(self):
        with tempfile.TemporaryDirectory() as run_dir:
            process = RunningProcess()
            with patch.object(velocity_app.subprocess, "Popen", return_value=process) as popen:
                returned = velocity_app.start_jmeter(
                    os.path.join(run_dir, "load.jmx"),
                    [],
                    os.path.join(run_dir, "results.jtl"),
                    os.path.join(run_dir, "jmeter.log"),
                )

            self.assertIs(returned, process)
            kwargs = popen.call_args.kwargs
            self.assertIs(kwargs["stderr"], velocity_app.subprocess.STDOUT)
            self.assertIsNot(kwargs["stdout"], velocity_app.subprocess.PIPE)
            self.assertTrue(returned.velocitypulse_output_log.endswith("jmeter_process.log"))

    def test_successful_run_test_start_redirects_to_live_progress(self):
        with tempfile.TemporaryDirectory() as run_dir:
            running = RunningProcess()
            fake_thread = unittest.mock.MagicMock()
            with (
                patch.object(velocity_app, "make_run_dir", return_value=run_dir),
                patch.object(velocity_app, "start_jmeter", return_value=running),
                patch.object(velocity_app.threading, "Thread", return_value=fake_thread) as thread_mock,
                patch.object(velocity_app.socketio, "emit"),
            ):
                response = self.client.post(
                    "/run_test",
                    data={
                        "jmx_file": (io.BytesIO(b"<jmeterTestPlan/>"), "load.jmx"),
                        "data_files": (io.BytesIO(b"id\n1\n"), "users.csv"),
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 302)
            self.assertIn("/live_progress", response.headers["Location"])
            self.assertTrue(velocity_app.test_running)
            self.assertIs(velocity_app.current_process, running)
            self.assertEqual(velocity_app.current_run_dir, run_dir)
            self.assertTrue(os.path.exists(os.path.join(run_dir, "load.jmx")))
            self.assertTrue(os.path.exists(os.path.join(run_dir, "users.csv")))
            self.assertEqual(thread_mock.call_count, 2)
            self.assertEqual(fake_thread.start.call_count, 2)

    def test_live_progress_page_contains_refresh_restore_controls(self):
        response = self.client.get("/live_progress")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn('id="responseChart"', html)
        self.assertIn('id="errorChart"', html)
        self.assertIn('id="logPanel"', html)
        self.assertIn('id="generateReportButton"', html)
        self.assertIn("setProgressSnapshot(snapshot.progress)", html)
        self.assertIn("renderCompletionSummary(snapshot.summary", html)
        self.assertIn("MAX_LIVE_POINTS = 500", html)

    def test_live_status_restores_completed_summary_progress_logs_and_report_availability(self):
        with tempfile.TemporaryDirectory() as run_dir:
            velocity_app.current_run_dir = run_dir
            result_file = os.path.join(run_dir, "results.jtl")
            with open(result_file, "w", encoding="utf-8") as handle:
                handle.write("timeStamp,elapsed,label,success\n1,100,Login,true\n")
            with open(os.path.join(run_dir, "jmeter.log"), "w", encoding="utf-8") as handle:
                handle.write("line one\nline two\n")

            velocity_app.transaction_stats["Login"] = [(100.0, True), (200.0, False)]
            velocity_app.live_progress_points.extend([
                {"timestamp": "1000", "response_time": 100.0, "error_rate": 0.0},
                {"timestamp": "1200", "response_time": 200.0, "error_rate": 50.0},
            ])
            velocity_app.last_test_summary = {
                "duration": "2.0 sec",
                "start": "10:00:00",
                "end": "10:00:02",
                "users": 7,
                "metrics": velocity_app.compute_summary(),
            }

            response = self.client.get("/live_status")
            payload = response.get_json()

            self.assertFalse(payload["running"])
            self.assertTrue(payload["completed"])
            self.assertTrue(payload["results_available"])
            self.assertEqual(payload["summary"]["users"], 7)
            self.assertEqual(len(payload["progress"]), 2)
            self.assertEqual(payload["logs"], ["line one", "line two"])
            self.assertEqual(payload["metrics"][0]["samples"], 2)

    def test_live_generate_report_uses_current_completed_run(self):
        source_fixture = os.path.join(
            os.path.dirname(__file__),
            "fixtures",
            "apache_jmeter",
            "HTMLReportTestFile.csv",
        )
        with tempfile.TemporaryDirectory() as run_dir:
            target = os.path.join(run_dir, "results.jtl")
            with open(source_fixture, "rb") as source, open(target, "wb") as output:
                output.write(source.read())

            velocity_app.current_run_dir = run_dir
            captured = {}

            with (
                patch.object(velocity_app, "save_report", side_effect=lambda data: captured.update(data)),
                patch.object(velocity_app, "generate_report_graph_assets", return_value={}),
            ):
                response = self.client.get("/generate_report")

            self.assertEqual(response.status_code, 302)
            self.assertIn("/report/latest", response.headers["Location"])
            self.assertTrue(captured["summary"])
            self.assertEqual(captured["file_name"], "results.jtl")
            self.assertEqual(captured["steady_state"], "Full test period")

    def test_stop_test_terminates_process_and_clears_running_state(self):
        process = StoppableProcess()
        velocity_app.test_running = True
        velocity_app.current_process = process

        with patch.object(velocity_app.socketio, "emit"):
            response = self.client.post("/stop_test")

        self.assertEqual(response.status_code, 302)
        self.assertTrue(process.terminated)
        self.assertGreaterEqual(process.wait_calls, 1)
        self.assertFalse(velocity_app.test_running)
        self.assertIsNone(velocity_app.current_process)

    def test_immediate_jmeter_failure_resets_running_state_and_sanitizes_filename(self):
        with tempfile.TemporaryDirectory() as run_dir:
            failed = FailedProcess()
            with (
                patch.object(velocity_app, "make_run_dir", return_value=run_dir),
                patch.object(velocity_app, "start_jmeter", return_value=failed) as start_mock,
                patch.object(velocity_app.socketio, "emit"),
            ):
                response = self.client.post(
                    "/run_test",
                    data={
                        "jmx_file": (io.BytesIO(b"<jmeterTestPlan/>"), "../../unsafe.jmx"),
                    },
                    content_type="multipart/form-data",
                )

            self.assertEqual(response.status_code, 302)
            self.assertFalse(velocity_app.test_running)
            self.assertIsNone(velocity_app.current_process)

            jmx_path = start_mock.call_args.args[0]
            self.assertEqual(os.path.dirname(jmx_path), run_dir)
            self.assertEqual(os.path.basename(jmx_path), "unsafe.jmx")

    def test_live_jtl_parser_uses_header_names_and_reports_max_threads(self):
        with tempfile.TemporaryDirectory() as root:
            result_file = os.path.join(root, "results.jtl")
            with open(result_file, "w", encoding="utf-8", newline="") as handle:
                handle.write(
                    "label,success,elapsed,timeStamp,allThreads,responseCode\n"
                    "Login,true,100,1000,5,200\n"
                    "Login,false,200,1200,7,500\n"
                )

            velocity_app.test_running = True
            velocity_app.current_process = FailedProcess()

            with (
                patch.object(velocity_app.socketio, "emit") as emit_mock,
                patch.object(velocity_app.time, "sleep", return_value=None),
            ):
                velocity_app.tail_results(result_file)

            completion_calls = [
                call
                for call in emit_mock.call_args_list
                if call.args and call.args[0] == "test_complete"
            ]
            self.assertEqual(len(completion_calls), 1)
            summary = completion_calls[0].args[1]
            self.assertEqual(summary["users"], 7)
            self.assertEqual(summary["duration"], "0.40 sec")
            self.assertEqual(len(summary["metrics"]), 1)
            self.assertEqual(summary["metrics"][0]["label"], "Login")
            self.assertEqual(summary["metrics"][0]["samples"], 2)
            self.assertEqual(summary["metrics"][0]["avg"], 150.0)
            self.assertEqual(summary["metrics"][0]["error_pct"], 50.0)
            self.assertFalse(velocity_app.test_running)
            self.assertIsNone(velocity_app.current_process)

    def test_start_monitoring_without_servers_stays_inactive(self):
        response = self.client.post("/start_monitoring", json={"servers": []})
        self.assertEqual(response.status_code, 400)
        self.assertFalse(velocity_app.monitoring_active)
        self.assertEqual(response.get_json()["servers"], 0)

    def test_remote_windows_monitoring_is_rejected_instead_of_faking_local_metrics(self):
        response = self.client.post(
            "/start_monitoring",
            json={
                "servers": [
                    {"name": "RemoteWin", "host": "10.0.0.50", "os": "windows"}
                ]
            },
        )
        self.assertEqual(response.status_code, 400)
        payload = response.get_json()
        self.assertEqual(payload["servers"], 0)
        self.assertTrue(payload["errors"])
        self.assertFalse(velocity_app.monitoring_active)

    def test_remote_windows_connection_check_is_not_a_false_positive(self):
        response = self.client.post(
            "/test_connection",
            json={"host": "10.0.0.50", "os": "windows"},
        )
        self.assertEqual(response.status_code, 501)
        self.assertIn("not configured", response.get_json()["status"])

    def test_live_status_counts_only_alive_monitor_threads(self):
        velocity_app.monitoring_active = True
        velocity_app.monitoring_threads = [FakeThread(True), FakeThread(False)]

        response = self.client.get("/live_status")
        payload = response.get_json()

        self.assertTrue(payload["monitoring"]["active"])
        self.assertEqual(payload["monitoring"]["servers"], 1)

    def test_valid_jmeter_upload_round_trip_shows_detected_window_without_error(self):
        fixture = os.path.join(
            os.path.dirname(__file__),
            "fixtures",
            "apache_jmeter",
            "HTMLReportTestFile.csv",
        )

        with tempfile.TemporaryDirectory() as upload_dir:
            with open(fixture, "rb") as source:
                payload = source.read()

            with patch.object(velocity_app, "UPLOAD_FOLDER", upload_dir):
                response = self.client.post(
                    "/upload",
                    data={
                        "file": (io.BytesIO(payload), "valid-results.csv"),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("valid-results.csv uploaded successfully", html)
            self.assertIn("Detected test window", html)
            self.assertIn("Report configuration", html)
            self.assertNotIn("Unable to read the uploaded JMeter result file", html)

    def test_timestamp_helpers_round_trip_millisecond_precision(self):
        original = 1788256800123
        formatted = velocity_app._format_test_timestamp(original)
        restored = velocity_app._parse_datetime_local(formatted["input"])
        self.assertEqual(restored, original)

    def test_upload_page_always_uses_upload_csv_jtl_wording(self):
        response = self.client.get("/upload")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Upload CSV/JTL", html)
        self.assertNotIn("Replace file", html)

    def test_unreadable_loaded_file_clears_stale_session_state(self):
        with tempfile.TemporaryDirectory() as upload_dir:
            bad_file = os.path.join(upload_dir, "bad.csv")
            with open(bad_file, "w", encoding="utf-8") as handle:
                handle.write("timeStamp,elapsed,label,success\n")
                handle.write("invalid,not-a-number,Login,true\n")

            with self.client.session_transaction() as session:
                session["uploaded_file"] = "bad.csv"
                session["uploaded_file_path"] = bad_file
                session["test_window"] = {"start_ms": 1, "end_ms": 2}
                session["summary"] = [{"Transaction": "stale"}]

            response = self.client.get("/upload")
            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertNotIn("JMeter result loaded", html)
            self.assertNotIn("Replace file", html)

            with self.client.session_transaction() as session:
                self.assertNotIn("uploaded_file", session)
                self.assertNotIn("uploaded_file_path", session)
                self.assertNotIn("test_window", session)
                self.assertNotIn("summary", session)

    def test_upload_rejects_partial_jmeter_schema_without_success_banner(self):
        with tempfile.TemporaryDirectory() as upload_dir:
            partial_csv = (
                b"timeStamp,responseCode,responseMessage\n"
                b"1000,200,OK\n"
                b"2000,200,OK\n"
            )
            with patch.object(velocity_app, "UPLOAD_FOLDER", upload_dir):
                response = self.client.post(
                    "/upload",
                    data={
                        "file": (io.BytesIO(partial_csv), "partial.csv"),
                    },
                    content_type="multipart/form-data",
                    follow_redirects=True,
                )

            self.assertEqual(response.status_code, 200)
            html = response.get_data(as_text=True)
            self.assertIn("Missing required JMeter column", html)
            self.assertIn("elapsed", html)
            self.assertIn("label", html)
            self.assertIn("success", html)
            self.assertNotIn("uploaded successfully", html)
            self.assertFalse(os.path.exists(os.path.join(upload_dir, "partial.csv")))

    def test_analyze_filters_charts_and_rag_to_selected_transactions_and_metrics(self):
        fixture = (
            os.path.dirname(__file__)
            + "/fixtures/apache_jmeter/HTMLReportTestFile.csv"
        )
        with self.client.session_transaction() as session:
            session["uploaded_file"] = "HTMLReportTestFile.csv"
            session["uploaded_file_path"] = fixture
            session["test_window"] = {}

        captured = {}

        def capture_report(report_data):
            captured.update(report_data)

        with (
            patch.object(velocity_app, "save_report", side_effect=capture_report),
            patch.object(velocity_app, "generate_report_graph_assets", return_value={}),
        ):
            response = self.client.post(
                "/analyze",
                data={
                    "report_name": "Selected transaction report",
                    "transactions": ["JR-OK"],
                    "metrics": ["avg"],
                    "rag_basis": "avg",
                    "green": "1.5",
                    "amber": "3.5",
                },
            )

        self.assertEqual(response.status_code, 302)
        self.assertEqual(captured["selected_metrics"], ["avg"])
        self.assertEqual(
            [row["Transaction"] for row in captured["summary"]],
            ["JR-OK"],
        )
        self.assertEqual(set(captured["series_avg_by_txn"]), {"JR-OK"})
        self.assertEqual(captured["rag_result"], "GREEN")

    def test_report_template_honors_metric_selection_and_historical_graph_paths(self):
        historical = report("Historical", "2026-10-01T10:00:00")
        historical.update({
            "selected_metrics": ["avg"],
            "chart_time_labels": [],
            "rag_counts": {"GREEN": 1, "AMBER": 0, "RED": 0},
            "graph_paths": {
                "response_distribution": "reports/graphs/report123/response_distribution.png",
                "rag_pie": "reports/graphs/report123/rag_pie.png",
            },
        })

        with patch.object(velocity_app, "load_history", return_value=[historical]):
            response = self.client.get("/report/0")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Avg (s)", html)
        self.assertNotIn("<th>P90 (s)</th>", html)
        self.assertNotIn("<th>Samples</th>", html)
        self.assertNotIn("Avg (ms)", html)
        self.assertIn("Response Time (seconds)", html)
        self.assertIn(
            "/static/reports/graphs/report123/response_distribution.png",
            html,
        )

    def test_redesigned_single_report_uses_seconds_for_response_times(self):
        current = report("Seconds Report", "2026-10-04T10:00:00", avg=1.234, p90=1.8)
        current.update({
            "selected_metrics": ["avg", "p90", "p95", "error", "samples"],
            "chart_time_labels": ["10:00", "10:01"],
            "series_avg_by_txn": {"Login": [1000.0, 1250.0]},
            "series_response_percentiles_over_time": {
                "P50 (Median)": [900.0, 1000.0],
                "P90": [1500.0, 1750.0],
                "P95": [1700.0, 1900.0],
            },
            "series_error_rate_by_txn": {"Login": [0.0, 1.0]},
            "series_throughput_over_time": [1.0, 1.1],
            "series_tps_by_txn": {},
            "graph_paths": {},
            "report_kpis": {
                "total_samples": 100,
                "successful_samples": 99,
                "failed_samples": 1,
                "error_pct": 1.0,
                "avg_s": 1.234,
                "p90_s": 1.8,
                "p95_s": 2.0,
                "throughput_tps": 1.1,
            },
        })

        with patch.object(velocity_app, "load_history", return_value=[current]):
            response = self.client.get("/report/0")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("Average Response Time Over Time (seconds", html)
        self.assertIn("Response Time Percentiles Over Time (seconds", html)
        self.assertIn("Avg (s)", html)
        self.assertIn("P90 (s)", html)
        self.assertIn("P95 (s)", html)
        self.assertNotIn("Avg (ms)", html)
        self.assertNotIn("P90 (ms)", html)
        self.assertNotIn("P95 (ms)", html)
        self.assertIn("Number(value) / 1000", html)

    def test_redesigned_compare_report_uses_seconds_and_delta_dashboard(self):
        earlier = report("Earlier", "2026-10-01T10:00:00", avg=1.0, p90=1.2)
        later = report("Later", "2026-10-02T10:00:00", avg=1.5, p90=1.7)

        with patch.object(velocity_app, "load_history", return_value=[later, earlier]):
            response = self.client.get(
                "/compare?report_ids=1&report_ids=0&metric=Avg%20(s)"
            )

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("VelocityPulse Comparison Report", html)
        self.assertIn("Improved transactions", html)
        self.assertIn("Degraded transactions", html)
        self.assertIn("Absolute Δ (s)", html)
        self.assertIn("Response Time (seconds)", html)
        self.assertNotIn("(ms)", html)

    def test_trend_chart_orders_oldest_to_newest(self):
        reports = [
            report("Newest", "2026-10-03T10:00:00", avg=3.0),
            report("Middle", "2026-10-02T10:00:00", avg=2.0),
            report("Oldest", "2026-10-01T10:00:00", avg=1.0),
        ]
        for item, date in zip(
            reports,
            ["03-10-2026", "02-10-2026", "01-10-2026"],
        ):
            item["test_date"] = date

        with patch.object(velocity_app, "load_history", return_value=reports):
            response = self.client.get("/trend?n=3")

        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        first = html.find("01-10-2026")
        second = html.find("02-10-2026")
        third = html.find("03-10-2026")
        self.assertTrue(0 <= first < second < third)

    def test_report_pdf_route_renders_pdf_response(self):
        historical = report("PDF Report", "2026-10-01T10:00:00")
        historical.update({
            "selected_metrics": ["avg", "p90"],
            "chart_time_labels": [],
            "rag_counts": {"GREEN": 1, "AMBER": 0, "RED": 0},
            "graph_paths": {},
        })

        class FakeHTML:
            def __init__(self, string, base_url=None):
                self.string = string
                self.base_url = base_url

            def write_pdf(self):
                return b"%PDF-fake"

        fake_weasyprint = types.SimpleNamespace(HTML=FakeHTML)
        with (
            patch.object(velocity_app, "load_history", return_value=[historical]),
            patch.dict(sys.modules, {"weasyprint": fake_weasyprint}),
        ):
            response = self.client.get("/export_report_pdf/0")

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertTrue(response.data.startswith(b"%PDF"))

    def test_compare_pdf_contains_real_comparison_rows(self):
        earlier = report("Earlier", "2026-10-01T10:00:00", avg=1.0)
        later = report("Later", "2026-10-02T10:00:00", avg=1.5)
        captured = {}

        class FakeHTML:
            def __init__(self, string, base_url=None):
                captured["html"] = string
                captured["base_url"] = base_url

            def write_pdf(self):
                return b"%PDF-compare"

        fake_weasyprint = types.SimpleNamespace(HTML=FakeHTML)
        with (
            patch.object(velocity_app, "load_history", return_value=[later, earlier]),
            patch.dict(sys.modules, {"weasyprint": fake_weasyprint}),
        ):
            response = self.client.get(
                "/compare/pdf?report_ids=1&report_ids=0&metric=Avg%20(s)"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.mimetype, "application/pdf")
        self.assertIn("Login", captured["html"])
        self.assertIn("Degraded", captured["html"])
        self.assertIn("0.50", captured["html"])

    def test_about_page_renders_with_changelog_fallback(self):
        response = self.client.get("/about")
        self.assertEqual(response.status_code, 200)


class PinAuthenticationTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.originals = {
            "AUTH_DB_PATH": velocity_app.AUTH_DB_PATH,
            "PIN_REGISTRATION_CODE": velocity_app.PIN_REGISTRATION_CODE,
            "PIN_ALLOWED_EMAIL_DOMAINS": velocity_app.PIN_ALLOWED_EMAIL_DOMAINS,
            "PIN_PEPPER": velocity_app.PIN_PEPPER,
            "PIN_MAX_FAILED_ATTEMPTS": velocity_app.PIN_MAX_FAILED_ATTEMPTS,
            "PIN_LOCKOUT_MINUTES": velocity_app.PIN_LOCKOUT_MINUTES,
            "PIN_SESSION_MINUTES": velocity_app.PIN_SESSION_MINUTES,
        }

        velocity_app.AUTH_DB_PATH = os.path.join(self.tempdir.name, "auth.db")
        velocity_app.PIN_REGISTRATION_CODE = "ORG-ACCESS-2026"
        velocity_app.PIN_ALLOWED_EMAIL_DOMAINS = {"company.test"}
        velocity_app.PIN_PEPPER = "test-pepper"
        velocity_app.PIN_MAX_FAILED_ATTEMPTS = 3
        velocity_app.PIN_LOCKOUT_MINUTES = 15
        velocity_app.PIN_SESSION_MINUTES = 60
        velocity_app.init_auth_db()

        velocity_app.app.config.update(TESTING=True)
        self.client = velocity_app.app.test_client()

    def tearDown(self):
        for name, value in self.originals.items():
            setattr(velocity_app, name, value)
        self.tempdir.cleanup()

    def register_user(self, email="user@company.test", pin="482731", name="Test User"):
        return self.client.post(
            "/register",
            data={
                "display_name": name,
                "email": email,
                "registration_code": "ORG-ACCESS-2026",
                "pin": pin,
                "confirm_pin": pin,
            },
        )

    def test_login_page_uses_pin_authentication_not_microsoft(self):
        response = self.client.get("/login")
        self.assertEqual(response.status_code, 200)
        html = response.get_data(as_text=True)
        self.assertIn("6-digit PIN", html)
        self.assertIn("Register your PIN", html)
        self.assertNotIn("Microsoft", html)

    def test_first_time_registration_hashes_pin_and_signs_user_in(self):
        response = self.register_user()
        self.assertEqual(response.status_code, 302)
        self.assertIn("/upload", response.headers["Location"])

        with velocity_app._auth_db_connection() as connection:
            row = connection.execute(
                "SELECT email, display_name, pin_hash FROM users WHERE email = ?",
                ("user@company.test",),
            ).fetchone()

        self.assertIsNotNone(row)
        self.assertEqual(row["display_name"], "Test User")
        self.assertNotEqual(row["pin_hash"], "482731")
        self.assertTrue(velocity_app._pin_matches(row["pin_hash"], "482731"))

        with self.client.session_transaction() as session:
            self.assertEqual(session["user_profile"]["email"], "user@company.test")
            self.assertEqual(session["user_profile"]["auth_type"], "pin")

    def test_registration_rejects_unapproved_email_domain(self):
        response = self.register_user(email="user@outside.test")
        self.assertEqual(response.status_code, 400)
        self.assertIn(
            "approved organization email",
            response.get_data(as_text=True),
        )

    def test_registration_requires_correct_organization_code(self):
        response = self.client.post(
            "/register",
            data={
                "display_name": "Test User",
                "email": "user@company.test",
                "registration_code": "WRONG",
                "pin": "482731",
                "confirm_pin": "482731",
            },
        )
        self.assertEqual(response.status_code, 403)
        html = response.get_data(as_text=True)
        self.assertIn("registration code is invalid", html)
        self.assertIn("Organization registration code", html)

    def test_registration_rejects_predictable_pin(self):
        response = self.register_user(pin="123456")
        self.assertEqual(response.status_code, 400)
        self.assertIn("less predictable PIN", response.get_data(as_text=True))

    def test_registered_user_can_logout_and_login_with_pin(self):
        self.assertEqual(self.register_user().status_code, 302)
        self.client.get("/logout")

        response = self.client.post(
            "/login",
            data={"email": "user@company.test", "pin": "482731"},
        )
        self.assertEqual(response.status_code, 302)
        self.assertIn("/upload", response.headers["Location"])

        with self.client.session_transaction() as session:
            self.assertEqual(session["user_profile"]["email"], "user@company.test")

    def test_wrong_pin_attempts_trigger_account_lockout(self):
        self.assertEqual(self.register_user().status_code, 302)
        self.client.get("/logout")

        for expected_status in (401, 401, 429):
            response = self.client.post(
                "/login",
                data={"email": "user@company.test", "pin": "111222"},
            )
            self.assertEqual(response.status_code, expected_status)

        response = self.client.post(
            "/login",
            data={"email": "user@company.test", "pin": "482731"},
        )
        self.assertEqual(response.status_code, 429)
        self.assertIn("Too many failed attempts", response.get_data(as_text=True))

    def test_expired_pin_session_redirects_back_to_login(self):
        self.assertEqual(self.register_user().status_code, 302)

        with self.client.session_transaction() as session:
            session["last_activity_at"] = int(time.time()) - (61 * 60)

        response = self.client.get("/history")
        self.assertEqual(response.status_code, 302)
        self.assertIn("/login", response.headers["Location"])

        with self.client.session_transaction() as session:
            self.assertNotIn("user_profile", session)



if __name__ == "__main__":
    unittest.main()
