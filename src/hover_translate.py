"""
Real-time Japanese hover-translation overlay for Windows.

Hover the mouse over Japanese text on screen -> after a dwell period, a small
region around the cursor is captured, OCR'd, translated to English, and shown
in a transparent always-on-top popup near the cursor.

Pipeline: dwell detect -> selected-text check (clipboard) or screen capture
(mss) -> OCR (Tesseract when installed, Windows OCR fallback) if no selection ->
local JMdict lookup for words, or cached Google phrase translation with bundled
OPUS-MT/CTranslate2 fallback -> tkinter overlay popup.

If text is selected/highlighted in the window under the cursor, that text is
used directly instead of OCR (see get_selected_text) -- more accurate since
it skips OCR entirely, and takes priority whenever a selection is present.

Word segmentation, readings, and dictionary (lemma) forms come from fugashi
(MeCab/UniDic) -- see HoverTranslator.analyze_words. Conjugated verbs and
adjectives are shown with their dictionary form (the form you'd look up in
a dictionary) alongside the inflected surface form actually on screen.

This module is also the engine behind dashboard_app.py -- it exposes the
HoverTranslator worker, the OverlayWindow popup, the study-database helpers,
and JSON-backed hotkey config (load_config/save_config). The dashboard is the
primary way to run the tool; running this file directly is a headless
fallback that shows only the overlay (no dashboard window).

Run (primary):
    python dashboard_app.py   # light-mode dashboard; hover runs while it's open

Run (headless overlay only, legacy):
    python hover_translate.py

Hotkeys are configurable (see config.json / the dashboard Settings page).
Defaults:
    F9  toggles the tool on/off without closing it.
    F10 pins/unpins the current popup so it stops auto-hiding.
    F11 saves the currently-pinned popup's word/phrase to study_words.db
        (only while pinned, to avoid saving every fleeting hover).

OCR backends:
  - Tesseract is preferred when its executable and Japanese language data are
    installed. It was more accurate than Windows OCR on this app's small,
    screen-rendered Japanese text in side-by-side testing.
  - Windows' built-in OCR is used automatically when Tesseract is unavailable.
    It requires the Japanese Windows language/OCR feature.

Known limitations (found via manual + automated hover testing):
  - Dense adjacent Japanese text: when a line of Japanese sits very close to
    another line of Japanese (tight paragraph spacing in native dialogs/
    tooltips, or a Japanese label directly above/touching the target text),
    either OCR engine can drop or garble characters of the hovered line
    (observed: "こんにちは" -> "にちは"). Dense multi-line text is the
    least reliable case, but isolated text can still be misread too.
    Sweeping CAPTURE_OFFSET_Y_PX did not fix the dense-text failures, so this
    is treated as an OCR weak spot rather than something capture geometry can
    route around.
  - Wrapped-line translation can merge unrelated text: OCR lines within one
    capture are glued into a single string before translation (see
    handle_dwell) so a sentence that visually wraps reads as one sentence.
    If the second captured line is actually unrelated background text
    rather than a real wrap continuation, it gets glued on and can corrupt
    that translation too.
  - Selection priority is keyboard-focus-based, not cursor-position-based:
    reading a selection works by simulating Ctrl+C and diffing the
    clipboard (see get_selected_text) -- there is no universal Windows API
    for "what text is selected under this point." Ctrl+C goes to whichever
    window has keyboard focus, which is normally the window you selected
    text in (and are now hovering over), but if focus is on a *different*
    window than the one under your cursor, this can attempt to copy from
    the wrong place. Known console/terminal windows are explicitly skipped
    (see CONSOLE_WINDOW_CLASSES) because Ctrl+C there sends SIGINT instead
    of copying and could kill a running process -- other non-terminal apps
    where Ctrl+C has a non-copy meaning are not guarded against.
"""

import asyncio
import ctypes
import ctypes.wintypes
from datetime import timedelta
import json
import os
import queue
import re
import shutil
import sqlite3
import sys
import threading
import time
from pathlib import Path

import fugashi
import mss
import pyperclip
import pytesseract
from pynput import keyboard

from app_logging import configure_logging, install_exception_logging
from dictionary_lookup import LocalJapaneseDictionary
# Must stay a module-level (eager) import, and must stay above any winrt
# import in this file. offline_translation pulls in ctranslate2, which
# carries a newer MSVCP140 than the one winrt-runtime bundles privately; in a
# packaged build, whichever loads first wins the DLL search and the older
# winrt copy crashes the process (0xc0000005) before Python logging starts.
# Every winrt import below is deliberately lazy (inside a function, only
# reached once OCR actually runs) so this one always loads first. Guarded by
# tests/test_packaging_regression.py -- see that file for the full story.
from offline_translation import OfflineJapaneseTranslator, TranslationSetupError
from phrase_translation import GooglePhraseTranslator
from spaced_repetition import format_db_datetime, utc_now


def _enable_windows_dpi_awareness():
    """Keep cursor coordinates and MSS capture pixels in the same coordinate space.

    Without this on a scaled display (for example 125%), GetCursorPos returns
    logical coordinates while MSS captures physical pixels. OCR then examines a
    different point from the real pointer, which can surface unrelated nearby words
    even though the word-box cursor filter itself is working correctly.
    """
    if sys.platform != "win32":
        return
    try:
        # Windows 10+: best behavior when moving between monitors with different
        # scaling. This must run before Tk creates its first window.
        if ctypes.windll.user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return
    except (AttributeError, OSError):
        pass
    try:
        # Windows 8.1 fallback (PROCESS_PER_MONITOR_DPI_AWARE).
        if ctypes.windll.shcore.SetProcessDpiAwareness(2) == 0:
            return
    except (AttributeError, OSError):
        pass
    try:
        # Windows Vista/7 fallback.
        ctypes.windll.user32.SetProcessDPIAware()
    except (AttributeError, OSError):
        pass


_enable_windows_dpi_awareness()

# --------------------------------------------------------------------------
# Tunable constants -- adjust these to trade off responsiveness vs. load.
# --------------------------------------------------------------------------

# How long (seconds) the cursor must stay within DWELL_MOVE_TOLERANCE_PX
# before a capture/OCR/translate cycle is triggered.
DWELL_TIME_SECONDS = 0.3

# Cursor movement (pixels) within DWELL_TIME_SECONDS still counts as "dwelling".
DWELL_MOVE_TOLERANCE_PX = 4

# How often (seconds) the background worker polls the current mouse position.
POLL_INTERVAL_SECONDS = 0.05

# Size of the screen region captured around the cursor, in pixels. Width is
# generous because the box is centered on the cursor but a hovered sentence
# can extend well past either side of it -- too narrow truncates the text
# instead of just picking up noise. Height covers ~2 lines so a sentence that
# word-wraps just below the hovered line isn't truncated either.
CAPTURE_WIDTH_PX = 420
CAPTURE_HEIGHT_PX = 62

# Vertical offset of the capture region relative to the cursor (cursor sits
# at the horizontal center, this many pixels below the top of the region).
CAPTURE_OFFSET_Y_PX = 16

# --- Selected-text priority --------------------------------------------------
# If text is selected/highlighted in the focused window, use it directly
# instead of OCR (skips OCR entirely -- exact text, no misreads). Implemented
# by simulating Ctrl+C and diffing the clipboard; see get_selected_text and
# the "Known limitations" note at the top of this file for the constraints
# that come with that approach.

SELECTION_PRIORITY_ENABLED = True

# How long (seconds) to wait after sending Ctrl+C for the clipboard to update.
SELECTION_COPY_WAIT_SECONDS = 0.08

# The Windows clipboard is a shared OS resource -- any app briefly holding it
# (including the one we just told to copy) can make pyperclip.paste()/copy()
# fail with nothing wrong long-term. A couple of quick retries clears most of
# these before they cost us the ability to restore the user's real clipboard.
CLIPBOARD_RETRY_ATTEMPTS = 3
CLIPBOARD_RETRY_DELAY_SECONDS = 0.02

# Window classes where Ctrl+C means "send SIGINT", not "copy" -- never send
# the simulated copy keystroke while one of these is focused. Extend this
# list if you hit another terminal emulator that isn't covered.
CONSOLE_WINDOW_CLASSES = {
    "ConsoleWindowClass",  # classic conhost (cmd.exe, legacy consoles)
    "CASCADIA_HOSTING_WINDOW_CLASS",  # Windows Terminal
}

# Screenshots are ~96dpi, well below what Tesseract is tuned for (~300dpi).
# Upscaling the captured image before OCR noticeably improves kanji accuracy.
OCR_UPSCALE_FACTOR = 4

# Tesseract will "hallucinate" text out of background noise/antialiasing with
# no real signal behind it -- per-word confidence (0-100) lets us drop that
# instead of feeding it to translation. Real recognized Japanese text tends
# to score well above this even when imperfect; noise scores low or -1.
OCR_MIN_CONFIDENCE = 40

# Only accept OCR whose recognized word box is actually under/very near the
# capture cursor. Previously, any Japanese anywhere in the 420px-wide capture
# could trigger a popup while the pointer itself was resting on empty space.
OCR_CURSOR_HORIZONTAL_MARGIN_PX = 18
OCR_CURSOR_VERTICAL_MARGIN_PX = 18
OCR_MIN_LINE_AVERAGE_CONFIDENCE = 55
OCR_SINGLE_CHARACTER_MIN_CONFIDENCE = 78

# Some hallucinated noise still scores confidently (e.g. a repeated simple
# katakana glyph like ニーニーニー...) since the glyph itself is easy to
# match even though it doesn't spell anything real. A line with very few
# distinct characters relative to its length is almost always this kind of
# garbage rather than real text, so it's dropped before translation.
OCR_GARBAGE_MIN_LEN = 5
OCR_MIN_UNIQUE_CHAR_RATIO = 0.45

# Don't re-run OCR/translate for a spot the cursor already triggered on
# within this many pixels, for this many seconds (avoids re-triggering on
# every tiny jitter while reading the same text). Radius/duration are sized
# around observed round-trip time (OCR ~0.5-0.7s + translate ~1-4.5s, mostly
# network-bound) so a spot doesn't re-fire again before its own popup has
# even had time to be read.
COOLDOWN_RADIUS_PX = 55
COOLDOWN_SECONDS = 4.0

