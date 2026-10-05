import json
import os
import shutil
import sqlite3
from datetime import datetime


class LocalPersistence:
    """Local-only SQLite persistence for VelocityPulse application data."""

    def __init__(
        self,
        db_path,
        backup_dir,
        legacy_history_path=None,
        legacy_baselines_path=None,
        legacy_projects_path=None,
        backup_retention=30,
    ):
        self.db_path = os.path.abspath(db_path)
        self.backup_dir = os.path.abspath(backup_dir)
        self.legacy_history_path = legacy_history_path
        self.legacy_baselines_path = legacy_baselines_path
        self.legacy_projects_path = legacy_projects_path
        self.backup_retention = max(3, int(backup_retention or 30))

    def _connect(self, path=None):
        target = os.path.abspath(path or self.db_path)
        directory = os.path.dirname(target)
        if directory:
            os.makedirs(directory, exist_ok=True)
        connection = sqlite3.connect(target, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        if target == self.db_path:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.execute("PRAGMA synchronous = NORMAL")
        return connection

    def initialize(self):
        os.makedirs(os.path.dirname(self.db_path) or ".", exist_ok=True)
        os.makedirs(self.backup_dir, exist_ok=True)

        with self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS storage_meta (
                    key TEXT PRIMARY KEY,
                    value TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS projects (
                    project_id TEXT PRIMARY KEY,
                    owner_user_id INTEGER,
                    name TEXT NOT NULL,
                    created_at TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE TABLE IF NOT EXISTS reports (
                    report_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    project_id TEXT,
                    project_name TEXT,
                    report_name TEXT,
                    timestamp TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_reports_project
                    ON reports(project_id, report_id DESC);
                CREATE INDEX IF NOT EXISTS idx_reports_timestamp
                    ON reports(timestamp);

                CREATE TABLE IF NOT EXISTS baselines (
                    row_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    baseline_id TEXT,
                    project_id TEXT,
                    project_name TEXT,
                    baseline_number INTEGER,
                    name TEXT,
                    created_at TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_baselines_project
                    ON baselines(project_id, row_id DESC);
                CREATE INDEX IF NOT EXISTS idx_baselines_number
                    ON baselines(project_id, baseline_number);

                CREATE TABLE IF NOT EXISTS run_contexts (
                    run_key TEXT PRIMARY KEY,
                    project_id TEXT,
                    project_name TEXT,
                    created_at TEXT,
                    payload_json TEXT NOT NULL
                );

                CREATE INDEX IF NOT EXISTS idx_run_contexts_project
                    ON run_contexts(project_id, created_at);
                """
            )

        status = self.integrity_check()
        if status != "ok":
            raise RuntimeError(f"VelocityPulse data database integrity check failed: {status}")

        self._migrate_legacy_json_if_needed()

    def integrity_check(self):
        try:
            with self._connect() as connection:
                row = connection.execute("PRAGMA quick_check").fetchone()
            return str(row[0] if row else "unknown")
        except sqlite3.DatabaseError as exc:
            return f"database-error: {exc}"

    @staticmethod
    def _json_payload(value):
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"))

    @staticmethod
    def _decode_payload(value):
        try:
            decoded = json.loads(value)
            return decoded if isinstance(decoded, dict) else {}
        except (TypeError, ValueError, json.JSONDecodeError):
            return {}

    def _table_count(self, table_name):
        allowed = {"projects", "reports", "baselines"}
        if table_name not in allowed:
            raise ValueError("Unsupported table name")
        with self._connect() as connection:
            row = connection.execute(f"SELECT COUNT(*) FROM {table_name}").fetchone()
        return int(row[0] if row else 0)

    def _backup_after_write(self):
        self.backup_now(label="autosave")

    def load_projects(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM projects ORDER BY created_at, rowid"
            ).fetchall()
        return [self._decode_payload(row["payload_json"]) for row in rows]

    def replace_projects(self, projects):
        projects = [dict(item) for item in (projects or []) if isinstance(item, dict)]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM projects")
            for project in projects:
                connection.execute(
                    """
                    INSERT INTO projects (
                        project_id, owner_user_id, name, created_at, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(project.get("id") or ""),
                        project.get("owner_user_id"),
                        str(project.get("name") or ""),
                        project.get("created_at"),
                        self._json_payload(project),
                    ),
                )
        self._backup_after_write()

    def load_reports(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM reports ORDER BY report_id DESC"
            ).fetchall()
        return [self._decode_payload(row["payload_json"]) for row in rows]

    def insert_report(self, report):
        report = dict(report or {})
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO reports (
                    project_id, project_name, report_name, timestamp, payload_json
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    str(report.get("project_id") or ""),
                    str(report.get("project_name") or ""),
                    str(report.get("report_name") or ""),
                    report.get("timestamp"),
                    self._json_payload(report),
                ),
            )
        self._backup_after_write()

    def replace_reports(self, reports):
        reports = [dict(item) for item in (reports or []) if isinstance(item, dict)]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM reports")
            # Existing application semantics keep newest report at index zero.
            for report in reversed(reports):
                connection.execute(
                    """
                    INSERT INTO reports (
                        project_id, project_name, report_name, timestamp, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?)
                    """,
                    (
                        str(report.get("project_id") or ""),
                        str(report.get("project_name") or ""),
                        str(report.get("report_name") or ""),
                        report.get("timestamp"),
                        self._json_payload(report),
                    ),
                )
        self._backup_after_write()

    def load_baselines(self):
        with self._connect() as connection:
            rows = connection.execute(
                "SELECT payload_json FROM baselines ORDER BY row_id DESC"
            ).fetchall()
        return [self._decode_payload(row["payload_json"]) for row in rows]

    def replace_baselines(self, baselines):
        baselines = [dict(item) for item in (baselines or []) if isinstance(item, dict)]
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute("DELETE FROM baselines")
            for profile in reversed(baselines):
                number = profile.get("number")
                try:
                    number = int(number) if number is not None else None
                except (TypeError, ValueError):
                    number = None
                connection.execute(
                    """
                    INSERT INTO baselines (
                        baseline_id, project_id, project_name, baseline_number,
                        name, created_at, payload_json
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        str(profile.get("id") or ""),
                        str(profile.get("project_id") or ""),
                        str(profile.get("project_name") or ""),
                        number,
                        str(profile.get("name") or ""),
                        profile.get("created_at"),
                        self._json_payload(profile),
                    ),
                )
        self._backup_after_write()

    def save_run_context(self, run_key, project):
        run_key = str(run_key or "").strip()
        if not run_key:
            raise ValueError("run_key is required")
        project = dict(project or {})
        payload = {
            "project_id": str(project.get("id") or project.get("project_id") or ""),
            "project_name": str(project.get("name") or project.get("project_name") or ""),
        }
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO run_contexts (
                    run_key, project_id, project_name, created_at, payload_json
                )
                VALUES (?, ?, ?, ?, ?)
                ON CONFLICT(run_key) DO UPDATE SET
                    project_id = excluded.project_id,
                    project_name = excluded.project_name,
                    payload_json = excluded.payload_json
                """,
                (
                    run_key,
                    payload["project_id"],
                    payload["project_name"],
                    datetime.now().isoformat(),
                    self._json_payload(payload),
                ),
            )
        self._backup_after_write()

    def load_run_context(self, run_key):
        run_key = str(run_key or "").strip()
        if not run_key:
            return {}
        with self._connect() as connection:
            row = connection.execute(
                "SELECT payload_json FROM run_contexts WHERE run_key = ?",
                (run_key,),
            ).fetchone()
        return self._decode_payload(row["payload_json"]) if row else {}

    def _meta_get(self, key):
        with self._connect() as connection:
            row = connection.execute(
                "SELECT value FROM storage_meta WHERE key = ?", (key,)
            ).fetchone()
        return row["value"] if row else None

    def _meta_set(self, key, value):
        with self._connect() as connection:
            connection.execute(
                """
                INSERT INTO storage_meta (key, value)
                VALUES (?, ?)
                ON CONFLICT(key) DO UPDATE SET value = excluded.value
                """,
                (key, str(value)),
            )

    @staticmethod
    def _read_legacy_list(path):
        if not path or not os.path.isfile(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            return data if isinstance(data, list) else []
        except (OSError, ValueError, json.JSONDecodeError):
            return []

    def _migrate_legacy_json_if_needed(self):
        if self._meta_get("legacy_json_migration_v1") == "complete":
            return

        projects = self._read_legacy_list(self.legacy_projects_path)
        reports = self._read_legacy_list(self.legacy_history_path)
        baselines = self._read_legacy_list(self.legacy_baselines_path)

        if projects and self._table_count("projects") == 0:
            self.replace_projects(projects)
        if reports and self._table_count("reports") == 0:
            self.replace_reports(reports)
        if baselines and self._table_count("baselines") == 0:
            self.replace_baselines(baselines)

        self._meta_set("legacy_json_migration_v1", "complete")

    def backup_now(self, label="manual"):
        status = self.integrity_check()
        if status != "ok":
            raise RuntimeError(f"Refusing to back up a database that failed integrity check: {status}")

        os.makedirs(self.backup_dir, exist_ok=True)
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S_%f")
        safe_label = "".join(ch for ch in str(label) if ch.isalnum() or ch in ("-", "_")) or "backup"
        destination = os.path.join(
            self.backup_dir,
            f"velocitypulse_data_{stamp}_{safe_label}.db",
        )

        source = self._connect()
        target = sqlite3.connect(destination, timeout=15)
        try:
            source.backup(target)
            target.commit()
        finally:
            target.close()
            source.close()

        self._prune_backups()
        return destination

    def backup_if_due(self):
        os.makedirs(self.backup_dir, exist_ok=True)
        today = datetime.now().strftime("%Y%m%d")
        prefix = f"velocitypulse_data_{today}_"
        existing_today = [
            name for name in os.listdir(self.backup_dir)
            if name.startswith(prefix) and name.endswith(".db")
        ]
        if existing_today:
            return None
        return self.backup_now(label="daily")

    def _prune_backups(self):
        backups = []
        for name in os.listdir(self.backup_dir):
            if not name.startswith("velocitypulse_data_") or not name.endswith(".db"):
                continue
            path = os.path.join(self.backup_dir, name)
            try:
                backups.append((os.path.getmtime(path), path))
            except OSError:
                continue
        backups.sort(reverse=True)
        for _, path in backups[self.backup_retention:]:
            try:
                os.remove(path)
            except OSError:
                pass

    def list_backups(self):
        backups = []
        if not os.path.isdir(self.backup_dir):
            return backups
        for name in os.listdir(self.backup_dir):
            if not name.startswith("velocitypulse_data_") or not name.endswith(".db"):
                continue
            path = os.path.join(self.backup_dir, name)
            try:
                backups.append({
                    "name": name,
                    "path": path,
                    "size_bytes": os.path.getsize(path),
                    "modified_at": datetime.fromtimestamp(os.path.getmtime(path)).isoformat(),
                })
            except OSError:
                continue
        return sorted(backups, key=lambda item: item["modified_at"], reverse=True)

    def restore_backup(self, backup_path):
        backup_path = os.path.abspath(backup_path)
        backup_root = os.path.abspath(self.backup_dir)
        if os.path.commonpath([backup_path, backup_root]) != backup_root:
            raise ValueError("Backup must be inside the configured VelocityPulse backup directory.")
        if not os.path.isfile(backup_path):
            raise FileNotFoundError(backup_path)

        with sqlite3.connect(backup_path, timeout=15) as source:
            row = source.execute("PRAGMA quick_check").fetchone()
            if not row or str(row[0]) != "ok":
                raise RuntimeError("Selected backup failed its SQLite integrity check.")

        safety_copy = self.backup_now(label="pre_restore")
        restore_temp = self.db_path + ".restore"
        shutil.copy2(backup_path, restore_temp)

        for suffix in ("-wal", "-shm"):
            try:
                os.remove(self.db_path + suffix)
            except FileNotFoundError:
                pass

        os.replace(restore_temp, self.db_path)

        status = self.integrity_check()
        if status != "ok":
            shutil.copy2(safety_copy, self.db_path)
            for suffix in ("-wal", "-shm"):
                try:
                    os.remove(self.db_path + suffix)
                except FileNotFoundError:
                    pass
            raise RuntimeError(f"Restore failed integrity check and was rolled back: {status}")
        return safety_copy
