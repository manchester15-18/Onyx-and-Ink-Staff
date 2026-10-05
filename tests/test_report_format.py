import unittest
from report_format import normalize_report


class ReportFormatTests(unittest.TestCase):
    def test_removes_model_reasoning_wrapper_and_markdown_fence(self):
        raw='Thought: I should make a plan.\nAction: Final Answer\n```markdown\n# Plan\n\n**Actions**\n*   Ship it\n```'
        self.assertEqual(normalize_report(raw),'# Plan\n\n**Actions**\n- Ship it')

    def test_preserves_normal_report_content(self):
        report='# Report\n\n1. First action\n\n- Known fact\n- Assumption'
        self.assertEqual(normalize_report(report),report)


if __name__=='__main__':unittest.main()