# If a JMdict lookup fails mid-session (e.g. a transient AV file-lock on the
# SQLite database), the dictionary is closed and word lookups fall back to
# phrase translation. Retrying on every single hover would hammer a lock
# that's still held; waiting this long between retry attempts gives a
# transient lock time to clear while still recovering automatically instead
# of staying degraded for the rest of the process's life.
DICTIONARY_RETRY_COOLDOWN_SECONDS = 30.0

# How far (pixels) the cursor must move from the popup's trigger point before
# the overlay is hidden.
HIDE_MOVE_DISTANCE_PX = 120

# Hide the overlay automatically after this many seconds even if the mouse
# hasn't moved away. Longer than the cooldown so a slow translate response
# doesn't get cut off right after it finally appears.
OVERLAY_AUTO_HIDE_SECONDS = 8.0

# Offset of the overlay window relative to the cursor.
OVERLAY_OFFSET_X_PX = 16
OVERLAY_OFFSET_Y_PX = 16

# --- Overlay appearance -----------------------------------------------------

OVERLAY_BG_COLOR = "#14141f"
OVERLAY_ACCENT_COLOR = "#7dd3fc"
OVERLAY_MAIN_TEXT_COLOR = "#f5f5f5"
OVERLAY_FURIGANA_COLOR = "#7dd3fc"
OVERLAY_TRANSLATION_COLOR = "#ffe066"
OVERLAY_ALPHA = 0.97
OVERLAY_MAX_WIDTH_PX = 360
OVERLAY_PADDING_PX = 14

OVERLAY_MAIN_FONT = ("Yu Gothic UI", 17)
OVERLAY_FURIGANA_FONT = ("Yu Gothic UI", 10)
OVERLAY_TRANSLATION_FONT = ("Segoe UI", 13, "italic")
OVERLAY_DICTIONARY_FONT = ("Segoe UI", 11)
OVERLAY_DICT_FORM_FONT = ("Segoe UI", 10)
OVERLAY_DICT_FORM_COLOR = "#a7f3d0"
OVERLAY_PIN_COLOR = "#fb923c"

# A very long selection (e.g. accidentally selecting a whole paragraph)
# still gets wrapped correctly now, but processing/translating hundreds of
# characters is slow and not very useful for a word/phrase lookup tool --
# truncate before analysis and show "..." so it's obvious it was cut.
MAX_TEXT_LENGTH = 120

KANJI_RE = re.compile(r"[一-鿿々]")
JAPANESE_CHAR_RE = re.compile(r"[぀-ヿ一-鿿々]")
JAPANESE_LETTER_RE = re.compile(r"[ぁ-んァ-ヶ一-鿿々]")
# Anything NOT hiragana/katakana/kanji/common Japanese punctuation gets
# stripped from OCR output -- drops stray Latin/UI text bleeding into the
# capture region instead of feeding it into translation as noise.
NON_JAPANESE_RE = re.compile(r"[^぀-ヿ一-鿿々、。・「」『』]")

# Placeholder shown while the local translation worker is in flight. The popup
# appears right after OCR instead of waiting, then updates in place.
TRANSLATING_PLACEHOLDER = "Translating offline…"
TRANSLATION_MODEL_LOAD_TIMEOUT_SECONDS = 30

# Set JAPANESE_HOVER_OCR_BACKEND to "tesseract" or "windows" to force a
# backend for troubleshooting. The default, "auto", prefers Tesseract when it
# and its Japanese language data are installed, then falls back to Windows OCR.
OCR_BACKEND_ENV = "JAPANESE_HOVER_OCR_BACKEND"

# --- Hotkeys (user-configurable; persisted in config.json) ------------------
# Three actions, each bound to a single key:
#   toggle -- turn hover translation on/off without closing the app.
#   pin    -- pin the current popup so it stops auto-hiding (by timer or the
#             cursor moving away), so you can sit and study it. Hovering a new
#             word while pinned still updates the popup.
#   save   -- save the currently-displayed word/phrase to the study database.
#             Only takes effect while pinned, so a deliberate "add to my study
#             list" action is required rather than saving every fleeting hover.
# Function keys (F1-F12) are recommended -- a normal typing key would fire the
# action every time you type it. Defaults below are used until config.json
# overrides them; see load_config/save_config and key_to_str/str_to_key.
DEFAULT_HOTKEYS = {"toggle": "f9", "pin": "f10", "save": "f11"}


def app_data_directory():
    """Return a user-writable data directory for source and packaged runs."""
    if getattr(sys, "frozen", False):
        base = os.environ.get("LOCALAPPDATA") or os.path.expanduser("~")
        path = os.path.join(base, "JapaneseHoverTranslator")
    else:
        # This file lives in src/, one level below the project root -- go up
        # twice so source runs keep storing data at the project root (beside
        # README/requirements.txt), not inside src/ itself.
        path = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    os.makedirs(path, exist_ok=True)
    return path


APP_DATA_DIRECTORY = app_data_directory()
CONFIG_PATH = os.path.join(APP_DATA_DIRECTORY, "config.json")
STUDY_DB_PATH = os.path.join(APP_DATA_DIRECTORY, "study_words.db")
TRANSLATION_CACHE_PATH = os.path.join(APP_DATA_DIRECTORY, "translation_cache.db")
log, LOG_PATH = configure_logging(APP_DATA_DIRECTORY)
install_exception_logging(log)

# --------------------------------------------------------------------------


class OcrSetupError(RuntimeError):
    """Raised when neither Japanese OCR backend is usable."""


def _find_tesseract_command():
    """Return a portable Tesseract executable path, or None."""
    candidates = [os.environ.get("TESSERACT_CMD"), shutil.which("tesseract")]
    for env_name in ("ProgramFiles", "ProgramFiles(x86)", "LOCALAPPDATA"):
        base = os.environ.get(env_name)
        if not base:
            continue
        if env_name == "LOCALAPPDATA":
            candidates.append(os.path.join(base, "Programs", "Tesseract-OCR", "tesseract.exe"))
        else:
            candidates.append(os.path.join(base, "Tesseract-OCR", "tesseract.exe"))
    for candidate in candidates:
        if candidate and os.path.isfile(candidate):
            return os.path.abspath(candidate)
    return None


def _find_tessdata_prefix(tesseract_command):
    """Find Japanese trained data without embedding a machine-specific path."""
    configured = os.environ.get("TESSDATA_PREFIX")
    candidates = [
        configured,
        os.path.join(os.path.dirname(tesseract_command), "tessdata"),
        # This file lives in src/, one level below the project root -- go up
        # twice so a "tessdata" folder at the project root (as documented in
        # the README) is still found.
        os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "tessdata"),
        str(Path.home() / ".tessdata"),
    ]
    for candidate in candidates:
        if candidate and os.path.isfile(os.path.join(candidate, "jpn.traineddata")):
            return os.path.abspath(candidate)
    return None


def _configure_tesseract():
    """Try to make Tesseract usable: locate the executable and its jpn
    tessdata, point pytesseract at them, and probe that "jpn" is actually
    among the languages it reports. Returns True/False rather than raising --
    callers (choose_ocr_backend) decide what an unavailable backend means."""
    command = _find_tesseract_command()
    if command is None:
        return False
    tessdata_prefix = _find_tessdata_prefix(command)
    if tessdata_prefix is None:
        return False
    pytesseract.pytesseract.tesseract_cmd = command
    os.environ["TESSDATA_PREFIX"] = tessdata_prefix
    try:
        return "jpn" in pytesseract.get_languages(config="")
    except Exception as exc:
        log.warning("Tesseract probe failed: %s", exc)
        return False


def _windows_ocr_available():
    """Probe whether Windows' built-in OCR has the Japanese language pack
    installed (Settings > Time & language > Language & region), without
    actually running any recognition. Returns False (rather than raising) on
    any failure -- including winrt itself being missing/broken -- since this
    is purely a capability check used to pick a backend, not a hard
    requirement."""
    # Keep this import lazy -- see the comment above the offline_translation
    # import near the top of this file for why (MSVCP140 DLL load order).
    try:
        from winrt.windows.media.ocr import OcrEngine

        return any(
            language.language_tag.lower() == "ja"
            or language.language_tag.lower().startswith("ja-")
            for language in OcrEngine.available_recognizer_languages
        )
    except Exception as exc:
        log.warning("Windows OCR probe failed: %s", exc)
        return False


def choose_ocr_backend():
    """Pick "tesseract" or "windows" for HoverTranslator.ocr_backend.

    Honors JAPANESE_HOVER_OCR_BACKEND if set (raising OcrSetupError if the
    forced choice isn't actually usable); otherwise prefers Tesseract when
    available since side-by-side testing on this app's small screen captures
    found it more accurate, falling back to Windows OCR, and raising
    OcrSetupError only if neither backend works at all -- this is what
    surfaces as the startup error dialog in dashboard_app.py's __main__
    guard when a fresh machine has neither set up.
    """
    requested = os.environ.get(OCR_BACKEND_ENV, "auto").strip().lower()
    if requested not in {"auto", "tesseract", "windows"}:
        raise OcrSetupError(
            f"{OCR_BACKEND_ENV} must be 'auto', 'tesseract', or 'windows'."
        )

    tesseract_available = _configure_tesseract()
    windows_available = _windows_ocr_available()
    if requested == "tesseract" and not tesseract_available:
        raise OcrSetupError(
            "Tesseract was requested but its executable or jpn.traineddata was not found."
        )
    if requested == "windows" and not windows_available:
        raise OcrSetupError(
            "Windows OCR was requested, but the Japanese OCR language is not installed."
        )
    if requested != "auto":
        return requested
    if tesseract_available:
        return "tesseract"
    if windows_available:
        return "windows"
    raise OcrSetupError(
        "No Japanese OCR backend is available. Install Tesseract with jpn.traineddata, "
        "or add Japanese in Windows Settings > Time & language > Language & region."
    )

user32 = ctypes.windll.user32


