import io
import os
import tempfile
import time
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


class FeatureSmokeTests(unittest.TestCase):
    def setUp(self):
        velocity_app.app.config.update(TESTING=True)
        self.client = velocity_app.app.test_client()
        self.authenticate()

        velocity_app.test_running = False
        velocity_app.current_process = None
        velocity_app.current_run_dir = None
        velocity_app.monitoring_active = False
        velocity_app.monitoring_threads = []
        velocity_app.monitoring_latest = {}
        velocity_app.transaction_stats.clear()

    def authenticate(self):
        with self.client.session_transaction() as session:
            session["user"] = "Regression User"
            session["user_profile"] = {
                "name": "Regression User",
                "email": "regression@example.invalid",
            }

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
        self.assertIn("Avg (ms)", html)
        self.assertNotIn("<th>P90 (ms)</th>", html)
        self.assertNotIn("<th>Samples</th>", html)
        self.assertIn(
            "/static/reports/graphs/report123/response_distribution.png",
            html,
        )

    def test_about_page_renders_with_changelog_fallback(self):
        response = self.client.get("/about")
        self.assertEqual(response.status_code, 200)


if __name__ == "__main__":
    unittest.main()
