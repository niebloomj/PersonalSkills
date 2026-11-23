import unittest
from pathlib import Path


class WorkflowTests(unittest.TestCase):
    def test_update_branches_are_unique_per_attempt_or_per_reviewed_change(self):
        root = Path(__file__).resolve().parents[1]
        workflow = (root / ".github/workflows/check-upstream-skills.yml").read_text(
            encoding="utf-8"
        )
        self.assertIn("RUN_LABEL: ${{ github.run_id }}-${{ github.run_attempt }}", workflow)
        self.assertIn('branch="automation/upstream-updates-$RUN_LABEL"', workflow)
        self.assertIn('branch="automation/upstream-review-$digest"', workflow)


if __name__ == "__main__":
    unittest.main()