def get_cursor_pos():
    """Physical-pixel cursor position via the Win32 API, in the same
    virtual-desktop coordinate space as get_virtual_screen_bounds() and mss
    screen captures. Requires per-monitor DPI awareness to already be set
    (see _enable_windows_dpi_awareness above) or this and mss disagree on
    scaled displays."""
    pt = ctypes.wintypes.POINT()
    user32.GetCursorPos(ctypes.byref(pt))
    return pt.x, pt.y


SM_XVIRTUALSCREEN = 76
SM_YVIRTUALSCREEN = 77
SM_CXVIRTUALSCREEN = 78
SM_CYVIRTUALSCREEN = 79


def get_virtual_screen_bounds():
    """Bounding box of the full virtual desktop across all monitors, as
    (left, top, width, height). A secondary monitor positioned left of or
    above the primary one has a negative left/top -- GetCursorPos and mss
    both use this same virtual-desktop coordinate space, so capture and
    overlay placement need to clamp against these bounds rather than
    assuming a (0, 0) origin (which is only true when there's one monitor
    or the primary is the top-left-most one)."""
    return (
        user32.GetSystemMetrics(SM_XVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_YVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CXVIRTUALSCREEN),
        user32.GetSystemMetrics(SM_CYVIRTUALSCREEN),
    )


GA_ROOT = 2
user32.WindowFromPoint.argtypes = [ctypes.wintypes.POINT]
user32.WindowFromPoint.restype = ctypes.wintypes.HWND
user32.GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.c_uint]
user32.GetAncestor.restype = ctypes.wintypes.HWND
user32.GetForegroundWindow.restype = ctypes.wintypes.HWND


def get_window_class_name(hwnd):
    """Win32 window class name for a window handle, used to recognize known
    terminal/console windows (see CONSOLE_WINDOW_CLASSES) where sending Ctrl+C
    would send SIGINT instead of copying."""
    buf = ctypes.create_unicode_buffer(256)
    user32.GetClassNameW(hwnd, buf, 256)
    return buf.value


def cursor_is_over_focused_window(x, y):
    """True if the top-level window under (x, y) is also the keyboard-focused
    one -- Ctrl+C goes to whichever window has focus, so the selection-copy
    trick is only meaningful (and only attempted) when that matches where
    the mouse actually is."""
    hwnd_at_point = user32.WindowFromPoint(ctypes.wintypes.POINT(x, y))
    if not hwnd_at_point:
        return False
    root_at_point = user32.GetAncestor(hwnd_at_point, GA_ROOT)
    foreground = user32.GetForegroundWindow()
    return bool(root_at_point) and root_at_point == foreground


def focused_window_is_console():
    """True if Ctrl+C would send SIGINT instead of copying (see the
    'Known limitations' note at the top of this file)."""
    foreground = user32.GetForegroundWindow()
    if not foreground:
        return False
    return get_window_class_name(foreground) in CONSOLE_WINDOW_CLASSES


def _clipboard_paste_with_retry():
    """pyperclip.paste(), retrying past transient Windows clipboard-lock
    failures. Returns None (distinct from a legitimately empty clipboard,
    which returns "") only once every attempt has failed."""
    for attempt in range(CLIPBOARD_RETRY_ATTEMPTS):
        try:
            return pyperclip.paste()
        except Exception:
            if attempt + 1 == CLIPBOARD_RETRY_ATTEMPTS:
                return None
            time.sleep(CLIPBOARD_RETRY_DELAY_SECONDS)
    return None


def _clipboard_copy_with_retry(text):
    """pyperclip.copy(), retrying past transient Windows clipboard-lock
    failures. Returns True on success, False if every attempt failed."""
    for attempt in range(CLIPBOARD_RETRY_ATTEMPTS):
        try:
            pyperclip.copy(text)
            return True
        except Exception:
            if attempt + 1 == CLIPBOARD_RETRY_ATTEMPTS:
                return False
            time.sleep(CLIPBOARD_RETRY_DELAY_SECONDS)
    return False


def get_selected_text(kb_controller, x, y):
    """Read the current text selection via the Ctrl+C/clipboard trick, or
    return None if there's no selection (or it isn't safe to try). The
    original clipboard contents are restored afterward whenever they could
    be read in the first place (see _clipboard_paste_with_retry)."""
    if not SELECTION_PRIORITY_ENABLED:
        return None
    if not cursor_is_over_focused_window(x, y):
        return None
    if focused_window_is_console():
        return None

    # Step 1: remember whatever is on the clipboard right now so it can be
    # restored below -- this trick temporarily overwrites it with Ctrl+C.
    original = _clipboard_paste_with_retry()

    # Step 2: simulate a physical Ctrl+C keypress into the focused window.
    # This is the only way to read "what's selected" -- Windows has no API
    # for it -- so whatever has focus receives this exactly as if the user
    # pressed it themselves.
    kb_controller.press(keyboard.Key.ctrl)
    kb_controller.press("c")
    kb_controller.release("c")
    kb_controller.release(keyboard.Key.ctrl)
    time.sleep(SELECTION_COPY_WAIT_SECONDS)  # give the target app time to update the clipboard

    # Step 3: read back whatever the Ctrl+C put on the clipboard (if anything).
    copied = _clipboard_paste_with_retry()

    # Step 4: put the user's original clipboard contents back so this lookup
    # is invisible to them -- best-effort, since the clipboard is a shared OS
    # resource another app could be holding right at this moment.
    if original is not None:
        if not _clipboard_copy_with_retry(original):
            log.warning(
                "could not restore the clipboard after reading a selection "
                "-- the copied Japanese text may be left on it"
            )
    else:
        log.warning(
            "could not read the clipboard before copying a selection -- "
            "its previous contents will not be restored"
        )

    # Step 5: decide whether the Ctrl+C actually captured a real selection.
    # No change from `original` means nothing was selected (Ctrl+C copies
    # nothing new in that case), and a copied string with no Japanese in it
    # isn't a selection this tool cares about.
    if not copied or copied == original:
        return None
    if not JAPANESE_CHAR_RE.search(copied):
        return None
    return copied.strip()


def is_repetitive_garbage(line):
    """True if `line` is long enough to judge and has too few distinct
    characters relative to its length -- catches OCR hallucinations like a
    repeated katakana glyph (e.g. "ニーニーニー...") that individually match
    confidently but don't spell anything real."""
    if len(line) < OCR_GARBAGE_MIN_LEN:
        return False
    return len(set(line)) / len(line) < OCR_MIN_UNIQUE_CHAR_RATIO


def ocr_box_near_capture_cursor(left, top, width, height, scale=1):
    """Whether an OCR word box is plausibly the text under the pointer."""
    cursor_x = CAPTURE_WIDTH_PX * scale / 2
    cursor_y = CAPTURE_OFFSET_Y_PX * scale
    margin_x = OCR_CURSOR_HORIZONTAL_MARGIN_PX * scale
    margin_y = OCR_CURSOR_VERTICAL_MARGIN_PX * scale
    return (
        left - margin_x <= cursor_x <= left + width + margin_x
        and top - margin_y <= cursor_y <= top + height + margin_y
    )


def clean_japanese_line(line):
    """Drop stray Latin/UI text and whitespace from an OCR line."""
    return NON_JAPANESE_RE.sub("", line or "")


def filter_tesseract_data(data):
    """Convert pytesseract word data into cursor-anchored, trusted lines.

    Kept as a pure function so the false-popup failure modes can be tested
    without a live screen capture or an installed Tesseract executable.
    """
    required = (
        "text", "conf", "block_num", "par_num", "line_num",
        "left", "top", "width", "height",
    )
    # pytesseract.image_to_data returns one flat dict of parallel lists (one
    # entry per recognized word) rather than a nested word/line structure --
    # fail loudly here if the shape ever changes instead of silently mis-zipping.
    missing = [name for name in required if name not in data]
    if missing:
        raise ValueError("OCR result is missing fields: " + ", ".join(missing))

    # Pass 1: group Tesseract's flat per-word rows back into lines, keyed by
    # Tesseract's own (block, paragraph, line) numbering, dropping any word
    # that's empty or below the per-word confidence floor as we go. Each kept
    # word also records whether its own bounding box is near the capture's
    # cursor point, since that's decided per-word, not per-line.
    line_words = {}
    for word, conf, block, par, line_num, left, top, width, height in zip(
        *(data[name] for name in required)
    ):
        word = str(word).strip()
        try:
            confidence = float(conf)
            left = float(left)
            top = float(top)
            width = float(width)
            height = float(height)
        except (TypeError, ValueError):
            continue
        if not word or confidence < OCR_MIN_CONFIDENCE:
            continue
        line_words.setdefault((block, par, line_num), []).append(
            {
                "text": word,
                "confidence": confidence,
                "near_cursor": ocr_box_near_capture_cursor(
                    left, top, width, height, OCR_UPSCALE_FACTOR
                ),
            }
        )

    # Pass 2: turn each surviving line's word list into one cleaned string,
    # keeping only lines that both plausibly sit under the cursor and look
    # like real Japanese text rather than OCR noise.
    lines = []
    for key in sorted(line_words):  # sorted so lines come out in reading order
        words = line_words[key]
        # A line only counts as "under the cursor" if at least one of its
        # words does -- this is what stops unrelated text elsewhere in the
        # capture region from producing a popup.
        if not any(word["near_cursor"] for word in words):
            continue
        line = clean_japanese_line("".join(word["text"] for word in words))
        average_confidence = sum(
            word["confidence"] for word in words
        ) / len(words)
        # Reject: empty after cleaning, no actual kana/kanji present, looks
        # like repeated-glyph hallucination, average confidence too low, or
        # (for a single lone character, which is easy to false-positive on)
        # confidence below the stricter single-character threshold.
        if (
            not line
            or not JAPANESE_LETTER_RE.search(line)
            or is_repetitive_garbage(line)
            or average_confidence < OCR_MIN_LINE_AVERAGE_CONFIDENCE
            or (
                len(line) == 1
                and average_confidence < OCR_SINGLE_CHARACTER_MIN_CONFIDENCE
            )
        ):
            continue
        lines.append(line)
    return "\n".join(lines)


