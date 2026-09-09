import hashlib
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class PackageLayoutTests(unittest.TestCase):
    def run_python(self, *arguments: str, cwd: Path, package_path: bool = False) -> subprocess.CompletedProcess[str]:
        environment = os.environ.copy()
        environment.pop("PYTHONPATH", None)
        if package_path:
            environment["PYTHONPATH"] = str(PROJECT_ROOT)
        return subprocess.run(
            [sys.executable, *arguments],
            cwd=cwd,
            capture_output=True,
            encoding="utf-8",
            env=environment,
            errors="replace",
            timeout=30,
        )

    def test_storage_import_does_not_load_qt(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.run_python(
                "-c",
                ("import sys; " "import codex_glass.storage.index; " "assert 'PyQt5' not in sys.modules"),
                cwd=Path(temporary_directory),
                package_path=True,
            )

        self.assertEqual(0, result.returncode, result.stderr)

    def test_source_resources_resolve_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            result = self.run_python(
                "-c",
                (
                    "from codex_glass.resources import resource_path; "
                    "print(resource_path('assets', 'codex-glass.png').read_bytes()[:8].hex())"
                ),
                cwd=Path(temporary_directory),
                package_path=True,
            )

        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("89504e470d0a1a0a", result.stdout.strip())

    def test_frozen_resources_resolve_from_pyinstaller_bundle_root(self):
        from codex_glass.resources import resource_path

        with (
            tempfile.TemporaryDirectory() as temporary_directory,
            mock.patch.object(sys, "frozen", True, create=True),
            mock.patch.object(sys, "_MEIPASS", temporary_directory, create=True),
        ):
            self.assertEqual(
                Path(temporary_directory) / "assets" / "web" / "dashboard.html",
                resource_path("assets", "web", "dashboard.html"),
            )

    def test_root_launchers_support_help_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cwd = Path(temporary_directory)
            for name in ("monitor.py", "web_dashboard.py", "desktop_widget.py"):
                with self.subTest(name=name):
                    result = self.run_python(str(PROJECT_ROOT / name), "--help", cwd=cwd)
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn("usage:", result.stdout.lower())

    def test_terminal_modes_run_with_relative_arguments_from_unrelated_working_directory(self):
        with tempfile.TemporaryDirectory() as temporary_directory:
            cwd = Path(temporary_directory)
            (cwd / "relative-sessions").mkdir()
            records = (
                {"timestamp": "2026-09-08T01:00:00Z", "type": "session_meta", "payload": {"cwd": "relative-project"}},
                {"timestamp": "2026-09-08T01:00:01Z", "type": "turn_context", "payload": {"model": "gpt-5"}},
                {
                    "timestamp": "2026-09-08T01:00:02Z",
                    "type": "token_count",
                    "payload": {
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 100,
                                "cached_input_tokens": 20,
                                "output_tokens": 10,
                                "reasoning_output_tokens": 2,
                                "total_tokens": 110,
                            }
                        }
                    },
                },
                {
                    "timestamp": "2026-09-08T01:01:02Z",
                    "type": "token_count",
                    "payload": {
                        "info": {
                            "total_token_usage": {
                                "input_tokens": 170,
                                "cached_input_tokens": 30,
                                "output_tokens": 25,
                                "reasoning_output_tokens": 4,
                                "total_tokens": 195,
                            }
                        }
                    },
                },
            )
            (cwd / "relative-sessions" / "one.jsonl").write_text(
                "".join(json.dumps(record, separators=(",", ":")) + "\n" for record in records),
                encoding="utf-8",
            )
            common = (
                "--sessions-dir",
                "relative-sessions",
                "--config",
                "relative-config.json",
                "--cwd",
                "relative-project",
            )
            for mode, extra in (("simple", ()), ("enhanced", ("--once",))):
                with self.subTest(mode=mode):
                    result = self.run_python(
                        str(PROJECT_ROOT / "monitor.py"),
                        mode,
                        *common,
                        *extra,
                        cwd=cwd,
                    )
                    self.assertEqual(0, result.returncode, result.stderr)
                    self.assertIn("195", result.stdout)

    def test_external_dashboard_html_preserves_baseline_bytes(self):
        from codex_glass.resources import resource_path
        from codex_glass.services.dashboard import HTML_PAGE

        html_bytes = resource_path("assets", "web", "dashboard.html").read_bytes()
        self.assertEqual(HTML_PAGE.encode("utf-8"), html_bytes)
        self.assertEqual(
            "cd8dc5a577737af2f9ca457d0009798842b812ebcb5d720ab1ed73564c1ac618",
            hashlib.sha256(html_bytes).hexdigest(),
        )


if __name__ == "__main__":
    unittest.main()
