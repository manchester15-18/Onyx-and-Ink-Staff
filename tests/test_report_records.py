import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

from report_records import export_google_doc, is_reference_report, metadata, report_pdf


class ReportRecordTests(unittest.TestCase):
    def make_report(self, root, text="# Holiday Launch\n\nReady."):
        path=Path(root)/"reports"/"holiday"/"assignments"/"one"/"avery.md"
        path.parent.mkdir(parents=True);path.write_text(text)
        return path

    def test_assignment_title_and_stable_created_metadata(self):
        with tempfile.TemporaryDirectory() as d:
            path=self.make_report(d);first=metadata(d,path)
            time.sleep(.01);path.write_text("# Holiday Launch\n\nRevised.")
            second=metadata(d,path)
            self.assertEqual(second["title"],"Holiday Launch")
            self.assertEqual(first["created"],second["created"])
            self.assertNotEqual(first["edited"],second["edited"])

    def test_reference_policy_detection(self):
        self.assertTrue(is_reference_report("Custom Orders", "Terms and Conditions for custom work"))
        self.assertTrue(is_reference_report("Employee Handbook", "Office hours"))
        self.assertFalse(is_reference_report("Holiday Instagram Captions", "Five product captions"))

    def test_google_export_updates_existing_document(self):
        with tempfile.TemporaryDirectory() as d, patch("workspace_tools.Workspace.create_doc") as create, patch("workspace_tools.Workspace.replace_doc") as replace:
            path=self.make_report(d,"# Return Policy\n\nReturns guidance.")
            create.return_value={"id":"doc-one","url":"https://docs.google.com/document/d/doc-one/edit"}
            first=export_google_doc(d,str(path.relative_to(Path(d)/"reports")),force=True)
            same=export_google_doc(d,str(path.relative_to(Path(d)/"reports")),force=True)
            path.write_text("# Return Policy\n\nUpdated returns guidance.")
            updated=export_google_doc(d,str(path.relative_to(Path(d)/"reports")),force=True)
            self.assertEqual(first["id"],"doc-one");self.assertTrue(same["unchanged"])
            create.assert_called_once();replace.assert_called_once();self.assertEqual(updated["id"],"doc-one")

    def test_pdf_export_is_valid_and_contains_pages(self):
        with tempfile.TemporaryDirectory() as d:
            path=self.make_report(d);title,data=report_pdf(d,str(path.relative_to(Path(d)/"reports")))
            self.assertEqual(title,"Holiday Launch")
            self.assertTrue(data.startswith(b"%PDF-1.4"));self.assertIn(b"/Type /Page",data)


if __name__=="__main__":unittest.main()