def filter_windows_ocr_lines(raw_lines):
    """Apply the backend-independent Japanese/noise checks to Windows OCR."""
    lines = [clean_japanese_line(line) for line in raw_lines]
    return "\n".join(
        line
        for line in lines
        if line
        and JAPANESE_LETTER_RE.search(line)
        and not is_repetitive_garbage(line)
    )


# UniDic POS categories where the surface form on screen is commonly an
# inflected form of a word you'd actually look up in a different (dictionary)
# form -- verbs, i-adjectives, na-adjectives. Particles/nouns/etc. don't
# inflect this way, so they're left out of the dictionary-form breakdown.
INFLECTING_POS = {"動詞", "形容詞", "形容動詞"}
NON_LEXICAL_POS = {"助詞", "助動詞", "補助記号", "空白", "記号"}


def kata_to_hira(text):
    """Katakana -> hiragana. UniDic readings are katakana; this file's
    furigana convention (and beginners' first-learned script) is hiragana."""
    return "".join(
        chr(ord(ch) - 0x60) if "ァ" <= ch <= "ヶ" else ch for ch in text
    )


def truncate_for_analysis(text):
    """Cap text at MAX_TEXT_LENGTH before furigana/dictionary-form analysis
    and translation -- an accidentally-selected whole paragraph is slow to
    process and not useful for a word/phrase lookup tool, so it's cut short
    with a trailing ellipsis instead."""
    if len(text) <= MAX_TEXT_LENGTH:
        return text
    return text[:MAX_TEXT_LENGTH] + "…"


class TranslationJob:
    """Initial (pre-translation) payload passed from the worker thread to the UI
    thread, sent as soon as OCR finishes so the popup appears without waiting
    on the separate offline translation worker."""

    def __init__(self, job_id, x, y, furigana_lines, dict_forms, raw_text):
        """Store one hover's OCR/selection results for the popup to render
        immediately; job_id lets a later ("translation_ready", job_id, text)
        message find its way back to the right popup even if the cursor has
        since moved on to a different word."""
        self.job_id = job_id
        self.x = x
        self.y = y
        # furigana_lines: list of lines, each a list of (surface, reading_or_None)
        # token tuples -- reading is the kana to render as ruby text above
        # surface, or None if surface needs no furigana (kana/punctuation).
        self.furigana_lines = furigana_lines
        # dict_forms: list of (surface, reading, lemma, lemma_reading) for
        # inflected content words (see INFLECTING_POS), surface != lemma.
        self.dict_forms = dict_forms
        self.raw_text = raw_text  # for saving to the study database


