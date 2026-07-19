import queue
import unittest
from unittest import mock

import hover_translate as ht


def ocr_data(words):
    """Build pytesseract-style column data from row dictionaries."""
    fields = (
        "text", "conf", "block_num", "par_num", "line_num",
        "left", "top", "width", "height",
    )
    return {field: [row[field] for row in words] for field in fields}


def word(text, confidence=95.5, left=800, top=35, width=120, height=55, line=1):
    # Tesseract coordinates are 4x the capture coordinates. The capture cursor
    # is therefore at (840, 64), making this default box cursor-anchored.
    return {
        "text": text,
        "conf": str(confidence),
        "block_num": 1,
        "par_num": 1,
        "line_num": line,
        "left": left,
        "top": top,
        "width": width,
        "height": height,
    }


class OcrFilteringTests(unittest.TestCase):
    def test_cursor_anchored_japanese_with_float_confidence_is_kept(self):
        data = ocr_data([word("食べる", confidence=96.25)])
        self.assertEqual(ht.filter_tesseract_data(data), "食べる")

    def test_remote_text_does_not_trigger_empty_space_hover(self):
        data = ocr_data([word("日本語", left=50, top=200)])
        self.assertEqual(ht.filter_tesseract_data(data), "")

    def test_low_confidence_and_single_character_noise_are_rejected(self):
        low_line = ocr_data([word("日本語", confidence=42)])
        weak_single = ocr_data([word("日", confidence=70)])
        self.assertEqual(ht.filter_tesseract_data(low_line), "")
        self.assertEqual(ht.filter_tesseract_data(weak_single), "")

    def test_repetitive_hallucination_is_rejected(self):
        data = ocr_data([word("ニニニニニニ", confidence=99)])
        self.assertEqual(ht.filter_tesseract_data(data), "")

    def test_latin_ui_bleed_is_removed(self):
        data = ocr_data([word("Menu日本語123", confidence=94)])
        self.assertEqual(ht.filter_tesseract_data(data), "日本語")

    def test_malformed_numeric_row_is_skipped_safely(self):
        data = ocr_data([word("日本語", confidence="not-a-number")])
        self.assertEqual(ht.filter_tesseract_data(data), "")

    def test_missing_ocr_columns_raise_clear_error(self):
        with self.assertRaisesRegex(ValueError, "missing fields"):
            ht.filter_tesseract_data({"text": ["日本語"]})

    def test_windows_backend_uses_same_noise_rules(self):
        self.assertEqual(
            ht.filter_windows_ocr_lines(["Menu 日本語 123", "ニニニニニニ"]),
            "日本語",
        )


class WorkerResilienceTests(unittest.TestCase):
    def test_hover_cycle_exception_does_not_escape_worker(self):
        translator = ht.HoverTranslator.__new__(ht.HoverTranslator)
        translator.running = True
        translator.enabled = True
        translator.ui_queue = queue.Queue()
        translator._last_runtime_error_notice = 0.0
        translator._last_trigger_pos = None
        translator._cooldown_broken_by_distance = False

        calls = {"count": 0}

        def fail_once(_x, _y):
            calls["count"] += 1
            translator.running = False
            raise RuntimeError("synthetic capture failure")

        translator.handle_dwell = fail_once
        with mock.patch.object(ht, "POLL_INTERVAL_SECONDS", 0), mock.patch.object(
            ht, "DWELL_TIME_SECONDS", 0
        ), mock.patch.object(ht, "get_cursor_pos", return_value=(100, 100)), mock.patch.object(
            ht.time, "sleep", return_value=None
        ):
            translator.dwell_watch_loop()

        self.assertEqual(calls["count"], 1)
        event = translator.ui_queue.get_nowait()
        self.assertEqual(event[0], "runtime_error")


