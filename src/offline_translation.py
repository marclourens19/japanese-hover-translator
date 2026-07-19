"""Fast, fully offline Japanese -> English translation.

The bundled Helsinki-NLP OPUS-MT model is converted to CTranslate2 INT8 at
build time. Runtime inference needs only ctranslate2 and sentencepiece: no
PyTorch, cloud API, API key, or network connection.
"""

import json
import os
import re
import sqlite3
import sys
import time
import unicodedata
from collections import OrderedDict
from pathlib import Path

import ctranslate2
import sentencepiece as spm

MODEL_DIRECTORY_NAME = "opus-mt-ja-en-ct2-int8"
MODEL_REQUIRED_FILES = {
    "config.json",
    "model.bin",
    "shared_vocabulary.json",
    "source.spm",
    "target.spm",
    "MODEL_INFO.json",
}
MEMORY_CACHE_SIZE = 512


class TranslationSetupError(RuntimeError):
    """Raised when the bundled translation engine cannot be loaded."""


def resource_root():
    """Source checkout root, or PyInstaller's extracted data root.

    This file lives in src/, one level below the project root -- .parent.parent
    (not .parent) so bundled_model_path()/dictionary_lookup.bundled_dictionary_path()
    keep finding models/ and data/ at the project root, where the PyInstaller
    spec's `datas` list also places them inside a packaged build.
    """
    if getattr(sys, "frozen", False) and hasattr(sys, "_MEIPASS"):
        return Path(sys._MEIPASS)
    return Path(__file__).resolve().parent.parent


def bundled_model_path():
    """Where the quantized OPUS-MT model directory lives, relative to
    resource_root() -- same source-vs-packaged-build distinction as
    dictionary_lookup.bundled_dictionary_path."""
    return resource_root() / "models" / MODEL_DIRECTORY_NAME


def normalize_source_text(text):
    """NFKC-normalize (e.g. full-width -> half-width forms) and collapse
    whitespace -- used as the cache key for both the offline and Google
    translators, so trivially-different inputs share one cache entry."""
    text = unicodedata.normalize("NFKC", text)
    return re.sub(r"\s+", " ", text).strip()


def polish_translation(text):
    """Fix deterministic model artifacts without rewriting valid content."""
    text = re.sub(r"\s+([,.;:!?])", r"\1", text).strip()
    text = re.sub(r"\bseve data\b", "save data", text, flags=re.IGNORECASE)
    text = re.sub(
        r"\b(can(?:not|'t)) mount this item\b",
        lambda match: f"{match.group(1)} equip this item",
        text,
        flags=re.IGNORECASE,
    )
    return text


