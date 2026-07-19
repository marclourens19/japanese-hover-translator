# Engineering notes

Short write-ups of bugs worth remembering — the kind that don't show up in a
diff, only in the reasoning behind it. First entry: a silent native crash that
only reproduced in the packaged build.

## The MSVCP140 DLL collision

**Symptom:** the freshly packaged `.exe` — built right after the resilience
pass that added rotating diagnostic logging and last-resort exception
reporting — exited immediately on launch. No dashboard window, no error
dialog, no log file. `python src/dashboard_app.py` from the same checkout
worked fine.

That combination is the interesting part. The app had *just* been given a
proper logging setup specifically so failures would be diagnosable, and this
crash happened before any of it could run. Windows reported exit code
`0xc0000005` (access violation) with no Python traceback anywhere — the
interpreter never got far enough to catch it.

### Root-causing it

An access violation before Python-level logging starts points at native code,
not application logic, so the question became: what does the packaged build
load that source mode doesn't load the same way? PyInstaller bundles every
native dependency's own copies of its shared libraries into one flat
`_internal/` directory. Two of this app's dependencies each carry a private
copy of the same DLL:

- `ctranslate2` (used by the bundled offline OPUS-MT translation fallback)
  ships alongside a modern `MSVCP140.dll` (14.51).
- `winrt-runtime` (used by the Windows OCR backend) ships its own, older
  private `MSVCP140.dll` (14.29).

Both land in the same output folder. Windows' DLL search order resolves
`MSVCP140.dll` to whichever one the process happens to touch *first* — and
that turned out to be import-order-dependent, not deterministic in any way
the code was accounting for. When the older winrt copy won the race, the
newer runtime's expectations weren't met and the process died with
`0xc0000005` before `dashboard_app.py` reached its first line of logging
setup.

Confirming it didn't require guesswork: manually importing `winrt` before
`hover_translate` in a plain `python -c` from the checkout reproduced the
same interpreter-level segfault, which meant this wasn't a packaging
artifact — the DLL race existed in source mode too, just usually masked by
whichever import happened to run first by accident.

### The fix — two parts, because either one alone is incomplete

1. **Deterministic import order.** [`src/hover_translate.py`](../src/hover_translate.py)
   imports `offline_translation` (which pulls in `ctranslate2`, and with it
   the newer `MSVCP140.dll`) eagerly at module level. Every `winrt` import in
   the file is deliberately lazy — pushed inside the functions that actually
   perform OCR, so it's only reached once a hover cycle runs, well after
   `ctranslate2` has already claimed the DLL slot. This guarantees the newer,
   backward-compatible copy always loads first, in both source and packaged
   runs.
2. **Removing the duplicate from the package.** Import order alone only
   protects the *first* load — a rebuilt package still physically contained
   both DLLs, which is one refactor away from reintroducing the race if the
   import order ever drifted. [`scripts/japanese_hover_translator.spec`](../scripts/japanese_hover_translator.spec)
   explicitly filters `winrt`'s private `MSVCP140.dll` out of the collected
   binaries before packaging, so only the single newer copy ships at all:

   ```python
   a.binaries = [
       entry
       for entry in a.binaries
       if str(entry[0]).replace("\\", "/").lower() != "winrt/msvcp140.dll"
   ]
   ```

### Guarding against a silent regression

The failure mode here is exactly the kind that regresses invisibly: someone
adds a feature, incidentally hoists a `winrt` import to module level for
convenience, and the packaged build starts crashing on launch again with no
traceback pointing at the cause. [`tests/test_packaging_regression.py`](../tests/test_packaging_regression.py)
exists specifically to catch that before it ships:

- Statically parses `hover_translate.py`'s AST to confirm no `winrt` import
  sits at module level.
- Confirms `offline_translation` is still imported eagerly.
- Actually imports `hover_translate` in a subprocess and asserts `ctranslate2`
  is loaded while no `winrt` module is, proving the ordering invariant holds
  at runtime, not just in the source text.
- Confirms the `.spec` file still contains the `MSVCP140` binary-exclusion
  filter, so the packaging half of the fix can't be silently deleted either.

All four checks run as part of the normal test suite on every push, so this
specific crash has a permanent regression guard rather than depending on
someone remembering the story.
