import unittest

from quality_browser import result_status


class BrowserAdapterStatusTests(unittest.TestCase):
    def test_nonpassing_statuses_block_adapter_success(self):
        self.assertEqual(result_status(["PASS", "PASS"]), "PASS")
        for status in ("FAIL", "UNSUPPORTED", "KNOWN_FAIL", "NOT_RUN", "FLAKY"):
            with self.subTest(status=status):
                self.assertEqual(result_status(["PASS", status]), status)

    def test_omitted_or_unknown_status_cannot_admit(self):
        self.assertEqual(result_status([]), "FAIL")
        self.assertEqual(result_status(["UNKNOWN"]), "FAIL")


if __name__ == "__main__":
    unittest.main()