class HoverTranslator:
    """The background engine: dwell detection, OCR/selection capture, and
    translation, run from two threads owned by this class.

    dwell_watch_loop (started by the owning app, e.g. DashboardApp, as a
    daemon thread) polls the cursor and, once it's held still long enough,
    calls handle_dwell -> capture_region/get_selected_text -> ocr -> queues a
    TranslationJob on ui_queue and a lookup job on _translation_queue.

    _translation_loop runs on its own daemon thread (started here in
    __init__) since loading the offline model can take a couple of seconds
    and inference shouldn't block dwell detection. It owns the JMdict
    connection, the Google phrase translator, and the offline model, and
    results come back via ui_queue as ("translation_ready", job_id, text).

    Neither thread ever touches Tkinter -- ui_queue is the only channel back
    to the UI, consumed on the Tk main thread (see DashboardApp._poll_queue).
    """

    def __init__(self, ui_queue: queue.Queue):
        """Pick an OCR backend, start the translation worker thread, and
        block (up to TRANSLATION_MODEL_LOAD_TIMEOUT_SECONDS) until it's
        confirmed ready. Raises OcrSetupError (from choose_ocr_backend) or
        TranslationSetupError if either can't be made to work -- the caller
        is expected to show that to the user, since a broken engine here
        means the app has no OCR or no translation, respectively."""
        # --- Basic run state ---
        self.ui_queue = ui_queue
        self.enabled = True  # toggled by the toggle hotkey; dwell loop no-ops while False
        self.running = True  # flipped False by stop(); dwell loop's outer while-loop condition

        # --- OCR backend selection (raises OcrSetupError if neither works) ---
        self.ocr_backend = choose_ocr_backend()
        self.ocr_backend_display = (
            "Tesseract" if self.ocr_backend == "tesseract" else "Windows OCR"
        )
        self.translation_backend_display = "JMdict words · Google phrases · offline fallback"

        # --- Shared resources used by the dwell worker thread ---
        self._sct = mss.MSS()  # screen-capture handle, reused across every hover
        self._tagger = fugashi.Tagger()  # MeCab/UniDic tokenizer for furigana + lemmas
        self._kb_controller = keyboard.Controller()  # simulates Ctrl+C for selection reads

        # --- Dwell cooldown tracking (see in_cooldown) ---
        self._last_trigger_pos = None  # (x, y) of the last successful trigger
        self._last_trigger_time = 0.0
        self._last_runtime_error_notice = 0.0  # throttles repeated "hover failed" UI notices

        # --- Translation job bookkeeping ---
        # job_counter/latest_translation_job_id let a translation result that
        # arrives late (after the cursor moved on) be recognized as stale and
        # discarded instead of popping up over whatever's showing now.
        self._job_counter = 0
        self._latest_translation_job_id = None
        self._translation_queue = queue.Queue(maxsize=1)

        # --- Start the translation worker thread and wait for it to finish
        # loading (JMdict + offline model + Google client) before returning,
        # so the caller never gets a HoverTranslator that isn't actually ready
        # to translate yet. ---
        self._translation_init_event = threading.Event()
        self._translation_init_error = None
        self._translation_thread = threading.Thread(
            target=self._translation_loop,
            name="offline-translation-worker",
            daemon=True,
        )
        self._translation_thread.start()
        if not self._translation_init_event.wait(TRANSLATION_MODEL_LOAD_TIMEOUT_SECONDS):
            self.running = False
            raise TranslationSetupError(
                "The bundled offline translation model took too long to load."
            )
        if self._translation_init_error is not None:
            self.running = False
            raise TranslationSetupError(str(self._translation_init_error))
        log.info("OCR backend: %s", self.ocr_backend_display)
        log.info("translation backend: %s", self.translation_backend_display)

    @staticmethod
    def clean_japanese_line(line):
        """Drop non-Japanese characters (stray Latin/UI text, spacing artifacts)."""
        return clean_japanese_line(line)

    def furigana_line(self, line):
        """Split a line into (surface, reading_or_None) tokens for ruby text."""
        tokens = []
        for word in self._tagger(line):
            surface = word.surface
            kana = word.feature.kana
            reading = kata_to_hira(kana) if kana and kana != "*" and KANJI_RE.search(surface) else None
            tokens.append((surface, reading))
        return tokens

    def dictionary_forms(self, text):
        """Inflected content words in text, as (surface, reading, lemma,
        lemma_reading) -- the surface form actually on screen next to the
        dictionary form you'd look it up under, for words where they differ."""
        seen = set()
        forms = []
        for word in self._tagger(text):
            feat = word.feature
            if feat.pos1 not in INFLECTING_POS:
                continue
            lemma = feat.lemma if feat.lemma and feat.lemma != "*" else word.surface
            if lemma == word.surface or lemma in seen:
                continue
            seen.add(lemma)
            reading = kata_to_hira(feat.kana) if feat.kana and feat.kana != "*" else ""
            lemma_reading = kata_to_hira(feat.lForm) if feat.lForm and feat.lForm != "*" else ""
            forms.append((word.surface, reading, lemma, lemma_reading))
        return forms

    def dictionary_candidates(self, text):
        """Exact/lemma forms worth trying as a single JMdict entry.

        The exact string is tried first so compounds such as 日本語 work even
        when UniDic splits them into multiple nouns. A lemma fallback is added
        only when there is one lexical token plus auxiliaries/particles, which
        covers inflected words without treating full sentences as one word.
        """
        candidate = text.strip().strip("「」『』（）()［］[]【】")
        if (
            not candidate
            or len(candidate) > 32
            or re.search(r"[\s。！？!?]", candidate)
        ):
            return []

        tokens = list(self._tagger(candidate))
        lexical = [
            word for word in tokens if getattr(word.feature, "pos1", "") not in NON_LEXICAL_POS
        ]
        if not lexical:
            return []

        candidates = [candidate]
        if len(lexical) == 1:
            word = lexical[0]
            lemma = word.feature.lemma
            if lemma and lemma != "*":
                candidates.append(lemma)
            candidates.append(word.surface)
        return list(dict.fromkeys(candidates))

    def capture_region(self, cx, cy):
        """Grab a CAPTURE_WIDTH_PX x CAPTURE_HEIGHT_PX screenshot centered
        (horizontally) around (cx, cy), clamped to the virtual desktop.
        Called from the dwell worker thread, never Tk's -- mss.grab() is
        just a screen-memory read, no UI interaction."""
        vleft, vtop, vwidth, vheight = get_virtual_screen_bounds()
        left = cx - CAPTURE_WIDTH_PX // 2
        top = cy - CAPTURE_OFFSET_Y_PX
        # Clamp so the capture box never extends past the virtual desktop's
        # edge -- an unclamped negative/overflowing region silently grabs
        # garbage (or nothing useful). Clamped against the full multi-monitor
        # virtual desktop, not just a (0, 0)-origin primary monitor, since a
        # secondary monitor left of/above the primary has negative coordinates.
        left = max(vleft, min(left, vleft + vwidth - CAPTURE_WIDTH_PX))
        top = max(vtop, min(top, vtop + vheight - CAPTURE_HEIGHT_PX))
        region = {
            "left": left,
            "top": top,
            "width": CAPTURE_WIDTH_PX,
            "height": CAPTURE_HEIGHT_PX,
        }
        shot = self._sct.grab(region)
        return shot

    def ocr(self, shot):
        """Recognize Japanese text in an mss screenshot using whichever
        backend choose_ocr_backend picked at startup. Returns newline-joined
        text (already filtered for the Japanese/noise/cursor-proximity
        checks each backend applies) or "" if nothing usable was found."""
        if self.ocr_backend == "windows":
            return self._ocr_windows(shot)
        return self._ocr_tesseract(shot)

    def _ocr_tesseract(self, shot):
        """Upscale + autocontrast the capture, run Tesseract with per-word
        confidence data, and filter to Japanese words near the cursor (see
        filter_tesseract_data / ocr_box_near_capture_cursor)."""
        # NOTE: on text packed tighter than a normal paragraph line (a dense
        # native dialog, a Japanese label directly touching the target line),
        # Tesseract can silently drop leading characters of the hovered line
        # even though the pixels are clearly legible to a human -- see the
        # "Known limitations" note at the top of this file. This is a model
        # weak spot, not something fixed by the preprocessing below.
        import PIL.Image
        import PIL.ImageOps

        img = PIL.Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        img = img.convert("L")
        img = img.resize(
            (img.width * OCR_UPSCALE_FACTOR, img.height * OCR_UPSCALE_FACTOR),
            PIL.Image.LANCZOS,
        )
        img = PIL.ImageOps.autocontrast(img, cutoff=1)
        # oem 3 (default, legacy+LSTM combined) tested more reliably than
        # oem 1 (LSTM-only) across sample text -- oem 1 occasionally missed
        # whole hiragana runs that oem 3 read correctly at high confidence.
        data = pytesseract.image_to_data(
            img,
            lang="jpn",
            config="--oem 3 --psm 6",
            output_type=pytesseract.Output.DICT,
        )

        return filter_tesseract_data(data)

    async def _ocr_windows_async(self, shot):
        """Run the capture through Windows' built-in OCR (Windows.Media.Ocr
        via the winrt projection) and return only the lines whose word boxes
        are anchored near the capture's cursor point -- unrelated nearby
        text (e.g. from a neighboring UI element) is filtered out here at
        the box level, before any text-level noise filtering runs."""
        # Imports stay lazy so a Tesseract-only installation can still run if
        # the optional WinRT projection is unavailable or damaged, and so
        # ctranslate2 (imported eagerly above, via offline_translation) always
        # loads its newer MSVCP140 before winrt's private older copy could.
        from winrt.windows.globalization import Language
        from winrt.windows.graphics.imaging import BitmapPixelFormat, SoftwareBitmap
        from winrt.windows.media.ocr import OcrEngine
        from winrt.windows.storage.streams import DataWriter

        language = Language("ja")
        if not OcrEngine.is_language_supported(language):
            raise OcrSetupError("The Japanese Windows OCR language is no longer available.")
        engine = OcrEngine.try_create_from_language(language)
        if engine is None:
            raise OcrSetupError("Windows could not create its Japanese OCR engine.")

        writer = DataWriter()
        writer.write_bytes(shot.bgra)
        bitmap = SoftwareBitmap.create_copy_from_buffer(
            writer.detach_buffer(),
            BitmapPixelFormat.BGRA8,
            shot.width,
            shot.height,
        )
        result = await engine.recognize_async(bitmap)
        anchored_lines = []
        for line in result.lines:
            boxes = [word.bounding_rect for word in line.words]
            if any(
                ocr_box_near_capture_cursor(box.x, box.y, box.width, box.height)
                for box in boxes
            ):
                anchored_lines.append(line.text)
        return anchored_lines

    def _ocr_windows(self, shot):
        """Sync wrapper around _ocr_windows_async, applying the same
        Japanese/noise filter Tesseract's path uses (filter_windows_ocr_lines)
        so both backends' output gets consistent treatment downstream."""
        # handle_dwell calls this synchronously on the dedicated dwell worker,
        # never on Tk's main thread. A fresh event loop here safely bridges the
        # WinRT async API without crossing Tkinter's thread boundary.
        raw_lines = asyncio.run(self._ocr_windows_async(shot))
        return filter_windows_ocr_lines(raw_lines)

    def _translation_loop(self):
        """The dedicated translation-worker thread's target function: loads
        JMdict/offline model/Google client once, then services jobs from
        _translation_queue one at a time until a None sentinel arrives (see
        stop()). Each job tries JMdict first for a single word/lemma, then
        Google (cached, with a backoff after failures), then the offline
        model as the final fallback -- see the module docstring's pipeline
        summary for the full picture. Every failure mode degrades rather
        than crashes the loop; see the try/except structure below."""
        dictionary = None
        dictionary_retry_after = 0.0
        offline_translator = None
        phrase_translator = None

        # === Phase 1: one-time startup of the three translation engines. ===
        # This runs once when the thread starts, before the job loop below.
        try:
            try:
                dictionary = LocalJapaneseDictionary()
            except Exception:
                # A broken dictionary should reduce word quality, not disable
                # all phrase translation or stop the application from opening.
                log.exception("JMdict initialization failed; continuing without it")

            try:
                offline_translator = OfflineJapaneseTranslator(TRANSLATION_CACHE_PATH)
            except Exception as exc:
                # The offline model is the guaranteed fallback. Without it,
                # startup should fail clearly instead of leaving an unreliable
                # network-only translator that silently stops working offline.
                log.exception("offline translation initialization failed")
                self._translation_init_error = exc
                return

            try:
                phrase_translator = GooglePhraseTranslator(TRANSLATION_CACHE_PATH)
            except Exception:
                # Cache/client setup can fail independently; the bundled model
                # remains a complete phrase translator.
                log.exception(
                    "Google phrase translator initialization failed; using offline only"
                )
        finally:
            # Unblocks HoverTranslator.__init__'s wait() no matter how the
            # startup above went -- __init__ then checks
            # self._translation_init_error to see whether it actually succeeded.
            self._translation_init_event.set()

        # === Phase 2: the main job loop -- one iteration per hover/selection
        # that made it past OCR, until stop() pushes a None sentinel. ===
        try:
            while True:
                item = self._translation_queue.get()  # blocks until a job (or the None sentinel) arrives
                if item is None:
                    break
                job_id, source_text, dictionary_candidates = item
                started = time.monotonic()
                try:
                    lookup_started = time.perf_counter()

                    # --- Step A: if JMdict is currently closed (a previous
                    # lookup failed), and enough time has passed, try
                    # reopening it before this lookup instead of staying
                    # degraded for the rest of the process's life. ---
                    if dictionary is None and time.monotonic() >= dictionary_retry_after:
                        try:
                            dictionary = LocalJapaneseDictionary()
                            log.info("JMdict dictionary reopened after previous failure")
                        except Exception:
                            log.exception("JMdict retry failed; still using phrase translation")
                            dictionary_retry_after = (
                                time.monotonic() + DICTIONARY_RETRY_COOLDOWN_SECONDS
                            )

                    # --- Step B: try JMdict first -- an exact offline
                    # dictionary hit is always preferred over machine
                    # translation, since it's a real definition rather than a
                    # guess. ---
                    dictionary_match = None
                    if dictionary is not None:
                        try:
                            dictionary_match = dictionary.lookup(dictionary_candidates)
                        except Exception:
                            # Transient failures (e.g. an AV file-lock on the
                            # SQLite file) recover on their own -- keep phrase
                            # translation working now, and retry reopening the
                            # dictionary after DICTIONARY_RETRY_COOLDOWN_SECONDS
                            # rather than leaving word lookups degraded forever.
                            log.exception("JMdict lookup failed; falling back to translation")
                            dictionary.close()
                            dictionary = None
                            dictionary_retry_after = (
                                time.monotonic() + DICTIONARY_RETRY_COOLDOWN_SECONDS
                            )

                    # --- Step C: the three-way fallback chain. Exactly one of
                    # these three branches produces the final `translated`
                    # text for this job:
                    #   1. a JMdict definition, if Step B found one;
                    #   2. otherwise Google phrase translation (itself falling
                    #      back to the offline model internally on failure,
                    #      and again explicitly below if it raises); or
                    #   3. the offline model directly, if Google's translator
                    #      never initialized at all. ---
                    if dictionary_match is not None:
                        translated = dictionary.format_match(dictionary_match)
                        cache_kind = "jmdict"
                        model_ms = (time.perf_counter() - lookup_started) * 1000
                    elif phrase_translator is not None:
                        try:
                            translated, cache_kind, model_ms = phrase_translator.translate(
                                source_text, offline_translator
                            )
                        except Exception:
                            log.exception(
                                "phrase translator failed; retrying with offline model"
                            )
                            translated, cache_kind, model_ms = offline_translator.translate(
                                source_text
                            )
                            cache_kind = "offline-recovery-" + cache_kind
                    else:
                        translated, cache_kind, model_ms = offline_translator.translate(
                            source_text
                        )
                        cache_kind = "offline-only-" + cache_kind
                except Exception:
                    # Last-resort catch-all: if every branch above somehow
                    # still raised (e.g. the offline model itself broke),
                    # show a clear retry message instead of losing this
                    # worker thread entirely.
                    log.exception("dictionary/translation lookup failed")
                    translated = "Translation unavailable — hover again to retry."
                    cache_kind = "error"
                    model_ms = 0.0
                elapsed_ms = (time.monotonic() - started) * 1000

                # Only deliver the result if this is still the newest job --
                # if the cursor moved on to another word while this lookup
                # was in flight, showing it now would incorrectly resurrect
                # the popup for text the user isn't hovering anymore.
                if self.running and job_id == self._latest_translation_job_id:
                    self.ui_queue.put(("translation_ready", job_id, translated))
                else:
                    log.info("discarded stale translation job %d", job_id)
                log.info(
                    "lookup job %d: %r -> %r (%s engine=%.0fms worker=%.0fms)",
                    job_id,
                    source_text,
                    translated,
                    cache_kind,
                    model_ms,
                    elapsed_ms,
                )
        finally:
            # Reached both on a normal stop() (None sentinel) and on an
            # unexpected exception escaping the loop -- either way, every
            # engine that was successfully opened above gets closed.
            if dictionary is not None:
                dictionary.close()
            if phrase_translator is not None:
                phrase_translator.close()
            if offline_translator is not None:
                offline_translator.close()

    def _queue_translation(self, job_id, source_text, dictionary_candidates):
        """Hand a job to the translation worker thread, called from
        handle_dwell right after OCR/selection succeeds."""
        self._latest_translation_job_id = job_id
        # Keep at most one not-yet-started job. If the pointer advances twice
        # while inference is busy, only the newest text is still useful.
        try:
            while True:
                self._translation_queue.get_nowait()
        except queue.Empty:
            pass
        self._translation_queue.put_nowait(
            (job_id, source_text, dictionary_candidates)
        )

    def in_cooldown(self, x, y):
        """True if (x, y) is within COOLDOWN_RADIUS_PX of the last trigger
        point and within COOLDOWN_SECONDS of it -- used by handle_dwell to
        avoid re-running OCR/translate on a spot the user is still reading."""
        if self._last_trigger_pos is None:
            return False
        dx = x - self._last_trigger_pos[0]
        dy = y - self._last_trigger_pos[1]
        dist = (dx * dx + dy * dy) ** 0.5
        return (
            dist <= COOLDOWN_RADIUS_PX
            and (time.monotonic() - self._last_trigger_time) <= COOLDOWN_SECONDS
        )

    def handle_dwell(self, x, y):
        """Called by dwell_watch_loop once the cursor has held still long
        enough: try a selection read first (get_selected_text), fall back to
        capture_region + ocr, then analyze the result (furigana, dictionary
        candidates) and queue it for translation. Runs entirely on the dwell
        worker thread; the only UI-visible effects are via ui_queue."""
        # Skip entirely if this spot already triggered a popup recently --
        # avoids re-running OCR/translate on every tiny jitter of the mouse
        # while the user is still reading the current popup.
        if self.in_cooldown(x, y):
            return

        # --- Step 1: get the text to translate, preferring a real text
        # selection (exact, no OCR misreads) and falling back to OCR only
        # when nothing is selected. ---
        t0 = time.monotonic()
        selected = get_selected_text(self._kb_controller, x, y)
        t1 = time.monotonic()

        if selected is not None:
            source = "selection"
            text = selected
            stage_label = "select=%.0fms" % ((t1 - t0) * 1000,)
            t2 = t1
        else:
            source = "ocr"
            shot = self.capture_region(x, y)
            t_capture = time.monotonic()
            try:
                text = self.ocr(shot)
            except Exception:
                # An OCR failure should not silently kill the long-lived dwell
                # worker. Log the traceback and allow the next dwell to retry.
                log.exception("%s failed during OCR", self.ocr_backend_display)
                return
            t2 = time.monotonic()
            stage_label = "capture=%.0fms ocr=%.0fms" % (
                (t_capture - t1) * 1000, (t2 - t_capture) * 1000,
            )

        # Record the trigger regardless of whether text was actually found --
        # an empty-space hover still starts this spot's cooldown, so hovering
        # blank space repeatedly doesn't re-run OCR every poll tick.
        self._last_trigger_pos = (x, y)
        self._last_trigger_time = time.monotonic()

        if not text:
            log.info("dwell at (%d, %d): no text found (%s)", x, y, stage_label)
            return

        # --- Step 2: cap runaway-long selections before any further
        # analysis/translation work is spent on them. ---
        text = truncate_for_analysis(text)

        self._job_counter += 1
        job_id = self._job_counter

        # Wrapped Japanese text has no real line break -- glue OCR lines back
        # into one continuous string so translation/analysis reads it as one
        # sentence instead of two unrelated ones. A selection is already one
        # string, so this is a no-op there.
        flat_text = "".join(text.splitlines())

        # --- Step 3: run the (fast, local) morphological analysis now, on
        # this thread, and hand it straight to the UI so the popup can
        # appear immediately -- the (slower, possibly network-bound)
        # translation itself is handed off separately to the dedicated
        # translation worker thread and arrives later via ui_queue. ---
        furigana_lines = [self.furigana_line(line) for line in text.splitlines()]
        dict_forms = self.dictionary_forms(flat_text)
        self.ui_queue.put(TranslationJob(job_id, x, y, furigana_lines, dict_forms, flat_text))
        self._queue_translation(
            job_id, flat_text, self.dictionary_candidates(flat_text)
        )
        t3 = time.monotonic()

        log.info(
            "dwell at (%d, %d) [%s]: text=%r (%s queued=%.0fms total=%.0fms)",
            x, y, source, text,
            stage_label, (t3 - t2) * 1000, (t3 - t0) * 1000,
        )

    def dwell_watch_loop(self):
        """Background thread: polls cursor position and detects dwell."""
        still_since = None
        still_pos = None
        triggered_for_still = False

        while self.running:
            time.sleep(POLL_INTERVAL_SECONDS)
            if not self.enabled:
                # Fully reset dwell state while paused. Resetting still_since
                # alone (but not still_pos) meant that re-enabling without the
                # cursor moving reached `time.monotonic() - still_since` with
                # still_since=None -> TypeError, killing this thread.
                still_since = None
                still_pos = None
                triggered_for_still = False
                continue

            try:
                x, y = get_cursor_pos()
            except Exception:
                log.exception("cursor polling failed; hover worker will retry")
                still_since = None
                still_pos = None
                triggered_for_still = False
                time.sleep(0.25)
                continue

            if still_pos is None:
                still_pos = (x, y)
                still_since = time.monotonic()
                triggered_for_still = False
                continue

            dx = x - still_pos[0]
            dy = y - still_pos[1]
            moved = (dx * dx + dy * dy) ** 0.5 > DWELL_MOVE_TOLERANCE_PX

            if moved:
                still_pos = (x, y)
                still_since = time.monotonic()
                triggered_for_still = False
                self.ui_queue.put(("cursor_moved", x, y))
                continue

            if (
                not triggered_for_still
                and time.monotonic() - still_since >= DWELL_TIME_SECONDS
            ):
                triggered_for_still = True
                try:
                    self.handle_dwell(x, y)
                except Exception:
                    # Capture, clipboard, morphology, or queue failures must
                    # never terminate the long-lived hover worker.
                    log.exception("hover cycle failed; worker recovered")
                    now = time.monotonic()
                    if now - self._last_runtime_error_notice >= 30.0:
                        self._last_runtime_error_notice = now
                        self.ui_queue.put(
                            (
                                "runtime_error",
                                "A hover could not be processed. The app recovered; "
                                "details were written to the log.",
                            )
                        )
                    still_since = now
                    triggered_for_still = False

    def toggle(self):
        """Flip enabled on/off (the toggle hotkey's handler in the headless
        launcher; DashboardApp implements its own equivalent for the UI)."""
        self.enabled = not self.enabled
        state = "ON" if self.enabled else "OFF"
        log.info("toggled %s", state)
        if not self.enabled:
            self._latest_translation_job_id = None
            self.ui_queue.put(("force_hide",))

    def stop(self):
        """Signal the translation worker to exit (via a None sentinel on
        _translation_queue), best-effort join it, and close the screen
        capture resource. Does not stop dwell_watch_loop itself (that thread
        is owned and joined by the app, e.g. DashboardApp._on_close) --
        self.running=False just makes it a no-op if it checks again."""
        started = time.monotonic()
        self.running = False
        self._latest_translation_job_id = None
        try:
            while True:
                self._translation_queue.get_nowait()
        except queue.Empty:
            pass
        try:
            self._translation_queue.put_nowait(None)
        except queue.Full:
            pass
        # This join is best-effort, not a guarantee: a translation mid-flight
        # (dictionary/Google/offline inference) can't be interrupted, so the
        # worker may still be alive when the timeout elapses -- it's a daemon
        # thread, so the process exiting will reclaim it regardless, but its
        # SQLite/model resources won't get a clean close in that case. Wrapped
        # in try/except so an unexpected join failure can never skip the
        # _sct.close() below.
        try:
            if (
                self._translation_thread.is_alive()
                and threading.current_thread() is not self._translation_thread
            ):
                self._translation_thread.join(timeout=2.0)
                if self._translation_thread.is_alive():
                    log.warning(
                        "translation worker did not stop within two seconds "
                        "-- its resources will be reclaimed at process exit "
                        "instead of closed cleanly"
                    )
        except Exception:
            log.exception("translation worker join failed unexpectedly")
        try:
            self._sct.close()
        except Exception:
            log.exception("screen-capture resource did not close cleanly")
        log.info("HoverTranslator.stop() finished in %.0fms", (time.monotonic() - started) * 1000)


