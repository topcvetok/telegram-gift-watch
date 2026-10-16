import shutil
import sqlite3
import tempfile
import unittest
from pathlib import Path

from checkpoint_sqlite import checkpoint


class CheckpointSqliteTests(unittest.TestCase):
    def test_checkpoint_moves_committed_wal_data_into_main_database(self):
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "settings.sqlite3"
            restored = Path(directory) / "restored.sqlite3"
            writer = sqlite3.connect(source)
            try:
                writer.execute("PRAGMA journal_mode=WAL")
                writer.execute("CREATE TABLE runtime_settings (key TEXT, value TEXT)")
                writer.execute("INSERT INTO runtime_settings VALUES ('max_lot_rub', '2500')")
                writer.commit()
                self.assertTrue(Path(f"{source}-wal").exists())

                self.assertTrue(checkpoint(source))
                shutil.copyfile(source, restored)
            finally:
                writer.close()

            with sqlite3.connect(restored) as db:
                value = db.execute(
                    "SELECT value FROM runtime_settings WHERE key = 'max_lot_rub'"
                ).fetchone()[0]
            self.assertEqual(value, "2500")

    def test_missing_database_is_ignored(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertFalse(checkpoint(Path(directory) / "missing.sqlite3"))


if __name__ == "__main__":
    unittest.main()
