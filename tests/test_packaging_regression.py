"""Regression guard for the packaged-build MSVCP140 crash (see HANDOFF.md).

Background: winrt-runtime ships its own private MSVCP140.dll (14.29), while
CTranslate2 (via the offline translation fallback) needs the newer one (14.51)
that PyInstaller collects at the package root. If Windows loads the winrt
copy first, the packaged app crashes with 0xc0000005 before Python logging
even starts -- so the failure is silent and only reproduces in a packaged
build, not `python dashboard_app.py` from source.

The fix has two parts, and either one being silently undone reintroduces the
crash with nothing else catching it:
  1. hover_translate.py imports offline_translation (which imports
     ctranslate2) eagerly at module level, while every winrt import stays
     lazy (inside functions, only reached once actual OCR/translation runs).
     This guarantees ctranslate2's newer MSVCP140 is already loaded into the
     process before winrt's older copy could ever be touched.
  2. japanese_hover_translator.spec explicitly drops the bundled
     winrt/MSVCP140.dll from the PyInstaller binary list, so only the single
     newer root copy ships at all.

This is not a packaged-build-only risk: manually importing winrt before
hover_translate in a plain `python -c` from this checkout segfaults the
interpreter outright (verified while writing these tests), so the eager/lazy
import ordering matters for `python dashboard_app.py` too, not only for a
PyInstaller build. These tests catch the two ways an innocent refactor could
quietly reintroduce it: hoisting a winrt import to module level in
hover_translate.py, or losing the .spec exclusion filter.
"""

import ast
from pathlib import Path
import subprocess
import sys
import unittest

PROJECT_ROOT = Path(__file__).resolve().parent.parent
SRC_DIR = PROJECT_ROOT / "src"
HOVER_TRANSLATE_PATH = SRC_DIR / "hover_translate.py"
SPEC_PATH = PROJECT_ROOT / "japanese_hover_translator.spec"


def _module_level_import_names(tree):
    """Top-level (not nested in a function/class) import module names."""
    names = []
    for node in tree.body:
        if isinstance(node, ast.Import):
            names.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.append(node.module)
    return names


class PackagingDllOrderRegressionTests(unittest.TestCase):
    def test_no_module_level_winrt_import_in_hover_translate(self):
        """winrt imports must stay lazy (inside functions), not hoisted to
        module level -- a module-level winrt import would race ctranslate2
        to load MSVCP140 first, un-fixing the packaged crash."""
        tree = ast.parse(HOVER_TRANSLATE_PATH.read_text(encoding="utf-8"))
        module_level_names = _module_level_import_names(tree)
        winrt_at_module_level = [n for n in module_level_names if n.startswith("winrt")]
        self.assertEqual(
            winrt_at_module_level,
            [],
            "Found a module-level winrt import in hover_translate.py -- this "
            "must stay lazy (imported inside a function) so ctranslate2 "
            "loads MSVCP140 first. See the module docstring in "
            "test_packaging_regression.py for why.",
        )

    def test_offline_translation_still_imported_at_module_level(self):
        """The eager import of offline_translation (-> ctranslate2) is what
        makes the newer MSVCP140 load first; it must not become lazy."""
        tree = ast.parse(HOVER_TRANSLATE_PATH.read_text(encoding="utf-8"))
        module_level_names = _module_level_import_names(tree)
        self.assertIn(
            "offline_translation",
            module_level_names,
            "hover_translate.py no longer imports offline_translation at "
            "module level -- this was required so ctranslate2 (and its "
            "newer MSVCP140) loads before any lazy winrt import can.",
        )

    def test_importing_hover_translate_loads_ctranslate2_without_touching_winrt(self):
        """End-to-end proof of the invariant the two static checks above
        exist to protect: a fresh interpreter that just imports
        hover_translate has ctranslate2 loaded, and has NOT loaded winrt
        (which only happens lazily once OCR actually runs)."""
        probe = (
            "import sys\n"
            "import hover_translate\n"
            "assert 'ctranslate2' in sys.modules, 'ctranslate2 did not load eagerly'\n"
            "winrt_modules = [m for m in sys.modules if m == 'winrt' or m.startswith('winrt.')]\n"
            "assert not winrt_modules, 'winrt loaded eagerly at import time: %r' % winrt_modules\n"
            "print('OK')\n"
        )
        result = subprocess.run(
            [sys.executable, "-c", probe],
            cwd=str(SRC_DIR),
            capture_output=True,
            text=True,
            timeout=60,
        )
        self.assertEqual(
            result.returncode,
            0,
            f"stdout={result.stdout!r} stderr={result.stderr!r}",
        )
        self.assertIn("OK", result.stdout)

    def test_spec_still_excludes_bundled_winrt_msvcp140(self):
        """The PyInstaller spec must keep dropping winrt's private
        MSVCP140.dll from the packaged binaries, or a fresh build
        reintroduces the two-copy DLL race even if the import order above
        stays correct (the import order only protects the *first* load;
        the .spec filter is what keeps the conflicting copy out of the
        package entirely)."""
        spec_text = SPEC_PATH.read_text(encoding="utf-8")
        self.assertIn(
            "msvcp140",
            spec_text.lower(),
            "japanese_hover_translator.spec no longer mentions MSVCP140 -- "
            "the filter that drops winrt's private copy from the packaged "
            "build appears to have been removed.",
        )
        self.assertIn(
            "a.binaries",
            spec_text,
            "japanese_hover_translator.spec no longer reassigns a.binaries -- "
            "the MSVCP140 exclusion filter appears to have been removed.",
        )


if __name__ == "__main__":
    unittest.main()
