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


if __name__ == "__main__":
    unittest.main()
