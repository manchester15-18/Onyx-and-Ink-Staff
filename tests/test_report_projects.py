import tempfile
import unittest
from pathlib import Path

from report_projects import active_project, ensure_project, migrate_legacy_reports, projects


class ReportProjectTests(unittest.TestCase):
    def test_legacy_reports_move_into_default_project(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);reports=root/'reports';reports.mkdir();(reports/'marketing_campaign.md').write_text('# Marketing')
            destination=migrate_legacy_reports(root)
            self.assertTrue((destination/'marketing_campaign.md').is_file())
            self.assertFalse((reports/'marketing_campaign.md').exists())

    def test_selected_project_becomes_agent_default(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder)
            slug,label,path=ensure_project(root,'Christmas 2026','Christmas 2026',activate=True)
            self.assertEqual((slug,label),('christmas-2026','Christmas 2026'))
            self.assertEqual(active_project(root)[0],slug)
            self.assertTrue(path.is_dir())
            self.assertTrue(next(item for item in projects(root) if item['id']==slug)['active'])


if __name__=='__main__':unittest.main()
