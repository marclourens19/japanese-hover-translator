"""Automated regression tests for Japanese Hover Translator."""

import sys
from pathlib import Path

# Application modules live in src/, not the project root -- add it to
# sys.path here (once, before any test module runs) so every test file can
# keep doing `import hover_translate as ht` etc. unchanged, the same way
# `python src/dashboard_app.py` gets it via the running script's own directory.
_SRC_DIR = str(Path(__file__).resolve().parent.parent / "src")
if _SRC_DIR not in sys.path:
    sys.path.insert(0, _SRC_DIR)
