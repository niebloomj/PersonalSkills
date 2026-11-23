import unittest
from pathlib import Path

WORKFLOW = Path(__file__).resolve().parents[1] / ".github/workflows/check-upstream-skills.yml"


class WorkflowTests(unittest.TestCase):
    def setUp(self):
        self.workflow = WORKFLOW.read_text(encoding="utf-8")

    def test_update_branches_are_unique_per_attempt_or_per_reviewed_change(self):
        self.assertIn("RUN_LABEL: ${{ github.run_id }}-${{ github.run_attempt }}", self.workflow)
        self.assertIn('branch="automation/upstream-updates-$RUN_LABEL"', self.workflow)
        self.assertIn('branch="automation/upstream-review-$digest"', self.workflow)

    def test_every_automated_commit_includes_the_generated_marketplace(self):
        commits = self.workflow.count("git commit -m")
        self.assertEqual(commits, 2)
        self.assertEqual(
            self.workflow.count("git add catalog.json licenses/ skills/ .claude-plugin/"), commits
        )


if __name__ == "__main__":
    unittest.main()
