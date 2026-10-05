import os
import tempfile
import unittest

from local_persistence import LocalPersistence


class LocalPersistenceTests(unittest.TestCase):
    def setUp(self):
        self.tempdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tempdir.name, "instance", "velocitypulse_data.db")
        self.backup_dir = os.path.join(self.tempdir.name, "instance", "backups")
        self.legacy_history = os.path.join(self.tempdir.name, "history.json")
        self.legacy_baselines = os.path.join(self.tempdir.name, "baselines.json")
        self.legacy_projects = os.path.join(self.tempdir.name, "projects.json")
        self.store = LocalPersistence(
            db_path=self.db_path,
            backup_dir=self.backup_dir,
            legacy_history_path=self.legacy_history,
            legacy_baselines_path=self.legacy_baselines,
            legacy_projects_path=self.legacy_projects,
            backup_retention=30,
        )
        self.store.initialize()

    def tearDown(self):
        self.tempdir.cleanup()

    def test_fresh_install_starts_empty_without_creating_json_storage(self):
        self.assertTrue(os.path.isfile(self.db_path))
        self.assertEqual(self.store.load_projects(), [])
        self.assertEqual(self.store.load_reports(), [])
        self.assertEqual(self.store.load_baselines(), [])
        self.assertFalse(os.path.exists(self.legacy_history))
        self.assertFalse(os.path.exists(self.legacy_baselines))
        self.assertFalse(os.path.exists(self.legacy_projects))
        self.assertEqual(self.store.list_backups(), [])

    def test_projects_reports_baselines_and_run_context_persist_in_sqlite(self):
        project = {
            "id": "project-a",
            "owner_user_id": 1,
            "name": "Application A",
            "created_at": "2026-10-05T18:40:00",
        }
        report = {
            "project_id": "project-a",
            "project_name": "Application A",
            "report_name": "Load Test 1",
            "timestamp": "2026-10-05T18:45:00",
            "summary": [{"Transaction": "Login", "Avg (s)": 0.5}],
        }
        baseline = {
            "id": "BL-1",
            "project_id": "project-a",
            "project_name": "Application A",
            "number": 1,
            "name": "Baseline 1",
            "created_at": "2026-10-05T18:46:00",
        }

        self.store.replace_projects([project])
        self.store.insert_report(report)
        self.store.replace_baselines([baseline])
        self.store.save_run_context("run_12345678", project)

        self.assertEqual(self.store.load_projects()[0]["name"], "Application A")
        self.assertEqual(self.store.load_reports()[0]["report_name"], "Load Test 1")
        self.assertEqual(self.store.load_baselines()[0]["name"], "Baseline 1")
        self.assertEqual(
            self.store.load_run_context("run_12345678"),
            {"project_id": "project-a", "project_name": "Application A"},
        )

        self.assertFalse(os.path.exists(self.legacy_history))
        self.assertFalse(os.path.exists(self.legacy_baselines))
        self.assertFalse(os.path.exists(self.legacy_projects))
        self.assertGreaterEqual(len(self.store.list_backups()), 4)

    def test_restore_returns_database_to_previous_snapshot(self):
        report_one = {
            "project_id": "project-a",
            "project_name": "Application A",
            "report_name": "Report One",
            "timestamp": "2026-10-05T18:45:00",
        }
        report_two = {
            "project_id": "project-a",
            "project_name": "Application A",
            "report_name": "Report Two",
            "timestamp": "2026-10-05T18:50:00",
        }

        self.store.insert_report(report_one)
        snapshot = self.store.backup_now(label="manual_test")
        self.store.insert_report(report_two)
        self.assertEqual(len(self.store.load_reports()), 2)

        safety_copy = self.store.restore_backup(snapshot)

        self.assertTrue(os.path.isfile(safety_copy))
        restored = self.store.load_reports()
        self.assertEqual(len(restored), 1)
        self.assertEqual(restored[0]["report_name"], "Report One")
        self.assertEqual(self.store.integrity_check(), "ok")


if __name__ == "__main__":
    unittest.main()
