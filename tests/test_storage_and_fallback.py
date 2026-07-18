from datetime import datetime, timedelta, timezone
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest import mock
import json

import hover_translate as ht
from phrase_translation import GooglePhraseTranslator
from spaced_repetition import format_db_datetime, parse_db_datetime, schedule_review, ReviewState


class StudyMigrationTests(unittest.TestCase):
    def test_legacy_database_migrates_once_without_losing_text(self):
        now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "study.db"
            connection = sqlite3.connect(path)
            connection.execute(
                """
                CREATE TABLE saved_words (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    surface_text TEXT NOT NULL,
                    translation TEXT,
                    dict_forms TEXT,
                    saved_at TEXT NOT NULL,
                    learned INTEGER NOT NULL DEFAULT 0
                )
                """
            )
            connection.executemany(
                """
                INSERT INTO saved_words
                    (surface_text, translation, dict_forms, saved_at, learned)
                VALUES (?, ?, '', '2026-07-01 00:00:00', ?)
                """,
                [("食べる", "to eat", 0), ("日本語", "Japanese", 1)],
            )
            connection.commit()
            connection.close()

            ht.init_study_db(path, now)
            ht.init_study_db(path, now + timedelta(days=1))

            connection = sqlite3.connect(path)
            rows = connection.execute(
                """
                SELECT surface_text, repetitions, interval_days, review_count, due_at
                FROM saved_words ORDER BY id
                """
            ).fetchall()
            connection.close()

        self.assertEqual([row[0] for row in rows], ["食べる", "日本語"])
        self.assertEqual(rows[0][1:4], (0, 0, 0))
        self.assertEqual(rows[1][1:4], (2, 6, 2))
        self.assertEqual(parse_db_datetime(rows[0][4]), now)
        self.assertEqual(parse_db_datetime(rows[1][4]), now + timedelta(days=6))

    def test_migration_never_touches_a_card_after_a_real_review(self):
        """Previously only the "still NULL due_at" migration guard was
        tested. This proves the same guard holds once a card has gone
        through an actual SM-2 review (schedule_review), not just the
        migration's own placeholder due_at -- a later app start (another
        init_study_db call) must leave it completely alone."""
        now = datetime(2026, 7, 18, 12, 0, tzinfo=timezone.utc)
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "study.db"
            ht.init_study_db(path, now)

            connection = sqlite3.connect(path)
            connection.execute(
                "INSERT INTO saved_words (surface_text, translation, dict_forms, saved_at)"
                " VALUES ('食べる', 'to eat', '', ?)",
                (format_db_datetime(now),),
            )
            connection.commit()
            row_id = connection.execute(
                "SELECT id FROM saved_words WHERE surface_text = '食べる'"
            ).fetchone()[0]
            connection.close()

            # First migration call assigns this brand-new card a due_at.
            ht.init_study_db(path, now)

            # A real review happens (mirrors dashboard_app._answer).
            result = schedule_review(ReviewState(), "good", reviewed_at=now)
            connection = sqlite3.connect(path)
            connection.execute(
                """
                UPDATE saved_words
                   SET repetitions = ?, interval_days = ?, ease_factor = ?,
                       due_at = ?, last_reviewed_at = ?, review_count = ?, lapses = ?
                 WHERE id = ?
                """,
                (
                    result.repetitions, result.interval_days, result.ease_factor,
                    format_db_datetime(result.due_at), format_db_datetime(result.last_reviewed_at),
                    result.review_count, result.lapses, row_id,
                ),
            )
            connection.commit()
            connection.close()
            reviewed_snapshot = (
                result.repetitions, result.interval_days, result.ease_factor,
                format_db_datetime(result.due_at), format_db_datetime(result.last_reviewed_at),
                result.review_count, result.lapses,
            )

            # A later app start (days later) must not touch this reviewed row.
            ht.init_study_db(path, now + timedelta(days=30))

            connection = sqlite3.connect(path)
            after = connection.execute(
                """
                SELECT repetitions, interval_days, ease_factor, due_at,
                       last_reviewed_at, review_count, lapses
                  FROM saved_words WHERE id = ?
                """,
                (row_id,),
            ).fetchone()
            connection.close()

        self.assertEqual(after, reviewed_snapshot)


class ConfigStorageTests(unittest.TestCase):
    def test_failed_atomic_save_preserves_previous_config(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "config.json"
            original = {"hotkeys": {"toggle": "f9", "pin": "f10", "save": "f11"}}
            path.write_text(json.dumps(original), encoding="utf-8")
            with mock.patch.object(ht, "CONFIG_PATH", str(path)):
                saved = ht.save_config({"not_serializable": object()})
            restored = json.loads(path.read_text(encoding="utf-8"))

        self.assertFalse(saved)
        self.assertEqual(restored, original)
        self.assertFalse(Path(str(path) + ".tmp").exists())


class PhraseFallbackTests(unittest.TestCase):
    class OfflineStub:
        def __init__(self):
            self.calls = []

        def translate(self, text):
            self.calls.append(text)
            return "offline result", "model", 12.0

    def test_google_failure_uses_offline_and_enters_backoff(self):
        with tempfile.TemporaryDirectory() as directory:
            translator = GooglePhraseTranslator(Path(directory) / "cache.db")
            offline = self.OfflineStub()
            with mock.patch.object(
                translator, "_translate_google", side_effect=OSError("offline")
            ):
                result = translator.translate("日本語を勉強します", offline)
            translator.close()

        self.assertEqual(result[0], "offline result")
        self.assertEqual(result[1], "offline-fallback-model")
        self.assertGreater(translator.google_unavailable_until, 0)
        self.assertEqual(offline.calls, ["日本語を勉強します"])

    def test_successful_google_result_is_persistently_cached(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "cache.db"
            first = GooglePhraseTranslator(path)
            offline = self.OfflineStub()
            with mock.patch.object(first, "_translate_google", return_value="Japanese"):
                translated = first.translate("日本語", offline)
            first.close()

            second = GooglePhraseTranslator(path)
            with mock.patch.object(
                second, "_translate_google", side_effect=AssertionError("network used")
            ):
                cached = second.translate("日本語", offline)
            second.close()

        self.assertEqual(translated[1], "google")
        self.assertEqual(cached[0], "Japanese")
        self.assertEqual(cached[1], "google-disk")
        self.assertEqual(offline.calls, [])


if __name__ == "__main__":
    unittest.main()
