import unittest
from report_format import normalize_report, standardize_staff_report


class ReportFormatTests(unittest.TestCase):
    def test_removes_model_reasoning_wrapper_and_markdown_fence(self):
        raw='Thought: I should make a plan.\nAction: Final Answer\n```markdown\n# Plan\n\n**Actions**\n*   Ship it\n```'
        self.assertEqual(normalize_report(raw),'# Plan\n\n**Actions**\n- Ship it')

    def test_preserves_normal_report_content(self):
        report='# Report\n\n1. First action\n\n- Known fact\n- Assumption'
        self.assertEqual(normalize_report(report),report)

    def test_standardizes_common_agent_labels_into_one_company_layout(self):
        report=standardize_staff_report('Launch update','**Assumptions:**\n- Price pending\n\n**Next action:** Confirm price','Avery')
        self.assertTrue(report.startswith('# Launch update\n\nPrepared by: Avery'))
        self.assertIn('## Summary\n\n- None recorded.',report)
        self.assertIn('## Assumptions and risks\n\n- Price pending',report)
        self.assertIn('## Next actions\n\nConfirm price',report)
        self.assertIn('## Decisions needed\n\n- None recorded.',report)
        self.assertNotIn('**',report)
        self.assertEqual(standardize_staff_report('Launch update',report,'Avery'),report)


if __name__=='__main__':unittest.main()
