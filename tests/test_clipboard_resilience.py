"""Unit tests for the clipboard retry helpers used by get_selected_text.

The Windows clipboard is a shared OS resource that any app can transiently
hold, so pyperclip.paste()/copy() failing once doesn't mean the clipboard is
gone for good -- these helpers retry a bounded number of times before giving
up, instead of the previous behavior of failing (and silently skipping the
clipboard restore) on the very first hiccup.
"""

import unittest
from unittest import mock

import hover_translate as ht


class ClipboardPasteRetryTests(unittest.TestCase):
    def test_succeeds_immediately_without_retrying(self):
        with mock.patch.object(ht.pyperclip, "paste", return_value="hello") as paste, \
                mock.patch.object(ht.time, "sleep"):
            result = ht._clipboard_paste_with_retry()
        self.assertEqual(result, "hello")
        self.assertEqual(paste.call_count, 1)

    def test_recovers_after_transient_failures(self):
        paste = mock.Mock(side_effect=[OSError("locked"), OSError("locked"), "hello"])
        with mock.patch.object(ht.pyperclip, "paste", paste), \
                mock.patch.object(ht.time, "sleep"):
            result = ht._clipboard_paste_with_retry()
        self.assertEqual(result, "hello")
        self.assertEqual(paste.call_count, 3)

    def test_returns_none_after_exhausting_all_attempts(self):
        paste = mock.Mock(side_effect=OSError("locked"))
        with mock.patch.object(ht.pyperclip, "paste", paste), \
                mock.patch.object(ht.time, "sleep"):
            result = ht._clipboard_paste_with_retry()
        self.assertIsNone(result)
        self.assertEqual(paste.call_count, ht.CLIPBOARD_RETRY_ATTEMPTS)

    def test_empty_clipboard_is_distinct_from_a_read_failure(self):
        """A legitimately empty clipboard ("") must not be confused with an
        unreadable one (None) -- get_selected_text's restore logic depends
        on telling these apart."""
        with mock.patch.object(ht.pyperclip, "paste", return_value=""), \
                mock.patch.object(ht.time, "sleep"):
            result = ht._clipboard_paste_with_retry()
        self.assertEqual(result, "")
        self.assertIsNotNone(result)


class ClipboardCopyRetryTests(unittest.TestCase):
    def test_succeeds_immediately_without_retrying(self):
        with mock.patch.object(ht.pyperclip, "copy") as copy, \
                mock.patch.object(ht.time, "sleep"):
            result = ht._clipboard_copy_with_retry("original text")
        self.assertTrue(result)
        copy.assert_called_once_with("original text")

    def test_recovers_after_transient_failures(self):
        copy = mock.Mock(side_effect=[OSError("locked"), None])
        with mock.patch.object(ht.pyperclip, "copy", copy), \
                mock.patch.object(ht.time, "sleep"):
            result = ht._clipboard_copy_with_retry("original text")
        self.assertTrue(result)
        self.assertEqual(copy.call_count, 2)

    def test_returns_false_after_exhausting_all_attempts(self):
        copy = mock.Mock(side_effect=OSError("locked"))
        with mock.patch.object(ht.pyperclip, "copy", copy), \
                mock.patch.object(ht.time, "sleep"):
            result = ht._clipboard_copy_with_retry("original text")
        self.assertFalse(result)
        self.assertEqual(copy.call_count, ht.CLIPBOARD_RETRY_ATTEMPTS)


if __name__ == "__main__":
    unittest.main()
