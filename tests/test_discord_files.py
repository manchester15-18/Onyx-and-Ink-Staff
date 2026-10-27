"""Offline tests for Discord file handling: inbound attachments and
outbound agent-created files. No network, no Discord token."""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from staff.discord_bridge import DiscordBridge
from staff.multimodal import prepare


def _bridge(tmp):
    root = Path(tmp)
    (root / "work").mkdir(parents=True)
    return DiscordBridge(root, chat=None, stop=None)


class DiscordFilesTests(unittest.TestCase):
    def setUp(self):
        self.folder = tempfile.TemporaryDirectory(); self.addCleanup(self.folder.cleanup)
        self.root = Path(self.folder.name)
        self.bridge = _bridge(self.root)

    def test_svg_is_read_as_text_not_rejected(self):
        path = self.root / "logo.svg"
        path.write_text('<svg xmlns="http://www.w3.org/2000/svg"><circle r="5"/></svg>', encoding="utf-8")
        blocks, names = prepare([path])
        self.assertEqual(names, ["logo.svg"])
        self.assertIn("<circle", blocks[0]["text"])

    def test_json_is_read_as_text(self):
        path = self.root / "data.json"
        path.write_text('{"price": 5}', encoding="utf-8")
        blocks, _ = prepare([path])
        self.assertIn("price", blocks[0]["text"])

    def test_exe_still_rejected_by_prepare(self):
        path = self.root / "sample.exe"; path.write_bytes(b"test")
        with self.assertRaises(ValueError):
            prepare([path])

    def test_download_sanitizes_traversal_filename(self):
        data = {"id": "123", "attachments": [
            {"filename": "../evil.png", "size": 3, "url": "https://cdn.example/x"},
        ]}
        reply = Mock(ok=True, content=b"PNG")
        with patch("staff.discord_bridge.requests.get", return_value=reply):
            paths, skipped = self.bridge.download_attachments(data, "token")
        self.assertEqual(skipped, [])
        self.assertEqual(paths[0].name, "123--evil.png")
        self.assertTrue(paths[0].is_relative_to(self.root / "work" / "discord-uploads"))

    def test_download_skips_unsupported_type_instead_of_failing(self):
        data = {"id": "124", "attachments": [
            {"filename": "tool.exe", "size": 3, "url": "https://cdn.example/x"},
        ]}
        with patch("staff.discord_bridge.requests.get") as get:
            paths, skipped = self.bridge.download_attachments(data, "token")
        get.assert_not_called()
        self.assertEqual(paths, [])
        self.assertTrue(any("unsupported" in note for note in skipped))

    def test_artifact_id_resolves_to_local_file(self):
        folder = self.root / "work" / "artifacts"; folder.mkdir(parents=True)
        target = folder / ("a" * 32 + ".png"); target.write_bytes(b"PNG")
        answer = "Design by Avery: /api/artifact?id=" + "a" * 32
        self.assertEqual(self.bridge.resolve_answer_files(answer), [target.resolve()])

    def test_work_path_resolves_and_uploads_folder_is_not_eligible(self):
        artifacts = self.root / "work" / "artifacts"; artifacts.mkdir(parents=True)
        target = artifacts / "report.csv"; target.write_text("a,b\n", encoding="utf-8")
        uploads = self.root / "work" / "uploads"; uploads.mkdir(parents=True)
        user_file = uploads / "secret.csv"; user_file.write_text("x\n", encoding="utf-8")
        answer = "See work/artifacts/report.csv and work/uploads/secret.csv"
        self.assertEqual(self.bridge.resolve_answer_files(answer), [target.resolve()])

    def test_path_escape_is_rejected(self):
        folder = self.root / "work" / "artifacts"; folder.mkdir(parents=True)
        answer = "See work/artifacts/../../.env and /api/artifact?id=" + "b" * 32
        self.assertEqual(self.bridge.resolve_answer_files(answer), [])


if __name__ == "__main__":
    unittest.main()
