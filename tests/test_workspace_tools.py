import unittest
from types import SimpleNamespace

from unittest.mock import patch
from workspace_tools import Workspace,WorkspaceSetupError,verified_account


class WorkspaceToolsTests(unittest.TestCase):
    def test_selected_google_account_is_verified_without_alias_restriction(self):
        response=SimpleNamespace(ok=True,status_code=200,json=lambda:{'user':{'emailAddress':'owner@gmail.com'}})
        self.assertEqual(verified_account(response),'owner@gmail.com')

    def test_disabled_drive_api_has_specific_instruction(self):
        response=SimpleNamespace(ok=False,status_code=403,json=lambda:{'error':{'errors':[{'reason':'accessNotConfigured'}]}})
        with self.assertRaisesRegex(WorkspaceSetupError,'Enable the Google Drive API'):verified_account(response)

    def test_replace_doc_clears_existing_body_and_inserts_replacement(self):
        workspace=Workspace('/tmp/example')
        with patch.object(workspace,'read_doc',return_value={'body':{'content':[{'endIndex':14}]}}),patch.object(workspace,'own',return_value='doc'),patch.object(workspace,'request',return_value={}) as request:
            workspace.replace_doc('doc','Replacement')
            operations=request.call_args.kwargs['json']['requests']
            self.assertEqual(operations[0]['deleteContentRange']['range'],{'startIndex':1,'endIndex':13})
            self.assertEqual(operations[1]['insertText']['text'],'Replacement\n')


if __name__=='__main__':unittest.main()
