import os
import subprocess
import sys
import tempfile
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))


class DbPathTests(unittest.TestCase):
    def test_db_path_creates_database_in_missing_directory(self):
        # Processo separato: app.db crea l'engine all'import e non va ricaricato qui.
        with tempfile.TemporaryDirectory() as tmp:
            db_path = os.path.join(tmp, "volume", "takeit.db")
            env = {**os.environ, "DB_PATH": db_path}
            result = subprocess.run(
                [sys.executable, "-c", "from app.db import create_db_and_tables; create_db_and_tables()"],
                cwd=ROOT,
                env=env,
                capture_output=True,
                text=True,
            )

            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(os.path.isfile(db_path))
            self.assertIn(db_path, result.stdout)
            self.assertFalse(os.path.exists(os.path.join(tmp, "takeit.db")))


if __name__ == "__main__":
    unittest.main()
