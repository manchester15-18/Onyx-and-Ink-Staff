import unittest

from workspace_tools import allowed_account


class WorkspaceToolsTests(unittest.TestCase):
    def test_primary_workspace_account_is_allowed_for_same_domain_alias(self):
        self.assertTrue(allowed_account('owner@onyxandink.org','inbox@onyxandink.org'))
        self.assertTrue(allowed_account('inbox@onyxandink.org','inbox@onyxandink.org'))
        self.assertFalse(allowed_account('someone@gmail.com','inbox@onyxandink.org'))
        self.assertFalse(allowed_account('','inbox@onyxandink.org'))


if __name__=='__main__':unittest.main()
