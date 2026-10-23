import tempfile
import json
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock,patch
from staff_autonomy import StaffAutonomy,in_window,report_context
from report_projects import ensure_project, save_project_overview


class StaffAutonomyTests(unittest.TestCase):
    def test_schedule_supports_daytime_overnight_and_continuous(self):
        self.assertTrue(in_window('','',datetime(2026,1,1,12)))
        self.assertTrue(in_window('09:00','17:00',datetime(2026,1,1,12)))
        self.assertFalse(in_window('09:00','17:00',datetime(2026,1,1,20)))
        self.assertTrue(in_window('20:00','06:00',datetime(2026,1,1,23)))

    def test_settings_are_private_and_validated(self):
        with tempfile.TemporaryDirectory() as folder:
            manager=StaffAutonomy(folder)
            state=manager.configure({'enabled':True,'start':'08:00','stop':'18:00','objective':'Continue the approved holiday launch work.'})
            self.assertTrue(state['enabled']);self.assertEqual(manager.path.stat().st_mode & 0o777,0o600)
            with self.assertRaises(ValueError):manager.configure({'enabled':True,'start':'08:00','stop':'','objective':'short'})
            state=manager.configure({'enabled':True,'start':'','stop':'','objective':'Continue approved holiday work without idle time.'})
            self.assertNotIn('interval',state)

    def test_run_now_uses_saved_reports_without_overlapping(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);_,_,path=ensure_project(root,'holiday-launch','Holiday Launch',activate=True);(path/'operational_plan.md').write_text('# Existing plan\nCompleted: research')
            save_project_overview(root,'holiday-launch','# Holiday Launch\n\n## Background\nA prepared holiday offer needs coordinated launch work.\n\n## Goals\n- Publish approved launch materials.\n\n## Desired outcomes\n- A ready customer experience with clear ownership.')
            manager=StaffAutonomy(root);manager.configure({'enabled':True,'start':'23:00','stop':'23:30','objective':'Continue approved work.','runNow':True})
            process=Mock();process.poll.return_value=None
            with patch('staff_autonomy.subprocess.Popen',return_value=process) as launch:
                manager.tick()
            directive=launch.call_args.args[0][-1]
            self.assertIn('Existing plan',directive);self.assertIn('Do not repeat completed work',directive)
            self.assertIn('--assignment-id',launch.call_args.args[0]);self.assertIn('--project',launch.call_args.args[0])

    def test_failed_run_honors_provider_cooldown_without_relaunching(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'work').mkdir();ready=time.time()+900
            (root/'work'/'groq-cooldown.json').write_text(json.dumps({'readyEpoch':ready}))
            manager=StaffAutonomy(root);manager.configure({'enabled':True,'start':'','stop':'','objective':'Continue approved work without retry bursts.'})
            process=Mock(returncode=1);process.poll.return_value=1;manager.process=process
            with patch('staff_autonomy.subprocess.Popen') as launch:manager.tick()
            self.assertFalse(launch.called)
            self.assertGreaterEqual(manager.load()['capacityReadyEpoch'],ready-1)

    def test_context_excludes_reports_from_other_projects_and_legacy_nested_reports(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);_,_,active=ensure_project(root,'Current','Current',activate=True);_,_,other=ensure_project(root,'Old','Old')
            save_project_overview(root,'current','# Current\n\n## Background\nActive work.\n\n## Goals\n- Finish.\n\n## Desired outcomes\n- Verified result.')
            (active/'agent-reports').mkdir();(active/'agent-reports'/'jordan-web-it.md').write_text('Current Jordan action')
            (active/'agent-reports'/'agent-reports').mkdir();(active/'agent-reports'/'agent-reports'/'jordan-web-it.md').write_text('Stale nested action')
            (other/'agent-reports').mkdir();(other/'agent-reports'/'avery-marketing.md').write_text('Old project action')
            context=report_context(root)
            self.assertIn('Current Jordan action',context);self.assertNotIn('Stale nested action',context);self.assertNotIn('Old project action',context)

    def test_failed_run_marks_agents_waiting_and_records_backoff(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);manager=StaffAutonomy(root)
            manager.configure({'enabled':True,'start':'','stop':'','objective':'Continue approved work without idle time.'})
            process=Mock(returncode=1);process.poll.return_value=1;manager.process=process
            with patch('staff_autonomy.subprocess.Popen'):
                manager.tick()
            state=manager.load();status=json.loads((root/'work'/'staff-work-status.json').read_text())
            self.assertEqual(state['consecutiveFailures'],1)
            self.assertIn('retrying automatically',state['lastFailure'])
            self.assertEqual(status['agents']['Morgan'],'waiting')


if __name__=='__main__':unittest.main()