class OfflineJapaneseTranslator:
    """Thread-owned local translator with memory + persistent SQLite caching."""

    def __init__(self, cache_path, model_dir=None):
        """Load the CTranslate2 engine and SentencePiece processors, and open
        the shared translation cache DB (see GooglePhraseTranslator.__init__
        for why it's the same file, opened via a separate connection).
        Raises TranslationSetupError if any required model file is missing
        or the engine fails to load -- this is the one translation backend
        callers can't gracefully do without (see _translation_loop in
        hover_translate.py), so its failure aborts startup entirely."""
        self.model_dir = Path(model_dir) if model_dir else bundled_model_path()
        missing = sorted(
            name for name in MODEL_REQUIRED_FILES if not (self.model_dir / name).is_file()
        )
        if missing:
            raise TranslationSetupError(
                "The bundled offline translation model is incomplete. Missing: "
                + ", ".join(missing)
            )

        try:
            model_info = json.loads(
                (self.model_dir / "MODEL_INFO.json").read_text(encoding="utf-8")
            )
            self.model_id = (
                f"{model_info['model_id']}:{model_info.get('conversion', 'CTranslate2')}"
            )
            self.source_processor = spm.SentencePieceProcessor(
                model_file=str(self.model_dir / "source.spm")
            )
            self.target_processor = spm.SentencePieceProcessor(
                model_file=str(self.model_dir / "target.spm")
            )
            self.engine = ctranslate2.Translator(
                str(self.model_dir),
                device="cpu",
                compute_type="int8",
                inter_threads=1,
                intra_threads=max(1, min(4, os.cpu_count() or 1)),
            )
        except TranslationSetupError:
            raise
        except Exception as exc:
            raise TranslationSetupError(
                f"The bundled offline translation model could not be loaded: {exc}"
            ) from exc

        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.cache_path, timeout=3.0)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=3000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS translation_cache (
                model_id TEXT NOT NULL,
                source_text TEXT NOT NULL,
                translated_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                hit_count INTEGER NOT NULL DEFAULT 0,
                PRIMARY KEY (model_id, source_text)
            )
            """
        )
        self.connection.commit()
        self.memory_cache = OrderedDict()

    def _remember(self, source_text, translated_text):
        """Insert/refresh an entry in the bounded in-memory LRU cache,
        evicting the least-recently-used entry once over MEMORY_CACHE_SIZE."""
        self.memory_cache[source_text] = translated_text
        self.memory_cache.move_to_end(source_text)
        while len(self.memory_cache) > MEMORY_CACHE_SIZE:
            self.memory_cache.popitem(last=False)

    def _cached(self, source_text):
        """Look up source_text in memory first, then the persistent disk
        cache keyed by (model_id, source_text) -- the model_id component
        means switching model versions can't return a stale translation
        from an older model. Returns (translation, cache_kind) or
        (None, None) on a full miss."""
        translated = self.memory_cache.get(source_text)
        if translated is not None:
            self.memory_cache.move_to_end(source_text)
            return translated, "memory"
        row = self.connection.execute(
            """
            SELECT translated_text
            FROM translation_cache
            WHERE model_id = ? AND source_text = ?
            """,
            (self.model_id, source_text),
        ).fetchone()
        if row is None:
            return None, None
        translated = row[0]
        self.connection.execute(
            """
            UPDATE translation_cache
            SET last_used_at = CURRENT_TIMESTAMP, hit_count = hit_count + 1
            WHERE model_id = ? AND source_text = ?
            """,
            (self.model_id, source_text),
        )
        self.connection.commit()
        self._remember(source_text, translated)
        return translated, "disk"

    def translate(self, text):
        """Translate text with the local CTranslate2 model: cache check,
        then SentencePiece-encode, beam-search decode, SentencePiece-decode,
        and polish_translation() cleanup. max_decoding_length scales with
        input length (bounded to 32-192 tokens) so short phrases don't pay
        for a full-length beam search and long ones aren't truncated
        early. Returns (translated_text, cache_kind, elapsed_ms)."""
        source_text = normalize_source_text(text)
        if not source_text:
            return "", "empty", 0.0

        cached, cache_kind = self._cached(source_text)
        if cached is not None:
            return cached, cache_kind, 0.0

        started = time.perf_counter()
        # 1. Japanese text -> subword tokens the model was trained on.
        source_tokens = self.source_processor.encode(source_text, out_type=str) + ["</s>"]
        max_length = max(32, min(192, len(source_tokens) * 4 + 16))
        # 2. Beam-search decode -- the actual translation step.
        result = self.engine.translate_batch(
            [source_tokens],
            beam_size=4,
            max_decoding_length=max_length,
            repetition_penalty=1.1,
        )[0]
        # 3. Drop special tokens, then subword tokens -> English text.
        target_tokens = [
            token
            for token in result.hypotheses[0]
            if token not in {"</s>", "<pad>"}
        ]
        translated = polish_translation(self.target_processor.decode(target_tokens))
        if not translated:
            raise RuntimeError("The offline model returned an empty translation.")
        elapsed_ms = (time.perf_counter() - started) * 1000

        self.connection.execute(
            """
            INSERT INTO translation_cache (model_id, source_text, translated_text)
            VALUES (?, ?, ?)
            ON CONFLICT(model_id, source_text) DO UPDATE SET
                translated_text = excluded.translated_text,
                last_used_at = CURRENT_TIMESTAMP
            """,
            (self.model_id, source_text, translated),
        )
        self.connection.commit()
        self._remember(source_text, translated)
        return translated, "model", elapsed_ms

    def close(self):
        """Close the cache DB connection; failures are swallowed since this
        runs during best-effort shutdown."""
        try:
            self.connection.close()
        except Exception:
            pass
