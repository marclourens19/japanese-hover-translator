import os
from pathlib import Path

from importlib.metadata import PackageNotFoundError

from PyInstaller.utils.hooks import collect_data_files, collect_dynamic_libs, copy_metadata


# SPECPATH is the directory containing *this* file -- this spec now lives in
# scripts/, one level below the project root, so go up one level to keep
# every path below pointing at the real root (src/, models/, data/, etc.).
project_dir = Path(SPECPATH).parent
diagnostic_console = os.environ.get("JHT_DIAGNOSTIC_CONSOLE") == "1"

datas = [
    (str(project_dir / "models"), "models"),
    (str(project_dir / "data"), "data"),
    (str(project_dir / "docs"), "docs"),
    (str(project_dir / "README.md"), "."),
    (str(project_dir / "LICENSE"), "."),
    (str(project_dir / "docs" / "THIRD_PARTY_NOTICES.md"), "."),
] + collect_data_files("unidic_lite")
for distribution in (
    "beautifulsoup4",
    "certifi",
    "charset-normalizer",
    "ctranslate2",
    "deep-translator",
    "fugashi",
    "idna",
    "mss",
    "numpy",
    "Pillow",
    "pynput",
    "pyperclip",
    "pytesseract",
    "PyYAML",
    "requests",
    "sentencepiece",
    "soupsieve",
    "unidic-lite",
    "winrt-runtime",
    "winrt-Windows.Foundation",
    "winrt-Windows.Foundation.Collections",
    "winrt-Windows.Globalization",
    "winrt-Windows.Graphics.Imaging",
    "winrt-Windows.Media.Ocr",
    "winrt-Windows.Storage.Streams",
    "urllib3",
):
    try:
        datas += copy_metadata(distribution)
    except PackageNotFoundError:
        pass
binaries = collect_dynamic_libs("ctranslate2")
hiddenimports = [
    "app_logging",
    "dictionary_lookup",
    "offline_translation",
    "phrase_translation",
    "spaced_repetition",
    "unidic_lite",
]

a = Analysis(
    [str(project_dir / "src" / "dashboard_app.py")],
    pathex=[str(project_dir), str(project_dir / "src")],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "jinja2",
        "lxml",
        "openpyxl",
        "pandas",
        "tensorflow",
        "torch",
        "transformers",
    ],
    noarchive=False,
    optimize=1,
)
# winrt-runtime ships an older private MSVCP140.dll (14.29), while the current
# Python/CTranslate2 bundle includes 14.51 at _internal/MSVCP140.dll. Keeping both
# makes Windows' DLL search order import-dependent and can crash before Python starts
# (0xc0000005 in _internal/winrt/MSVCP140.dll). The newer VC runtime is backward
# compatible, so keep the single root copy used by every native extension.
a.binaries = [
    entry
    for entry in a.binaries
    if str(entry[0]).replace("\\", "/").lower() != "winrt/msvcp140.dll"
]
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="JapaneseHoverTranslator",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=diagnostic_console,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name="JapaneseHoverTranslator",
)
