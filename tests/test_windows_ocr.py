"""Coverage for the Windows OCR backend (_ocr_windows/_ocr_windows_async).

This was previously completely untested -- no test mocked winrt, so the
async-to-sync bridging (asyncio.run per call) and the cursor-anchoring filter
on the WinRT recognition result had no coverage at all. winrt is installed in
this dev environment, so the real winrt.windows.* modules are patched in
place rather than faking sys.modules entries.
"""

import asyncio
import unittest
from unittest import mock

import hover_translate as ht


def make_shot(width=420, height=62):
    shot = mock.Mock()
    shot.width = width
    shot.height = height
    shot.bgra = b"\x00" * (width * height * 4)
    return shot


def bare_translator():
    return ht.HoverTranslator.__new__(ht.HoverTranslator)


def make_line(text, x, y, width, height):
    word = mock.Mock()
    word.bounding_rect = mock.Mock(x=x, y=y, width=width, height=height)
    line = mock.Mock()
    line.text = text
    line.words = [word]
    return line


class WindowsOcrAsyncTests(unittest.TestCase):
    def _patch_engine(self, lines):
        recognize_result = mock.Mock()
        recognize_result.lines = lines
        engine = mock.Mock()
        engine.recognize_async = mock.AsyncMock(return_value=recognize_result)

        writer = mock.Mock()
        writer.detach_buffer.return_value = mock.Mock()

        patches = [
            mock.patch("winrt.windows.globalization.Language"),
            mock.patch("winrt.windows.media.ocr.OcrEngine"),
            mock.patch("winrt.windows.graphics.imaging.SoftwareBitmap"),
            mock.patch("winrt.windows.storage.streams.DataWriter", return_value=writer),
        ]
        started = [p.start() for p in patches]
        self.addCleanup(lambda: [p.stop() for p in patches])
        _language_cls, ocr_engine_cls, software_bitmap_cls, _data_writer_cls = started
        ocr_engine_cls.is_language_supported.return_value = True
        ocr_engine_cls.try_create_from_language.return_value = engine
        software_bitmap_cls.create_copy_from_buffer.return_value = mock.Mock()
        return engine

    def test_keeps_only_the_line_anchored_near_the_cursor(self):
        translator = bare_translator()
        # Cursor point at scale=1 is (CAPTURE_WIDTH_PX/2, CAPTURE_OFFSET_Y_PX) = (210, 16).
        near = make_line("日本語", x=190, y=0, width=40, height=30)
        far = make_line("遠い", x=0, y=0, width=10, height=10)
        engine = self._patch_engine([near, far])

        lines = asyncio.run(translator._ocr_windows_async(make_shot()))

        self.assertEqual(lines, ["日本語"])
        engine.recognize_async.assert_awaited_once()

    def test_returns_no_lines_when_nothing_is_near_the_cursor(self):
        translator = bare_translator()
        far = make_line("遠い", x=0, y=0, width=10, height=10)
        self._patch_engine([far])

        lines = asyncio.run(translator._ocr_windows_async(make_shot()))

        self.assertEqual(lines, [])

    def test_raises_setup_error_when_japanese_language_unsupported(self):
        translator = bare_translator()
        with mock.patch("winrt.windows.globalization.Language"), \
                mock.patch("winrt.windows.media.ocr.OcrEngine") as ocr_engine_cls:
            ocr_engine_cls.is_language_supported.return_value = False
            with self.assertRaises(ht.OcrSetupError):
                asyncio.run(translator._ocr_windows_async(make_shot()))

    def test_raises_setup_error_when_engine_creation_fails(self):
        translator = bare_translator()
        with mock.patch("winrt.windows.globalization.Language"), \
                mock.patch("winrt.windows.media.ocr.OcrEngine") as ocr_engine_cls:
            ocr_engine_cls.is_language_supported.return_value = True
            ocr_engine_cls.try_create_from_language.return_value = None
            with self.assertRaises(ht.OcrSetupError):
                asyncio.run(translator._ocr_windows_async(make_shot()))


class WindowsOcrSyncWrapperTests(unittest.TestCase):
    def test_ocr_windows_applies_the_shared_japanese_noise_filter(self):
        """_ocr_windows must run filter_windows_ocr_lines on the anchored
        lines -- a line with no Japanese characters at all must be dropped
        even though it was correctly anchored near the cursor."""
        translator = bare_translator()
        latin_only = make_line("OK", x=190, y=0, width=40, height=30)

        recognize_result = mock.Mock()
        recognize_result.lines = [latin_only]
        engine = mock.Mock()
        engine.recognize_async = mock.AsyncMock(return_value=recognize_result)
        writer = mock.Mock()
        writer.detach_buffer.return_value = mock.Mock()

        with mock.patch("winrt.windows.globalization.Language"), \
                mock.patch("winrt.windows.media.ocr.OcrEngine") as ocr_engine_cls, \
                mock.patch("winrt.windows.graphics.imaging.SoftwareBitmap") as software_bitmap_cls, \
                mock.patch("winrt.windows.storage.streams.DataWriter", return_value=writer):
            ocr_engine_cls.is_language_supported.return_value = True
            ocr_engine_cls.try_create_from_language.return_value = engine
            software_bitmap_cls.create_copy_from_buffer.return_value = mock.Mock()

            result = translator._ocr_windows(make_shot())

        self.assertEqual(result, "")

    def test_ocr_windows_uses_a_fresh_event_loop_per_call(self):
        """handle_dwell calls this synchronously and repeatedly on the dwell
        worker thread -- asyncio.run() must not leak or reuse a closed loop
        across calls."""
        translator = bare_translator()
        near = make_line("日本語", x=190, y=0, width=40, height=30)

        recognize_result = mock.Mock()
        recognize_result.lines = [near]
        engine = mock.Mock()
        engine.recognize_async = mock.AsyncMock(return_value=recognize_result)
        writer = mock.Mock()
        writer.detach_buffer.return_value = mock.Mock()

        with mock.patch("winrt.windows.globalization.Language"), \
                mock.patch("winrt.windows.media.ocr.OcrEngine") as ocr_engine_cls, \
                mock.patch("winrt.windows.graphics.imaging.SoftwareBitmap") as software_bitmap_cls, \
                mock.patch("winrt.windows.storage.streams.DataWriter", return_value=writer):
            ocr_engine_cls.is_language_supported.return_value = True
            ocr_engine_cls.try_create_from_language.return_value = engine
            software_bitmap_cls.create_copy_from_buffer.return_value = mock.Mock()

            first = translator._ocr_windows(make_shot())
            second = translator._ocr_windows(make_shot())

        self.assertEqual(first, "日本語")
        self.assertEqual(second, "日本語")
        self.assertEqual(engine.recognize_async.await_count, 2)


if __name__ == "__main__":
    unittest.main()
