"""Google phrase translation with bounded latency, cache, and offline fallback."""

import logging
import sqlite3
import threading
import time
from collections import OrderedDict
from pathlib import Path

import deep_translator.google as google_module
from deep_translator import GoogleTranslator

from offline_translation import normalize_source_text

GOOGLE_TIMEOUT = (2.5, 4.5)
GOOGLE_RETRY_BACKOFF_SECONDS = 60
MEMORY_CACHE_SIZE = 512
log = logging.getLogger("japanese_hover_translator.phrase_translation")

# _translate_google monkeypatches google_module.requests.get, which is
# process-wide shared state, not per-instance -- two GooglePhraseTranslator
# instances (or any other concurrent user of deep_translator.google) calling
# translate() at the same moment could otherwise interleave their patch and
# restore, leaving the wrong timeout installed or restoring a value set by
# the other caller. Only one caller in the whole process may hold the patch
# at a time. There's exactly one translation worker thread today, so this
# never contends in practice; it's here so that stays true if that changes.
_GOOGLE_REQUESTS_PATCH_LOCK = threading.Lock()


class GooglePhraseTranslator:
    """Thread-owned Google Translate client with a persistent local cache."""

    def __init__(self, cache_path):
        """Set up the Google client and a persistent SQLite cache at
        cache_path. WAL mode because this same file (TRANSLATION_CACHE_PATH
        in hover_translate.py) is also opened separately by
        OfflineJapaneseTranslator -- two independent connections to one file
        from the same process need WAL to avoid lock contention."""
        self.client = GoogleTranslator(source="ja", target="en")
        self.cache_path = Path(cache_path)
        self.cache_path.parent.mkdir(parents=True, exist_ok=True)
        self.connection = sqlite3.connect(self.cache_path, timeout=3.0)
        self.connection.execute("PRAGMA journal_mode=WAL")
        self.connection.execute("PRAGMA synchronous=NORMAL")
        self.connection.execute("PRAGMA busy_timeout=3000")
        self.connection.execute(
            """
            CREATE TABLE IF NOT EXISTS google_phrase_cache (
                source_text TEXT PRIMARY KEY,
                translated_text TEXT NOT NULL,
                created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                last_used_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP,
                hit_count INTEGER NOT NULL DEFAULT 0
            )
            """
        )
        self.connection.commit()
        self.memory_cache = OrderedDict()
        self.google_unavailable_until = 0.0

    def _remember(self, source_text, translated_text):
        """Insert/refresh an entry in the bounded in-memory LRU cache,
        evicting the least-recently-used entry once over MEMORY_CACHE_SIZE."""
        self.memory_cache[source_text] = translated_text
        self.memory_cache.move_to_end(source_text)
        while len(self.memory_cache) > MEMORY_CACHE_SIZE:
            self.memory_cache.popitem(last=False)

    def _cached(self, source_text):
        """Look up source_text in memory first, then the persistent disk
        cache (promoting a disk hit back into memory). Returns
        (translation, cache_kind) or (None, None) on a full miss."""
        translated = self.memory_cache.get(source_text)
        if translated is not None:
            self.memory_cache.move_to_end(source_text)
            return translated, "google-memory"
        row = self.connection.execute(
            "SELECT translated_text FROM google_phrase_cache WHERE source_text = ?",
            (source_text,),
        ).fetchone()
        if row is None:
            return None, None
        translated = row[0]
        self.connection.execute(
            """
            UPDATE google_phrase_cache
            SET last_used_at = CURRENT_TIMESTAMP, hit_count = hit_count + 1
            WHERE source_text = ?
            """,
            (source_text,),
        )
        self.connection.commit()
        self._remember(source_text, translated)
        return translated, "google-disk"

    def _store(self, source_text, translated_text):
        """Persist a fresh Google result to disk (upsert) and memory."""
        self.connection.execute(
            """
            INSERT INTO google_phrase_cache (source_text, translated_text)
            VALUES (?, ?)
            ON CONFLICT(source_text) DO UPDATE SET
                translated_text = excluded.translated_text,
                last_used_at = CURRENT_TIMESTAMP
            """,
            (source_text, translated_text),
        )
        self.connection.commit()
        self._remember(source_text, translated_text)

    def _translate_google(self, source_text):
        """The actual network call to Google Translate (via deep_translator,
        an unofficial web-scraping client with no built-in timeout). Patches
        in a bounded timeout for the duration of this one call -- see
        _GOOGLE_REQUESTS_PATCH_LOCK above for why that's guarded by a lock."""
        with _GOOGLE_REQUESTS_PATCH_LOCK:
            original_get = google_module.requests.get

            def bounded_get(*args, **kwargs):
                """requests.get with a default timeout, deferring to any
                explicit timeout the caller (deep_translator) already set."""
                kwargs.setdefault("timeout", GOOGLE_TIMEOUT)
                return original_get(*args, **kwargs)

            google_module.requests.get = bounded_get
            try:
                return self.client.translate(source_text)
            finally:
                google_module.requests.get = original_get

    def translate(self, text, offline_translator):
        """Translate text via Google (cached), or the given offline
        translator if Google isn't due to be retried yet or the request
        fails. Every Google failure moves google_unavailable_until forward
        by GOOGLE_RETRY_BACKOFF_SECONDS, so a genuinely-down/blocked Google
        doesn't get hammered on every single hover -- it's retried at most
        once per backoff window. Returns (translated_text, cache_kind,
        elapsed_ms) uniformly across all three paths (memory/disk cache,
        live Google, offline fallback)."""
        source_text = normalize_source_text(text)
        cached, cache_kind = self._cached(source_text)
        if cached is not None:
            return cached, cache_kind, 0.0

        if time.monotonic() >= self.google_unavailable_until:
            started = time.perf_counter()
            try:
                translated = self._translate_google(source_text)
                if not translated:
                    raise RuntimeError("Google Translate returned an empty response.")
                self._store(source_text, translated)
                return translated, "google", (time.perf_counter() - started) * 1000
            except Exception:
                log.warning(
                    "Google phrase translation failed; using offline fallback",
                    exc_info=True,
                )
                self.google_unavailable_until = (
                    time.monotonic() + GOOGLE_RETRY_BACKOFF_SECONDS
                )

        translated, cache_kind, elapsed_ms = offline_translator.translate(source_text)
        return translated, f"offline-fallback-{cache_kind}", elapsed_ms

    def close(self):
        """Close the cache DB connection; failures are swallowed since this
        runs during best-effort shutdown."""
        try:
            self.connection.close()
        except Exception:
            pass
