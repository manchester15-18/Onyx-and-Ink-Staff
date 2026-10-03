import unittest
from types import SimpleNamespace

from workspace_tools import WorkspaceSetupError,verified_account


class WorkspaceToolsTests(unittest.TestCase):
    def test_selected_google_account_is_verified_without_alias_restriction(self):
        response=SimpleNamespace(ok=True,status_code=200,json=lambda:{'user':{'emailAddress':'owner@gmail.com'}})
        self.assertEqual(verified_account(response),'owner@gmail.com')

    def test_disabled_drive_api_has_specific_instruction(self):
        response=SimpleNamespace(ok=False,status_code=403,json=lambda:{'error':{'errors':[{'reason':'accessNotConfigured'}]}})
        with self.assertRaisesRegex(WorkspaceSetupError,'Enable the Google Drive API'):verified_account(response)


if __name__=='__main__':unittest.main()
