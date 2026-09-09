import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from codex_glass.core.usage import default_codex_sessions_dir, default_config_path
from codex_glass.storage.index import default_index_path


class PortableDiscoveryTests(unittest.TestCase):
    def test_custom_codex_home_controls_sessions_config_and_index(self):
        with tempfile.TemporaryDirectory(prefix="codex 用户 ") as folder:
            root = Path(folder).resolve()
            with patch.dict(os.environ, {"CODEX_HOME": folder}):
                self.assertEqual(root / "sessions", default_codex_sessions_dir())
                self.assertEqual(root / "monitor_config.json", default_config_path())
                self.assertEqual(root / "codex-monitor.sqlite3", default_index_path())

    def test_empty_codex_home_preserves_existing_default_index(self):
        with patch.dict(os.environ, {"CODEX_HOME": ""}):
            self.assertEqual(Path.home() / ".codex" / "sessions", default_codex_sessions_dir())
            self.assertEqual(Path.home() / ".codex" / "codex-monitor.sqlite3", default_index_path())
