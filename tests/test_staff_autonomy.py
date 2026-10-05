import tempfile
import json
import time
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock,patch
from staff_autonomy import StaffAutonomy,in_window


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
            root=Path(folder);path=root/'reports'/'holiday-launch';path.mkdir(parents=True);(path/'operational_plan.md').write_text('# Existing plan\nCompleted: research')
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


if __name__=='__main__':unittest.main()
