from __future__ import annotations

import json
import io
import tempfile
import unittest
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

from qa_sentinel.config import load_suite
from qa_sentinel.cli import main
from qa_sentinel.scaffold import ScaffoldConflict, generated_files, init_project


class ScaffoldTests(unittest.TestCase):
    def test_init_creates_minimal_valid_starter(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            created = init_project(root)
            self.assertEqual(set(created), set(generated_files()))
            self.assertTrue((root / "suites/smoke.json").exists())
            self.assertTrue((root / ".github/workflows/qa-sentinel.yml").exists())
            suite = load_suite(root / "suites/smoke.json")
            self.assertEqual(suite.tests[0].case_id, "health")
            self.assertEqual(suite.tests[0].assertions[0].kind, "status")
            self.assertIn("qa-sentinel doctor", (root / "README-qa-sentinel.md").read_text())

    def test_repeat_init_refuses_without_modifying_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_project(root)
            before = {
                path: path.read_bytes()
                for path in (root / relative for relative in generated_files())
            }
            with self.assertRaises(ScaffoldConflict) as raised:
                init_project(root)
            self.assertEqual(set(raised.exception.conflicts), set(before))
            after = {path: path.read_bytes() for path in before}
            self.assertEqual(after, before)

    def test_partial_conflict_lists_all_existing_generated_files_before_writing(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            conflict = root / "suites/smoke.json"
            conflict.parent.mkdir(parents=True)
            conflict.write_text(json.dumps({"keep": True}), encoding="utf-8")
            with self.assertRaises(ScaffoldConflict) as raised:
                init_project(root)
            self.assertEqual(raised.exception.conflicts, (conflict,))
            self.assertFalse((root / "README-qa-sentinel.md").exists())
            self.assertEqual(json.loads(conflict.read_text()), {"keep": True})

    def test_force_only_replaces_known_generated_files(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_project(root)
            custom = root / "notes.txt"
            custom.write_text("keep", encoding="utf-8")
            (root / "suites/smoke.json").write_text("{}", encoding="utf-8")
            init_project(root, force=True)
            self.assertEqual(custom.read_text(), "keep")
            self.assertEqual(load_suite(root / "suites/smoke.json").name, "Local smoke suite")

    def test_cli_init_prints_exact_next_commands(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            output = io.StringIO()
            with redirect_stdout(output):
                exit_code = main(["init", directory])
            self.assertEqual(exit_code, 0)
            text = output.getvalue()
            self.assertIn("qa-sentinel validate suites/smoke.json", text)
            self.assertIn("qa-sentinel doctor suites/smoke.json --env environments/local.json", text)
            self.assertIn("qa-sentinel run suites/smoke.json", text)

    def test_cli_init_conflict_is_non_destructive(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            init_project(root)
            original = (root / "suites/smoke.json").read_text()
            error = io.StringIO()
            with redirect_stderr(error):
                exit_code = main(["init", directory])
            self.assertEqual(exit_code, 2)
            self.assertIn("no files changed", error.getvalue())
            self.assertEqual((root / "suites/smoke.json").read_text(), original)


if __name__ == "__main__":
    unittest.main()
