import unittest
from pathlib import Path


class WorkflowTests(unittest.TestCase):
    def test_update_branch_is_unique_for_each_run_attempt(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/check-upstream-skills.yml").read_text(encoding="utf-8")
        self.assertIn("GITHUB_RUN_ATTEMPT: ${{ github.run_attempt }}", workflow)
        self.assertIn(
            'branch="automation/upstream-skills-${GITHUB_RUN_ID}-${GITHUB_RUN_ATTEMPT}"',
            workflow,
        )


if __name__ == "__main__":
    unittest.main()
