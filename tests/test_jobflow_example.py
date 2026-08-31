from __future__ import annotations

import json
import sys
import tempfile
import threading
import unittest
from pathlib import Path
from unittest.mock import patch

from qa_sentinel.config import load_suite
from qa_sentinel.reporting import build_report, write_html_report, write_json_report, write_junit_report
from qa_sentinel.runner import SuiteRunner


PORTFOLIO_ROOT = Path(__file__).resolve().parents[2]
# Local portfolio checkouts keep JobFlow beside this repository. In CI, the
# sibling checkout is placed in the workspace root instead, so accept both
# layouts and skip cleanly when this optional integration dependency is absent.
JOBFLOW_ROOT = PORTFOLIO_ROOT / "jobflow"
if not JOBFLOW_ROOT.is_dir():
    JOBFLOW_ROOT = Path(__file__).resolve().parents[1] / "jobflow"
if str(JOBFLOW_ROOT) not in sys.path:
    sys.path.insert(0, str(JOBFLOW_ROOT))

try:
    from jobflow.server import build_server  # noqa: E402
except ModuleNotFoundError:  # The sibling app is optional in standalone clones.
    build_server = None


EXAMPLE_ROOT = Path(__file__).parents[1] / "examples" / "jobflow"


@unittest.skipUnless(
    build_server is not None,
    "JobFlow sibling checkout is not available; skipping optional cross-project tests",
)
class JobFlowIntegrationTests(unittest.TestCase):
    def run_suite(self, filename: str):
        with tempfile.TemporaryDirectory() as directory:
            db_path = Path(directory) / "jobflow.db"
            server = build_server(
                "127.0.0.1",
                0,
                database_path=str(db_path),
                static_dir=JOBFLOW_ROOT / "static",
                seed_demo=False,
            )
            thread = threading.Thread(target=server.serve_forever, daemon=True)
            thread.start()
            try:
                base_url = f"http://127.0.0.1:{server.server_port}"
                suite = load_suite(EXAMPLE_ROOT / filename, {"base_url": base_url})
                with patch.dict(
                    "os.environ",
                    {
                        "GITHUB_SHA": "jobflow-test-sha",
                        "GITHUB_REF_NAME": "main",
                        "GITHUB_RUN_URL": "https://ci.example.test/jobflow/1",
                    },
                ):
                    result = SuiteRunner().run(suite)
                yield result, server, db_path
            finally:
                server.shutdown()
                server.server_close()
                thread.join(timeout=2)

    def test_passing_workflow_proves_lifecycle_stale_conflict_and_cleanup(self) -> None:
        for result, server, _ in self.run_suite("jobflow-workflow.json"):
            self.assertTrue(result.is_successful, [test.status for test in result.tests])
            self.assertEqual(result.total, 6)
            self.assertEqual(result.passed, 6)
            self.assertEqual(result.tests[4].response.status, 409)
            self.assertEqual(server.database.list_applications({})["total"], 0)
            self.assertTrue(result.suite_config_hash)

    def test_failure_path_keeps_cleanup_and_writes_three_artifacts(self) -> None:
        for result, server, db_path in self.run_suite("failure-workflow.json"):
            self.assertFalse(result.is_successful)
            self.assertEqual(result.failed, 1)
            self.assertEqual(result.passed, 2)
            self.assertEqual(result.tests[-1].case.case_id, "cleanup")
            self.assertEqual(result.tests[-1].status, "passed")
            self.assertEqual(server.database.list_applications({})["total"], 0)
            with tempfile.TemporaryDirectory() as artifacts:
                root = Path(artifacts)
                write_json_report(result, root / "report.json")
                write_html_report(result, root / "report.html")
                write_junit_report(result, root / "report.xml")
                report = json.loads((root / "report.json").read_text(encoding="utf-8"))
                self.assertEqual(report["provenance"]["git_sha"], "jobflow-test-sha")
                self.assertEqual(report["summary"]["failed"], 1)
                self.assertIn("broken-contract", (root / "report.html").read_text(encoding="utf-8"))
                self.assertIn("jobflow-test-sha", (root / "report.xml").read_text(encoding="utf-8"))

    def test_temp_database_is_not_written_to_the_repository(self) -> None:
        for _, _, db_path in self.run_suite("jobflow-workflow.json"):
            self.assertNotIn(JOBFLOW_ROOT, db_path.parents)


if __name__ == "__main__":
    unittest.main()
