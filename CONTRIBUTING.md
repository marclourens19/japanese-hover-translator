# Contributing

Thanks for considering contributing to Japanese Hover Translator. This is a
single-maintainer hobby project turned into something other Japanese learners
might find useful — contributions of any size are welcome, from a typo fix to
a new feature.

## Getting set up

Follow [Install and run from source](README.md#install-and-run-from-source)
in the README, including `git lfs install` before cloning — the bundled JMdict
database and translation model are tracked with Git LFS, and a clone without
it gets small pointer files instead of working data.

Run the app with `python src/dashboard_app.py` and the test suite with:

```powershell
python -m unittest discover -s tests -t . -v
```

or `run_tests.cmd`. All 62+ tests should pass before and after your change.

## Where things live

The module docstrings and class/method docstrings throughout the codebase are
the primary source of truth for how each piece works — they were written
specifically so a new contributor could read a file and understand it without
needing this document to explain internals. A few starting points:

- `dashboard_app.DashboardApp` — the whole UI and its three-thread model.
  Start with the class docstring.
- `hover_translate.HoverTranslator` — the background engine (dwell detection,
  OCR, translation dispatch). Also start with the class docstring.
- `hover_translate.OverlayWindow` — the popup itself.
- `dictionary_lookup.py`, `phrase_translation.py`, `offline_translation.py` —
  the three translation backends (JMdict, Google, offline OPUS-MT).
- `spaced_repetition.py` — pure SM-2 scheduling logic, no UI/DB dependencies,
  the easiest file to unit test changes against in isolation.

See also the [How it works](README.md#how-it-works) diagram in the README for
the overall data flow before diving into any one file.

## Ground rules for changes

- **Tkinter widgets may only be touched from the Tk main thread.** The dwell
  worker and the hotkey listener run on their own threads and communicate
  with the UI exclusively through `ui_queue`, drained by
  `DashboardApp._poll_queue`. Touching a widget from another thread has
  caused real corrupted-render bugs in this project's history — if you're
  adding a new cross-thread interaction, route it through the queue the same
  way the existing ones do.
- **Every failure mode should degrade, not crash.** The translation pipeline
  in particular is built so that a JMdict failure falls back to phrase
  translation, a Google failure falls back to the offline model, and so on —
  see `HoverTranslator._translation_loop`'s docstring. If you're touching
  that path, keep that property: log and degrade rather than letting an
  exception kill a background thread.
- **Don't reintroduce the MSVCP140 packaged-build crash.** `src/hover_translate.py`
  imports `offline_translation` (which pulls in `ctranslate2`) eagerly at
  module level, and every `winrt` import stays lazy (inside a function). This
  ordering is load-bearing — see the comment above the `offline_translation`
  import and `tests/test_packaging_regression.py`, which will fail if this
  regresses.
- **Run the full test suite**, not just tests near your change — several
  tests exist specifically to catch regressions in past bugs (see
  `tests/test_packaging_regression.py`, `tests/test_translator_stop.py`,
  `tests/test_clipboard_resilience.py`).
- **New behavior should ship with a test where practical.** The pure-logic
  modules (`src/spaced_repetition.py`, the OCR filtering functions in
  `src/hover_translate.py`) are the easiest to test without touching Tk or the
  OS; see the existing tests in `tests/` for the established mocking
  patterns (e.g. `tests/test_windows_ocr.py` for mocking `winrt`).

## Reporting bugs / requesting features

Open a GitHub issue. For anything you believe is a genuine security issue
rather than a regular bug, see [SECURITY.md](SECURITY.md) instead — please
don't file those as public issues.

## Pull requests

Keep PRs focused on one change. Describe *why* the change is needed, not just
what it does — the "why" is what's hard to recover later from the diff alone.
If your change touches the threading model, the packaged-build import order,
or the translation fallback chain, call that out explicitly in the PR
description so it gets extra scrutiny in review.