class CooldownDistanceResetTests(unittest.TestCase):
    """A deliberate re-hover of the same spot, after moving far enough away
    to have auto-hidden that popup, must not be swallowed by the cooldown
    meant only for tiny jitter. See HoverTranslator._cooldown_broken_by_distance."""

    def _translator(self):
        translator = ht.HoverTranslator.__new__(ht.HoverTranslator)
        translator._last_trigger_pos = (500, 500)
        translator._last_trigger_time = ht.time.monotonic()
        translator._cooldown_broken_by_distance = False
        return translator

    def test_in_cooldown_true_for_a_nearby_recent_trigger(self):
        translator = self._translator()
        self.assertTrue(translator.in_cooldown(510, 505))  # well within COOLDOWN_RADIUS_PX

    def test_in_cooldown_false_once_broken_by_distance(self):
        translator = self._translator()
        translator._cooldown_broken_by_distance = True
        # Same nearby point that test_in_cooldown_true_for_a_nearby_recent_trigger
        # proves would otherwise be in cooldown.
        self.assertFalse(translator.in_cooldown(510, 505))

    def test_dwell_watch_loop_breaks_cooldown_after_moving_past_hide_distance(self):
        translator = self._translator()
        translator.running = True
        translator.enabled = True

        far_point = (
            translator._last_trigger_pos[0] + ht.HIDE_MOVE_DISTANCE_PX + 1,
            translator._last_trigger_pos[1],
        )

        def get_pos_once():
            # Stop the loop after this one poll -- dwell_watch_loop is an
            # infinite loop until self.running goes False.
            translator.running = False
            return far_point

        with mock.patch.object(ht, "POLL_INTERVAL_SECONDS", 0), mock.patch.object(
            ht, "get_cursor_pos", side_effect=get_pos_once
        ), mock.patch.object(ht.time, "sleep", return_value=None):
            translator.dwell_watch_loop()

        self.assertTrue(translator._cooldown_broken_by_distance)
        # And in_cooldown must now be False for a spot near the original
        # trigger, exactly the scenario this fix targets.
        self.assertFalse(translator.in_cooldown(505, 500))

    def test_handle_dwell_resets_the_flag_on_a_fresh_trigger(self):
        translator = self._translator()
        translator._cooldown_broken_by_distance = True
        translator._job_counter = 0
        translator.ui_queue = queue.Queue()
        translator._tagger = mock.MagicMock(return_value=[])
        translator._kb_controller = mock.MagicMock()

        with mock.patch.object(
            ht, "get_selected_text", return_value=None
        ), mock.patch.object(
            translator, "capture_region", return_value=object()
        ), mock.patch.object(
            translator, "ocr", return_value=""
        ):
            translator.handle_dwell(700, 700)

        self.assertFalse(translator._cooldown_broken_by_distance)


class TranslationBackendDisplayTests(unittest.TestCase):
    """translation_backend_display must reflect which engines are actually
    active, not a fixed string set before startup finished -- see
    HoverTranslator._dictionary_active / _phrase_translator_active."""

    def _translator(self, dictionary_active, phrase_translator_active):
        translator = ht.HoverTranslator.__new__(ht.HoverTranslator)
        translator._dictionary_active = dictionary_active
        translator._phrase_translator_active = phrase_translator_active
        return translator

    def test_all_backends_active(self):
        translator = self._translator(True, True)
        self.assertEqual(
            translator.translation_backend_display,
            "JMdict words · Google phrases · offline fallback",
        )

    def test_jmdict_unavailable_is_reflected(self):
        translator = self._translator(False, True)
        self.assertIn("JMdict unavailable", translator.translation_backend_display)
        self.assertIn("Google phrases", translator.translation_backend_display)

    def test_google_unavailable_is_reflected(self):
        translator = self._translator(True, False)
        self.assertIn("JMdict words", translator.translation_backend_display)
        self.assertIn("Google unavailable", translator.translation_backend_display)


if __name__ == "__main__":
    unittest.main()
