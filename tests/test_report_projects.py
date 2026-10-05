import tempfile
import unittest
from pathlib import Path

from report_projects import active_project, create_report, delete_project, delete_report, ensure_project, migrate_legacy_reports, projects


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

    def test_manual_reports_and_project_deletion_stay_inside_report_root(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);ensure_project(root,'Holiday Launch','Holiday Launch',activate=True)
            saved=create_report(root,'holiday-launch','Avery','Email campaign','# Campaign\n\nReady for review.')
            path=root/'reports'/saved['id'];self.assertTrue(path.is_file())
            delete_report(root,saved['id']);self.assertFalse(path.exists())
            ensure_project(root,'Second Project','Second Project',activate=True)
            active=delete_project(root,'holiday-launch')
            self.assertEqual(active,'second-project');self.assertFalse((root/'reports/holiday-launch').exists())
            with self.assertRaises(ValueError):delete_report(root,'../outside.md')


if __name__=='__main__':unittest.main()
