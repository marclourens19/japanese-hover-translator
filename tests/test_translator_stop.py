"""Coverage for HoverTranslator.stop()'s join-timeout and failure paths.

Previously untested: whether the warning fires when the translation worker
doesn't stop within the join timeout, and whether _sct.close() still runs
when the join itself raises unexpectedly (the try/except added around it).
"""

import queue
import unittest
from unittest import mock

import hover_translate as ht


def bare_translator_for_stop():
    translator = ht.HoverTranslator.__new__(ht.HoverTranslator)
    translator.running = True
    translator._latest_translation_job_id = 1
    translator._translation_queue = queue.Queue()
    return translator


class HoverTranslatorStopTests(unittest.TestCase):
    def test_stop_logs_no_warning_when_worker_stops_within_timeout(self):
        translator = bare_translator_for_stop()
        thread = mock.Mock()
        thread.is_alive.side_effect = [True, False]
        translator._translation_thread = thread
        translator._sct = mock.Mock()

        with mock.patch.object(ht.log, "warning") as warn, \
                mock.patch.object(ht.log, "exception") as exc:
            translator.stop()

        thread.join.assert_called_once_with(timeout=2.0)
        translator._sct.close.assert_called_once()
        warn.assert_not_called()
        exc.assert_not_called()
        self.assertFalse(translator.running)

    def test_stop_warns_but_still_closes_capture_when_worker_does_not_stop(self):
        translator = bare_translator_for_stop()
        thread = mock.Mock()
        thread.is_alive.return_value = True  # still alive before and after join
        translator._translation_thread = thread
        translator._sct = mock.Mock()

        with mock.patch.object(ht.log, "warning") as warn:
            translator.stop()

        thread.join.assert_called_once_with(timeout=2.0)
        translator._sct.close.assert_called_once()
        warn.assert_called_once()
        self.assertIn("did not stop", warn.call_args.args[0])

    def test_stop_still_closes_capture_if_join_raises_unexpectedly(self):
        translator = bare_translator_for_stop()
        thread = mock.Mock()
        thread.is_alive.return_value = True
        thread.join.side_effect = RuntimeError("boom")
        translator._translation_thread = thread
        translator._sct = mock.Mock()

        with mock.patch.object(ht.log, "exception") as exc:
            translator.stop()

        translator._sct.close.assert_called_once()
        exc.assert_called_once()

    def test_stop_logs_exception_but_does_not_raise_when_capture_close_fails(self):
        translator = bare_translator_for_stop()
        thread = mock.Mock()
        thread.is_alive.side_effect = [True, False]
        translator._translation_thread = thread
        translator._sct = mock.Mock()
        translator._sct.close.side_effect = OSError("capture already gone")

        with mock.patch.object(ht.log, "exception") as exc:
            translator.stop()  # must not raise

        exc.assert_called_once()

    def test_stop_drains_and_terminates_the_translation_queue(self):
        translator = bare_translator_for_stop()
        translator._translation_queue.put_nowait(("stale-job", "text", []))
        thread = mock.Mock()
        thread.is_alive.side_effect = [True, False]
        translator._translation_thread = thread
        translator._sct = mock.Mock()

        translator.stop()

        # the stale job must be gone and replaced with exactly the sentinel
        self.assertEqual(translator._translation_queue.get_nowait(), None)
        self.assertTrue(translator._translation_queue.empty())
        self.assertIsNone(translator._latest_translation_job_id)


if __name__ == "__main__":
    unittest.main()
