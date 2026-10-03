import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import github_sync

class BackupTests(unittest.TestCase):
    def test_secret_blocks_before_fetch_or_push(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.git').mkdir();(root/'.env').write_text('GROQ_API_KEY=private-value-for-test\n');(root/'main.py').write_text('key="private-value-for-test"')
            def fake_git(*args):
                if args[:2]==('remote','get-url'):return b'https://github.com/example/private-project.git'
                if args[0]=='diff':return b''
                if args[0]=='ls-files':return b'main.py\0'
                raise AssertionError('Network or mutation reached despite secret')
            with patch.object(github_sync,'ROOT',root),patch.object(github_sync,'git',side_effect=fake_git):
                with self.assertRaisesRegex(RuntimeError,'credentials detected'):github_sync.sync()
    def test_embedded_remote_credentials_rejected(self):
        with tempfile.TemporaryDirectory() as folder:
            root=Path(folder);(root/'.git').mkdir()
            with patch.object(github_sync,'ROOT',root),patch.object(github_sync,'git',return_value=b'https://token@github.com/example/repo.git'):
                with self.assertRaisesRegex(RuntimeError,'without embedded credentials'):github_sync.sync()

if __name__=='__main__':unittest.main()
