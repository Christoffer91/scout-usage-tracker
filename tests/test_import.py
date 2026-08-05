import json
import sqlite3
import tempfile
import unittest
from pathlib import Path

from scout_usage_tracker.import_usage import import_usage
from scout_usage_tracker.database import connect_history
from scout_usage_tracker.privacy import session_digest, source_event_key
from scout_usage_tracker.source import read_source

from tests.helpers import event, make_source


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.source = self.root / "source.sqlite3"
        self.history = self.root / "history.sqlite3"
        self.secret = b"a" * 32

    def tearDown(self):
        self.tmp.cleanup()

    def active_rows(self):
        connection = sqlite3.connect(self.history)
        rows = connection.execute("SELECT * FROM usage_versions WHERE active=1").fetchall()
        connection.close()
        return rows

    def test_idempotent_import_duplicate_protection_and_private_storage(self):
        make_source(self.source, [event()])
        first = import_usage(self.source, self.history, self.secret)
        second = import_usage(self.source, self.history, self.secret)
        self.assertEqual((first.inserted, second.inserted), (1, 0))
        connection = sqlite3.connect(self.history)
        count = connection.execute("SELECT COUNT(*) FROM usage_versions").fetchone()[0]
        dump = " ".join(str(item) for row in connection.execute("SELECT * FROM usage_versions") for item in row)
        connection.close()
        self.assertEqual(count, 1)
        self.assertNotIn("synthetic-session", dump)
        self.assertNotIn(str(self.source), dump)
        self.assertNotIn('[{"tokenCount"', dump)

    def test_correction_supersedes_and_keeps_audit_version(self):
        make_source(self.source, [event(total=20)])
        import_usage(self.source, self.history, self.secret)
        connection = sqlite3.connect(self.source)
        connection.execute("UPDATE assistant_usage_events SET total_nano_aiu=21 WHERE id=1")
        connection.commit(); connection.close()
        result = import_usage(self.source, self.history, self.secret)
        connection = sqlite3.connect(self.history)
        states = connection.execute("SELECT active, total_nano_aiu FROM usage_versions ORDER BY imported_at").fetchall()
        connection.close()
        self.assertEqual((result.inserted, result.superseded), (1, 1))
        self.assertEqual(sorted(states), [(0, 20), (1, 21)])

    def test_session_only_correction_supersedes_and_changes_digest(self):
        make_source(self.source, [event(session="synthetic-alpha")])
        import_usage(self.source, self.history, self.secret)
        connection = sqlite3.connect(self.history)
        original_digest = connection.execute("SELECT session_digest FROM usage_versions WHERE active=1").fetchone()[0]
        connection.close()
        source_connection = sqlite3.connect(self.source)
        source_connection.execute("UPDATE assistant_usage_events SET session_id='synthetic-beta' WHERE id=1")
        source_connection.commit(); source_connection.close()
        result = import_usage(self.source, self.history, self.secret)
        connection = sqlite3.connect(self.history)
        versions = connection.execute("SELECT active, session_digest FROM usage_versions").fetchall()
        connection.close()
        self.assertEqual((result.inserted, result.superseded), (1, 1))
        self.assertEqual(len(versions), 2)
        self.assertNotEqual(original_digest, next(digest for active, digest in versions if active))

    def test_exact_raw_detail_change_creates_privacy_safe_version(self):
        compact = '[{"tokenCount":10,"costPerBatch":2,"batchSize":1}]'
        spaced = '[ { "tokenCount": 10, "costPerBatch": 2, "batchSize": 1 } ]'
        make_source(self.source, [event(details=compact)])
        import_usage(self.source, self.history, self.secret)
        source_connection = sqlite3.connect(self.source)
        source_connection.execute("UPDATE assistant_usage_events SET token_details_json=? WHERE id=1", (spaced,))
        source_connection.commit(); source_connection.close()
        result = import_usage(self.source, self.history, self.secret)
        connection = sqlite3.connect(self.history)
        dump = " ".join(str(value) for row in connection.execute("SELECT * FROM usage_versions") for value in row)
        count = connection.execute("SELECT COUNT(*) FROM usage_versions").fetchone()[0]
        connection.close()
        self.assertEqual((result.inserted, result.superseded, count), (1, 1, 2))
        self.assertNotIn(compact, dump)
        self.assertNotIn(spaced, dump)

    def test_invalid_rows_are_counted_and_history_schema_migrates(self):
        make_source(self.source, [event(identifier=1), (*event(identifier=2)[:3], -1, *event(identifier=2)[4:])])
        result = import_usage(self.source, self.history, self.secret)
        self.assertEqual((result.seen, result.inserted, result.rows_skipped), (2, 1, 1))
        connection = sqlite3.connect(self.history)
        self.assertEqual(connection.execute("SELECT rows_skipped FROM import_runs").fetchone()[0], 1)
        connection.close()

        legacy = self.root / "legacy-history.sqlite3"
        legacy_connection = sqlite3.connect(legacy)
        legacy_connection.execute("CREATE TABLE import_runs (run_id INTEGER PRIMARY KEY, imported_at TEXT NOT NULL, source_status TEXT NOT NULL, rows_seen INTEGER NOT NULL, rows_inserted INTEGER NOT NULL, rows_superseded INTEGER NOT NULL, possible_id_gap INTEGER NOT NULL, warnings_json TEXT NOT NULL)")
        legacy_connection.commit(); legacy_connection.close()
        migrated = connect_history(legacy)
        columns = {row[1] for row in migrated.execute("PRAGMA table_info(import_runs)")}
        migrated.close()
        self.assertIn("rows_skipped", columns)

    def test_negative_stored_adjustment_is_imported(self):
        make_source(self.source, [event(total=-20)])
        result = import_usage(self.source, self.history, self.secret)
        self.assertEqual(result.status, "ok")
        self.assertEqual(self.active_rows()[0][11], -20)

    def test_missing_schema_and_gap_statuses(self):
        self.assertEqual(read_source(self.source).status, "missing")
        connection = sqlite3.connect(self.source)
        connection.execute("CREATE TABLE assistant_usage_events(id INTEGER PRIMARY KEY)")
        connection.commit(); connection.close()
        self.assertEqual(read_source(self.source).status, "schema")

    def test_initial_retention_gap_when_first_id_exceeds_one(self):
        make_source(self.source, [event(identifier=4)])
        result = read_source(self.source)
        self.assertEqual(result.status, "ok")
        self.assertTrue(result.possible_id_gap)

    def test_corrupt_database_status(self):
        self.source.write_text("not sqlite", encoding="utf-8")
        self.assertEqual(read_source(self.source).status, "corrupt")

    def test_locked_database_status(self):
        make_source(self.source, [event()])
        lock = sqlite3.connect(self.source, timeout=0)
        lock.execute("BEGIN EXCLUSIVE")
        try:
            self.assertEqual(read_source(self.source, timeout=0.01).status, "locked")
        finally:
            lock.rollback(); lock.close()

    def test_session_hmac_is_salted_and_source_key_hides_inputs(self):
        self.assertNotEqual(session_digest(b"a" * 32, "same"), session_digest(b"b" * 32, "same"))
        key = source_event_key(self.secret, self.source, 123)
        self.assertEqual(len(key), 64)
        self.assertNotIn("123", key)
        self.assertNotIn("source", key)
