# Third-party notices

## JMdict Japanese-English dictionary

This application includes a compact SQLite conversion of the English JMdict data.

- Copyright: Electronic Dictionary Research and Development Group and contributors
- Project: https://www.jmdict.org/
- Source conversion: https://github.com/scriptin/jmdict-simplified
- Bundled dictionary date: 2026-07-13
- License: Creative Commons Attribution-ShareAlike 4.0 International

The dictionary is distributed in accordance with the EDRDG general dictionary licence.
The complete EDRDG licence and CC BY-SA 4.0 legal text are included at
`data/EDRDG_GENERAL_DICTIONARY_LICENCE.html` and `data/CC-BY-SA-4.0.txt`.
Source and update details are recorded in `data/JMDICT_SOURCE.md`. Release maintainers
should run `scripts/update_jmdict.ps1` at least monthly before publishing a new build.

## deep-translator

- Project: https://github.com/nidhaloff/deep-translator
- Purpose: Google Translate web client used for phrase translation
- License: MIT

Google Translate is a network service and is not bundled with this application. This
project is not affiliated with or endorsed by Google. When the service is unavailable,
the bundled OPUS-MT model is used instead.

## Helsinki-NLP OPUS-MT Japanese → English

This application includes a CTranslate2 INT8 conversion of
[`Helsinki-NLP/opus-mt-ja-en`](https://huggingface.co/Helsinki-NLP/opus-mt-ja-en).

- Authors: Helsinki-NLP / OPUS-MT contributors
- Model source: https://huggingface.co/Helsinki-NLP/opus-mt-ja-en
- Original model type: Marian transformer-align
- Included conversion: CTranslate2 INT8
- License: Apache License 2.0

The complete license text is included at
`models/opus-mt-ja-en-ct2-int8/LICENSE`.

## CTranslate2

- Project: https://github.com/OpenNMT/CTranslate2
- License: MIT

## SentencePiece

- Project: https://github.com/google/sentencepiece
- License: Apache License 2.0

## Other bundled runtime components

The Windows bundle also includes the following open-source runtime components:

| Component | Purpose | License |
|---|---|---|
| fugashi | MeCab Python bindings | MIT |
| UniDic Lite code / bundled UniDic 2.1.2 | Japanese morphological dictionary | MIT or WTFPL / BSD |
| MeCab | Japanese morphological analysis | BSD, LGPL 2.1, or GPL 2.0 |
| MSS | Screen capture | MIT |
| Pillow | Image processing | HPND |
| pynput | Global hotkeys | LGPL 3.0 |
| pyperclip | Clipboard access | BSD 3-Clause |
| pytesseract | Optional Tesseract bridge | Apache 2.0 |
| NumPy | Numeric runtime used by CTranslate2 | BSD 3-Clause |
| PyYAML | CTranslate2 configuration support | MIT |
| Python/WinRT projections | Windows built-in OCR bridge | MIT |
| deep-translator | Google phrase-translation client | MIT |
| Requests | HTTPS client used by deep-translator | Apache 2.0 |
| Beautiful Soup | HTML parsing used by deep-translator | MIT |

The optional Tesseract executable and Japanese trained data are detected on the
user's machine and are not included in this distribution. PyInstaller is used to
produce the bundle but is not part of the running application.
