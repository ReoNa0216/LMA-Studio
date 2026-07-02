import unittest
from pathlib import Path

from annotation_app.app import native_path_dialog_response


class NativePathDialogResponseTest(unittest.TestCase):
    def test_returns_selected_directory_path(self):
        result = native_path_dialog_response("directory", lambda: Path(r"D:\LIFMSProjects\Batch03"))

        self.assertEqual(
            result,
            {
                "ok": True,
                "kind": "directory",
                "path": r"D:\LIFMSProjects\Batch03",
                "cancelled": False,
            },
        )

    def test_marks_cancelled_file_dialog(self):
        result = native_path_dialog_response("file", lambda: None)

        self.assertEqual(
            result,
            {
                "ok": True,
                "kind": "file",
                "path": "",
                "cancelled": True,
            },
        )

    def test_rejects_unknown_dialog_kind(self):
        with self.assertRaises(ValueError):
            native_path_dialog_response("volume", lambda: None)


if __name__ == "__main__":
    unittest.main()
