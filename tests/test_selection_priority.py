"""Coverage for get_selected_text's safety guards and edge cases.

These were previously untested: the console-window skip (Ctrl+C means SIGINT
there, so it must never be sent), the cursor-vs-focus mismatch guard, and the
distinction between "no selection" and "unsafe to try" outcomes.
"""

import unittest
from unittest import mock

import hover_translate as ht


def fake_kb_controller():
    controller = mock.Mock()
    controller.press = mock.Mock()
    controller.release = mock.Mock()
    return controller


class GetSelectedTextGuardTests(unittest.TestCase):
    def test_returns_none_and_sends_nothing_when_priority_disabled(self):
        controller = fake_kb_controller()
        with mock.patch.object(ht, "SELECTION_PRIORITY_ENABLED", False), \
                mock.patch.object(ht, "cursor_is_over_focused_window") as guard:
            result = ht.get_selected_text(controller, 10, 10)
        self.assertIsNone(result)
        guard.assert_not_called()
        controller.press.assert_not_called()

    def test_returns_none_and_sends_nothing_when_cursor_not_over_focused_window(self):
        controller = fake_kb_controller()
        with mock.patch.object(ht, "cursor_is_over_focused_window", return_value=False), \
                mock.patch.object(ht, "focused_window_is_console") as console_guard:
            result = ht.get_selected_text(controller, 10, 10)
        self.assertIsNone(result)
        console_guard.assert_not_called()
        controller.press.assert_not_called()

    def test_never_sends_ctrl_c_to_a_console_window(self):
        """The one safety property that matters most here: Ctrl+C in a
        console sends SIGINT, not copy, and could kill a running process."""
        controller = fake_kb_controller()
        with mock.patch.object(ht, "cursor_is_over_focused_window", return_value=True), \
                mock.patch.object(ht, "focused_window_is_console", return_value=True):
            result = ht.get_selected_text(controller, 10, 10)
        self.assertIsNone(result)
        controller.press.assert_not_called()
        controller.release.assert_not_called()


class GetSelectedTextClipboardOutcomeTests(unittest.TestCase):
    def _run(self, original, copied):
        controller = fake_kb_controller()
        paste = mock.patch.object(
            ht, "_clipboard_paste_with_retry", side_effect=[original, copied]
        )
        with mock.patch.object(ht, "cursor_is_over_focused_window", return_value=True), \
                mock.patch.object(ht, "focused_window_is_console", return_value=False), \
                paste, \
                mock.patch.object(ht, "_clipboard_copy_with_retry", return_value=True) as restore, \
                mock.patch.object(ht.time, "sleep"):
            result = ht.get_selected_text(controller, 10, 10)
        controller.press.assert_any_call("c")
        return result, restore

    def test_returns_stripped_japanese_selection(self):
        result, restore = self._run(original="", copied=" 日本語 ")
        self.assertEqual(result, "日本語")
        restore.assert_called_once_with("")

    def test_returns_none_when_clipboard_unchanged(self):
        """copied == original means Ctrl+C had nothing to copy (no selection)."""
        result, _restore = self._run(original="same", copied="same")
        self.assertIsNone(result)

    def test_returns_none_when_copied_text_has_no_japanese(self):
        result, _restore = self._run(original="", copied="just english text")
        self.assertIsNone(result)

    def test_returns_none_when_clipboard_read_after_copy_fails(self):
        result, _restore = self._run(original="", copied=None)
        self.assertIsNone(result)

    def test_warns_but_does_not_crash_when_original_clipboard_unreadable(self):
        controller = fake_kb_controller()
        with mock.patch.object(ht, "cursor_is_over_focused_window", return_value=True), \
                mock.patch.object(ht, "focused_window_is_console", return_value=False), \
                mock.patch.object(
                    ht, "_clipboard_paste_with_retry", side_effect=[None, "日本語"]
                ), \
                mock.patch.object(ht, "_clipboard_copy_with_retry") as restore, \
                mock.patch.object(ht.time, "sleep"), \
                mock.patch.object(ht.log, "warning") as warn:
            result = ht.get_selected_text(controller, 10, 10)
        # original was unreadable (None) -- nothing to restore, and no restore
        # attempt should be made against an unknown prior value.
        restore.assert_not_called()
        warn.assert_called_once()
        self.assertEqual(result, "日本語")

    def test_warns_when_restoring_the_original_clipboard_fails(self):
        result, restore = self._run(original="prior", copied="日本語")
        self.assertEqual(result, "日本語")
        restore.assert_called_once_with("prior")

        controller = fake_kb_controller()
        with mock.patch.object(ht, "cursor_is_over_focused_window", return_value=True), \
                mock.patch.object(ht, "focused_window_is_console", return_value=False), \
                mock.patch.object(
                    ht, "_clipboard_paste_with_retry", side_effect=["prior", "日本語"]
                ), \
                mock.patch.object(ht, "_clipboard_copy_with_retry", return_value=False), \
                mock.patch.object(ht.time, "sleep"), \
                mock.patch.object(ht.log, "warning") as warn:
            ht.get_selected_text(controller, 10, 10)
        warn.assert_called_once()


if __name__ == "__main__":
    unittest.main()