def init_study_db(path=STUDY_DB_PATH, now=None):
    """Create or migrate the study database without losing saved cards."""
    conn = sqlite3.connect(path)
    try:
        # Step 1: create the table if this is a brand-new database. Uses the
        # *current* full schema, so a fresh install never needs migrating.
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS saved_words (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                surface_text TEXT NOT NULL,
                translation TEXT,
                dict_forms TEXT,
                saved_at TEXT NOT NULL,
                learned INTEGER NOT NULL DEFAULT 0,
                repetitions INTEGER NOT NULL DEFAULT 0,
                interval_days INTEGER NOT NULL DEFAULT 0,
                ease_factor REAL NOT NULL DEFAULT 2.5,
                due_at TEXT,
                last_reviewed_at TEXT,
                review_count INTEGER NOT NULL DEFAULT 0,
                lapses INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        # Step 2: for a pre-existing database from an older version of the
        # app, add any SM-2 columns it doesn't have yet -- CREATE TABLE IF
        # NOT EXISTS above is a no-op for a table that already exists, so an
        # old schema needs these ALTER TABLEs to catch up.
        existing = {
            row[1] for row in conn.execute("PRAGMA table_info(saved_words)")
        }
        migrations = {
            "repetitions": "INTEGER NOT NULL DEFAULT 0",
            "interval_days": "INTEGER NOT NULL DEFAULT 0",
            "ease_factor": "REAL NOT NULL DEFAULT 2.5",
            "due_at": "TEXT",
            "last_reviewed_at": "TEXT",
            "review_count": "INTEGER NOT NULL DEFAULT 0",
            "lapses": "INTEGER NOT NULL DEFAULT 0",
        }
        for column, declaration in migrations.items():
            if column not in existing:
                conn.execute(
                    f"ALTER TABLE saved_words ADD COLUMN {column} {declaration}"
                )

        # Step 3: backfill due_at for any row that predates SM-2 scheduling
        # (i.e. still has no due_at at all) -- this only ever touches rows
        # left over from before this migration, never a card scheduled by
        # the current SM-2 code, since those always set due_at themselves.
        migration_now = now or utc_now()
        due_now = format_db_datetime(migration_now)
        learned_due = format_db_datetime(migration_now + timedelta(days=6))
        # Legacy "learned" cards retain a head start and become review cards due
        # in six days. Other existing cards enter the due queue immediately.
        conn.execute(
            """
            UPDATE saved_words
               SET repetitions = CASE WHEN learned = 1 THEN 2 ELSE repetitions END,
                   interval_days = CASE WHEN learned = 1 THEN 6 ELSE interval_days END,
                   review_count = CASE WHEN learned = 1 THEN MAX(review_count, 2) ELSE review_count END,
                   due_at = CASE WHEN learned = 1 THEN ? ELSE ? END
             WHERE due_at IS NULL OR due_at = ''
            """,
            (learned_due, due_now),
        )
        conn.commit()
    finally:
        conn.close()


def format_dict_forms(dict_forms):
    """Render dictionary_forms()-style tuples as one display/storage string,
    e.g. "食べた(たべた)→食べる(たべる)" -- used both by the overlay popup
    and when persisting a saved word to the study database."""
    return "; ".join(
        f"{surface}({reading})→{lemma}({lemma_reading})"
        for surface, reading, lemma, lemma_reading in dict_forms
    )


# --- Hotkey config (JSON-backed, user-editable) -----------------------------


def key_to_str(key):
    """Serialize a pynput key to a stable string for config storage.

    keyboard.Key.f9 -> "f9"; a letter KeyCode -> its char; anything else that
    only has a virtual-key code -> "vk<N>". Returns None if unrepresentable."""
    if isinstance(key, keyboard.Key):
        return key.name
    if isinstance(key, keyboard.KeyCode):
        if key.char:
            return key.char.lower()
        if key.vk is not None:
            return f"vk{key.vk}"
    return None


def str_to_key(s):
    """Parse a config string back into a pynput key object, or None."""
    if not s:
        return None
    if hasattr(keyboard.Key, s):
        return getattr(keyboard.Key, s)
    if s.startswith("vk") and s[2:].isdigit():
        return keyboard.KeyCode.from_vk(int(s[2:]))
    if len(s) == 1:
        return keyboard.KeyCode.from_char(s)
    return None


def key_display(s):
    """Human-friendly label for a hotkey config string ('f9' -> 'F9')."""
    if not s:
        return "(unset)"
    return s.upper()


def load_config():
    """Load config.json, falling back to defaults for anything missing."""
    cfg = {"hotkeys": dict(DEFAULT_HOTKEYS)}
    try:
        with open(CONFIG_PATH, "r", encoding="utf-8") as f:
            data = json.load(f)
        stored = data.get("hotkeys")
        if isinstance(stored, dict):
            for action in DEFAULT_HOTKEYS:
                if isinstance(stored.get(action), str):
                    cfg["hotkeys"][action] = stored[action]
    except FileNotFoundError:
        pass
    except Exception as exc:
        log.warning("failed to read config, using defaults: %s", exc)
    return cfg


def save_config(cfg):
    """Write config.json atomically: write-and-fsync a .tmp file, then
    os.replace() it over the real path. A crash or power loss mid-write
    leaves the previous config intact rather than a half-written/corrupt
    file. Returns True/False rather than raising -- callers (DashboardApp's
    hotkey-rebind handlers) roll back their in-memory change on False."""
    temporary_path = CONFIG_PATH + ".tmp"
    try:
        with open(temporary_path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2, ensure_ascii=False)
            f.flush()
            os.fsync(f.fileno())
        os.replace(temporary_path, CONFIG_PATH)
        return True
    except Exception:
        log.exception("failed to save config")
        try:
            os.remove(temporary_path)
        except OSError:
            pass
        return False


# --- Overlay popup ----------------------------------------------------------


class OverlayWindow:
    """Borderless, always-on-top translation popup, rendered as a Toplevel of
    the given root so it shares one Tk event loop with the rest of the app.

    All methods must be called on the Tk main thread. Cross-thread events (from
    the dwell worker or the hotkey listener) must be marshalled through a queue
    and dispatched here on the main thread -- touching these widgets from
    another thread produces corrupted/blank renders (learned the hard way)."""

    def __init__(self, root):
        """Build the (initially hidden) popup Toplevel and its fonts/canvas.
        Actual content is drawn later by _render, called from show()/
        update_translation()/toggle_pin() once real data is available."""
        import tkinter as tk
        import tkinter.font as tkfont

        self.root = root
        self.win = tk.Toplevel(root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.attributes("-alpha", OVERLAY_ALPHA)

        self.main_font = tkfont.Font(
            root=root, family=OVERLAY_MAIN_FONT[0], size=OVERLAY_MAIN_FONT[1]
        )
        self.furigana_font = tkfont.Font(
            root=root, family=OVERLAY_FURIGANA_FONT[0], size=OVERLAY_FURIGANA_FONT[1]
        )
        self.dict_form_font = tkfont.Font(
            root=root, family=OVERLAY_DICT_FORM_FONT[0], size=OVERLAY_DICT_FORM_FONT[1]
        )

        # Colored outer frame + inset inner frame gives the popup a bordered
        # "card" look; the top strip is an extra accent stripe for prominence.
        outer = tk.Frame(self.win, bg=OVERLAY_ACCENT_COLOR)
        outer.pack()
        inner = tk.Frame(outer, bg=OVERLAY_BG_COLOR)
        inner.pack(padx=2, pady=2)
        strip = tk.Frame(inner, bg=OVERLAY_ACCENT_COLOR, height=4)
        strip.pack(fill="x")
        self.canvas = tk.Canvas(inner, bg=OVERLAY_BG_COLOR, highlightthickness=0)
        self.canvas.pack()

        # Labels shown in the pinned-state banner; kept in sync with the
        # user's configured pin/save hotkeys by the owning app.
        self.pin_label = key_display(DEFAULT_HOTKEYS["pin"])
        self.save_label = key_display(DEFAULT_HOTKEYS["save"])

        self.state = {
            "visible": False, "anchor": None, "hide_at": None, "pinned": False,
            "current_job_id": None, "current_furigana": None,
            "current_dict_forms": None, "current_xy": None,
            "current_raw_text": None, "current_translation": None,
        }

    def set_hotkey_labels(self, pin_label, save_label):
        """Update the "F10 save / F9 unpin"-style labels shown in the pinned
        banner -- called by the owning app whenever the user rebinds a
        hotkey, so the popup never shows a stale key."""
        self.pin_label = pin_label
        self.save_label = save_label

    def _draw_furigana_lines(self, furigana_lines, y):
        """Draw ruby-annotated Japanese text starting at (padding, y); wraps to
        a new row at OVERLAY_MAX_WIDTH_PX so long text doesn't run off the edge
        of the popup. Returns the y position after the last row."""
        furigana_h = self.furigana_font.metrics("linespace")
        main_h = self.main_font.metrics("linespace")
        row_h = furigana_h + main_h + 2
        max_x = OVERLAY_MAX_WIDTH_PX - OVERLAY_PADDING_PX
        for line in furigana_lines:
            x = OVERLAY_PADDING_PX
            for surface, reading in line:
                width = self.main_font.measure(surface)
                if x > OVERLAY_PADDING_PX and x + width > max_x:
                    x = OVERLAY_PADDING_PX
                    y += row_h
                if reading:
                    self.canvas.create_text(
                        x + width / 2, y, text=reading, font=self.furigana_font,
                        fill=OVERLAY_FURIGANA_COLOR, anchor="n",
                    )
                self.canvas.create_text(
                    x, y + furigana_h, text=surface, font=self.main_font,
                    fill=OVERLAY_MAIN_TEXT_COLOR, anchor="nw",
                )
                x += width
            y += row_h
        return y

    def _render(self, furigana_lines, dict_forms, translation_text, x, y):
        """Redraw the whole popup from scratch onto the canvas (pin banner
        if pinned, furigana text, translation, dictionary forms if any),
        resize the window to fit, and position it near (x, y) -- flipping to
        whichever side of the cursor keeps it on the virtual desktop. Called
        by show()/update_translation()/toggle_pin() any time the content or
        pinned state changes; there's no incremental-update path, a full
        canvas.delete("all") + redraw is simple and fast enough at this size."""
        canvas = self.canvas
        canvas.delete("all")  # full redraw every time -- see docstring for why
        cy = OVERLAY_PADDING_PX  # running "next free y" cursor, grows as each section is drawn

        # --- Section 1: pinned banner (only when pinned) ---
        if self.state["pinned"]:
            canvas.create_text(
                OVERLAY_PADDING_PX, cy,
                text=f"📌 Pinned — {self.save_label} save · {self.pin_label} unpin",
                font=self.dict_form_font, fill=OVERLAY_PIN_COLOR, anchor="nw",
            )
            cy += self.dict_form_font.metrics("linespace") + 6

        # --- Section 2: the furigana-annotated Japanese text itself ---
        cy = self._draw_furigana_lines(furigana_lines, cy)

        # --- Section 3: divider line, then the translation/definition text.
        # JMdict definitions are shown in a plain (non-italic) font to read
        # more like a dictionary entry than a machine-translation guess. ---
        cy += 4
        canvas.create_line(
            OVERLAY_PADDING_PX, cy, OVERLAY_MAX_WIDTH_PX, cy,
            fill=OVERLAY_ACCENT_COLOR, width=1,
        )
        cy += 10

        translation_font = (
            OVERLAY_DICTIONARY_FONT
            if translation_text.startswith("JMdict dictionary")
            else OVERLAY_TRANSLATION_FONT
        )
        translation_id = canvas.create_text(
            OVERLAY_PADDING_PX, cy, text=translation_text,
            font=translation_font, fill=OVERLAY_TRANSLATION_COLOR,
            anchor="nw", width=OVERLAY_MAX_WIDTH_PX - 2 * OVERLAY_PADDING_PX,
        )
        # Force Tk to lay out the text item now so its real (possibly
        # multi-line, word-wrapped) bounding box can be measured -- needed to
        # know where the next section should start.
        self.win.update_idletasks()
        t_bbox = canvas.bbox(translation_id)
        cy = t_bbox[3] if t_bbox else cy

        # --- Section 4: dictionary-form breakdown, only shown when the
        # hovered text contained inflected words worth explaining. ---
        if dict_forms:
            cy += 10
            canvas.create_line(
                OVERLAY_PADDING_PX, cy, OVERLAY_MAX_WIDTH_PX, cy,
                fill=OVERLAY_ACCENT_COLOR, width=1,
            )
            cy += 8
            canvas.create_text(
                OVERLAY_PADDING_PX, cy, text="Dictionary form:",
                font=self.dict_form_font, fill=OVERLAY_DICT_FORM_COLOR, anchor="nw",
            )
            cy += self.dict_form_font.metrics("linespace") + 2
            forms_text = "\n".join(
                f"{surface}({reading}) → {lemma}({lemma_reading})"
                for surface, reading, lemma, lemma_reading in dict_forms
            )
            canvas.create_text(
                OVERLAY_PADDING_PX, cy, text=forms_text, font=self.dict_form_font,
                fill=OVERLAY_DICT_FORM_COLOR, anchor="nw",
                width=OVERLAY_MAX_WIDTH_PX - 2 * OVERLAY_PADDING_PX,
            )

        # --- Section 5: shrink-wrap the window to exactly fit what was just
        # drawn (bounded below by OVERLAY_MAX_WIDTH_PX so short text doesn't
        # produce a tiny sliver of a popup). ---
        bbox = canvas.bbox("all")
        content_w = bbox[2] if bbox else OVERLAY_MAX_WIDTH_PX
        content_h = bbox[3] if bbox else OVERLAY_PADDING_PX
        canvas.config(
            width=max(content_w, OVERLAY_MAX_WIDTH_PX) + OVERLAY_PADDING_PX,
            height=content_h + OVERLAY_PADDING_PX,
        )

        self.win.update_idletasks()
        w = self.win.winfo_reqwidth()
        h = self.win.winfo_reqheight()

        # --- Section 6: position the window near the cursor, flipping to
        # whichever side keeps the whole popup on-screen. ---
        vleft, vtop, vwidth, vheight = get_virtual_screen_bounds()
        # Default to the cursor's bottom-right; flip to the opposite side of
        # the cursor on whichever axis would otherwise run off the screen.
        # Clamped against the full virtual desktop so this also works correctly
        # when the cursor is on a secondary monitor.
        px = x + OVERLAY_OFFSET_X_PX
        if px + w > vleft + vwidth:
            px = x - OVERLAY_OFFSET_X_PX - w
        py = y + OVERLAY_OFFSET_Y_PX
        if py + h > vtop + vheight:
            py = y - OVERLAY_OFFSET_Y_PX - h
        px = max(vleft, min(px, vleft + vwidth - w))
        py = max(vtop, min(py, vtop + vheight - h))

        # --- Section 7: apply the geometry, show the window, and (re)arm
        # the auto-hide timer for this render. ---
        self.win.geometry(f"{w}x{h}+{px}+{py}")
        self.win.deiconify()
        self.state["visible"] = True
        self.state["anchor"] = (x, y)
        self.state["hide_at"] = time.monotonic() + OVERLAY_AUTO_HIDE_SECONDS

    def show(self, job):
        """Display a TranslationJob's OCR/selection result immediately (with
        a "Translating…" placeholder in place of the real translation, which
        arrives later and separately -- see update_translation)."""
        self.state["current_job_id"] = job.job_id
        self.state["current_furigana"] = job.furigana_lines
        self.state["current_dict_forms"] = job.dict_forms
        self.state["current_xy"] = (job.x, job.y)
        self.state["current_raw_text"] = job.raw_text
        self.state["current_translation"] = TRANSLATING_PLACEHOLDER
        self._render(job.furigana_lines, job.dict_forms, TRANSLATING_PLACEHOLDER, job.x, job.y)

    def update_translation(self, job_id, translated_text):
        """Fill in the real translation once it arrives. Ignored if the
        popup has since moved on to a different word (job_id mismatch) or
        been hidden -- a slow translation for an abandoned hover must not
        pop back up or overwrite whatever's showing now."""
        if not self.state["visible"] or self.state["current_job_id"] != job_id:
            return
        self.state["current_translation"] = translated_text
        self._render(
            self.state["current_furigana"], self.state["current_dict_forms"],
            translated_text, *self.state["current_xy"],
        )

    def hide(self):
        """Withdraw the popup and clear visible/anchor/hide_at/pinned state.
        Also clears pinned -- hiding always fully resets, there's no
        "hidden but still pinned" state."""
        if self.state["visible"]:
            self.win.withdraw()
            self.state["visible"] = False
            self.state["anchor"] = None
            self.state["hide_at"] = None
            self.state["pinned"] = False

    def toggle_pin(self):
        """Flip the pinned state; returns the new state, or None if nothing is
        showing to pin."""
        if not self.state["visible"]:
            return None
        self.state["pinned"] = not self.state["pinned"]
        self._render(
            self.state["current_furigana"], self.state["current_dict_forms"],
            self.state["current_translation"], *self.state["current_xy"],
        )
        return self.state["pinned"]

    def handle_cursor_moved(self, x, y):
        """Auto-hide the popup once the cursor has moved more than
        HIDE_MOVE_DISTANCE_PX from where it was triggered -- unless pinned,
        which suppresses this so the user can move the mouse away to read
        the popup without it disappearing."""
        if self.state["visible"] and self.state["anchor"] and not self.state["pinned"]:
            ax, ay = self.state["anchor"]
            dx = x - ax
            dy = y - ay
            if (dx * dx + dy * dy) ** 0.5 > HIDE_MOVE_DISTANCE_PX:
                self.hide()

    def tick(self):
        """Called periodically on the main thread to enforce auto-hide."""
        if (
            self.state["visible"]
            and self.state["hide_at"] is not None
            and not self.state["pinned"]
            and time.monotonic() >= self.state["hide_at"]
        ):
            self.hide()

    def current_entry(self):
        """The word/phrase currently shown, as a dict ready for saving -- or
        None unless the popup is both visible and pinned (a deliberate save)."""
        if not self.state["visible"] or not self.state["pinned"]:
            return None
        return {
            "surface_text": self.state["current_raw_text"],
            "translation": self.state["current_translation"],
            "dict_forms": self.state["current_dict_forms"] or [],
        }


def save_entry_to_db(entry):
    """Insert a popup entry (as returned by OverlayWindow.current_entry) into
    the study database. Shared by the standalone launcher and the dashboard."""
    conn = sqlite3.connect(STUDY_DB_PATH)
    try:
        conn.execute(
            "INSERT INTO saved_words (surface_text, translation, dict_forms, saved_at)"
            " VALUES (?, ?, ?, ?)",
            (
                entry["surface_text"],
                entry["translation"],
                format_dict_forms(entry["dict_forms"]),
                format_db_datetime(utc_now()),
            ),
        )
        conn.commit()
    finally:
        conn.close()


def main():
    """Headless overlay-only launcher (no dashboard window). This is the legacy
    entry point; dashboard_app.py is the primary way to run the tool. Quit by
    pressing Ctrl+C in the console."""
    import tkinter as tk

    init_study_db()
    config = load_config()
    hotkeys = {a: str_to_key(config["hotkeys"][a]) for a in ("toggle", "pin", "save")}

    ui_queue: queue.Queue = queue.Queue()
    translator = HoverTranslator(ui_queue)

    root = tk.Tk()
    root.withdraw()  # no dashboard in headless mode -- only the overlay shows

    overlay = OverlayWindow(root)
    overlay.set_hotkey_labels(
        key_display(config["hotkeys"]["pin"]), key_display(config["hotkeys"]["save"])
    )

    def save_current_entry():
        """Save hotkey handler -- mirrors DashboardApp._save_current."""
        entry = overlay.current_entry()
        if entry is None:
            log.info("save ignored -- pin the popup first")
            return
        try:
            save_entry_to_db(entry)
            log.info("saved to study list: %r", entry["surface_text"])
        except Exception as exc:
            log.warning("failed to save entry: %s", exc)

    def poll_queue():
        """ui_queue consumer, run on a timer -- mirrors
        DashboardApp._poll_queue (there's no dashboard here, so no
        toggle_enabled/hotkey-recording cases to handle)."""
        try:
            while True:
                item = ui_queue.get_nowait()
                if isinstance(item, TranslationJob):
                    overlay.show(item)
                elif item[0] == "translation_ready":
                    overlay.update_translation(item[1], item[2])
                elif item[0] == "cursor_moved":
                    overlay.handle_cursor_moved(item[1], item[2])
                elif item[0] == "force_hide":
                    overlay.hide()
                elif item[0] == "toggle_pin":
                    overlay.toggle_pin()
                elif item[0] == "save_entry":
                    save_current_entry()
        except queue.Empty:
            pass
        overlay.tick()
        root.after(50, poll_queue)

    def on_key_press(key):
        # Runs on pynput's listener thread -- never touch tkinter here. Route
        # through ui_queue; poll_queue (main thread) does the real work.
        if hotkeys["toggle"] is not None and key == hotkeys["toggle"]:
            translator.toggle()
        elif hotkeys["pin"] is not None and key == hotkeys["pin"]:
            ui_queue.put(("toggle_pin",))
        elif hotkeys["save"] is not None and key == hotkeys["save"]:
            ui_queue.put(("save_entry",))

    dwell_thread = threading.Thread(target=translator.dwell_watch_loop, daemon=True)
    dwell_thread.start()
    listener = keyboard.Listener(on_press=on_key_press)
    listener.start()

    log.info(
        "hover translator (headless) running -- hotkeys %s/%s/%s. Ctrl+C to quit.",
        key_display(config["hotkeys"]["toggle"]),
        key_display(config["hotkeys"]["pin"]),
        key_display(config["hotkeys"]["save"]),
    )
    root.after(50, poll_queue)
    try:
        root.mainloop()
    except KeyboardInterrupt:
        pass
    finally:
        translator.stop()
        listener.stop()


if __name__ == "__main__":
    main()
