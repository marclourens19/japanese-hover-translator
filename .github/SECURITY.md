# Security

Japanese Hover Translator is a local desktop application: it has no server, no
accounts, and no backend. Everything it does happens on the machine it's
running on, with two exceptions described below. This document explains the
app's actual data flow and how to report a real security issue.

## What this app can see, and where it goes

- **Screen content.** When you hover over Japanese text, the app captures a
  small region of your screen around the cursor and runs OCR on it locally
  (Tesseract or Windows OCR, whichever is active — shown on the Overview
  page). That image is processed in memory and discarded; it is never saved
  to disk or sent anywhere.
- **Clipboard.** If the window under your cursor has focus, the app briefly
  simulates Ctrl+C to read a text selection directly (more accurate than
  OCR), then restores whatever was on the clipboard before. Known
  terminal/console window classes are explicitly skipped, since Ctrl+C means
  "send SIGINT" there, not "copy" — see `CONSOLE_WINDOW_CLASSES` in
  `src/hover_translate.py`. This only ever reads a selection you already made in
  the foreground window; it does not poll or monitor the clipboard
  otherwise.
- **Network.** Single words that resolve to a JMdict entry are looked up
  entirely offline. Everything else (phrases, sentences) is sent to Google
  Translate's public web endpoint (via the unofficial `deep-translator`
  client — there is no API key or account involved) to get a translation.
  If that fails or is unreachable, the bundled offline OPUS-MT model handles
  it instead, with no network call at all. Successful phrase translations
  are cached locally (`translation_cache.db`) so the same phrase is never
  sent twice.
- **Local storage.** Settings (`config.json`), saved words
  (`study_words.db`), the translation cache, and diagnostic logs are all
  plain local files — under the project directory when run from source, or
  `%LOCALAPPDATA%\JapaneseHoverTranslator` in a packaged build. Nothing here
  is encrypted; treat these files the way you'd treat any other local
  application data. None of it is transmitted anywhere by this app.

**Practical implication:** don't hover over or select text you wouldn't want
sent to Google Translate's servers (passwords, private messages, anything
sensitive). Everything else stays local.

## A known trust boundary: the Tesseract executable

If you use the Tesseract OCR backend, this app invokes whatever
`tesseract.exe` it discovers on `PATH`, in common install directories, or via
the `TESSERACT_CMD` environment variable (see `_find_tesseract_command` in
`src/hover_translate.py`). It does not verify the binary's authenticity beyond
that discovery step. This is standard behavior for any tool that shells out
to a system-installed dependency, but it does mean the app trusts whatever
executable answers to that name/path on your system — install Tesseract only
from the [official UB-Mannheim build](https://github.com/UB-Mannheim/tesseract/wiki)
or your own build from source.

## Reporting a vulnerability

If you find a genuine security issue (not a bug report — see the issue
tracker for those), please open a private
[GitHub Security Advisory](https://github.com/marclourens19/japanese-hover-translator/security/advisories/new) for this repository
rather than a public issue, so it can be fixed before details are public.

## Supported versions

Only the latest tagged release is supported. This is a single-maintainer
open-source project without a formal LTS policy.
