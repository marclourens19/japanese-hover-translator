import queue
import threading
import unittest
from unittest import mock

import hover_translate as ht


class OfflineStub:
    def __init__(self, _cache_path):
        self.closed = False

    def translate(self, text):
        return "offline: " + text, "model", 1.0

    def close(self):
        self.closed = True


class DictionaryStub:
    def lookup(self, _candidates):
        raise OSError("dictionary read failed")

    def close(self):
        pass


class PhraseStub:
    def __init__(self, _cache_path):
        pass

    def translate(self, text, _offline):
        return "phrase: " + text, "google", 2.0

    def close(self):
        pass


def bare_translator():
    translator = ht.HoverTranslator.__new__(ht.HoverTranslator)
    translator.running = True
    translator.ui_queue = queue.Queue()
    translator._translation_queue = queue.Queue()
    translator._translation_init_event = threading.Event()
    translator._translation_init_error = None
    translator._latest_translation_job_id = 1
    translator._translation_queue.put((1, "日本語", ["日本語"]))
    translator._translation_queue.put(None)
    return translator


class _SelfUpdatingJobQueue(queue.Queue):
    """A translation queue that updates the owning translator's
    _latest_translation_job_id as each job comes off the queue -- mirrors
    production, where _queue_translation sets that id at enqueue time (so
    a job already in flight is never treated as stale by the worker that's
    about to process it). Lets a test queue up several real jobs and see
    all of their results, instead of every job but the last being (quite
    correctly) discarded as superseded."""

    def __init__(self, translator):
        super().__init__()
        self._translator = translator

    def get(self, *args, **kwargs):
        item = super().get(*args, **kwargs)
        if item is not None:
            self._translator._latest_translation_job_id = item[0]
        return item


def translator_with_jobs(jobs):
    """Like bare_translator, but with an arbitrary sequence of jobs queued
    (each a (job_id, text, candidates) tuple), each treated as "latest" at
    the moment it's dequeued -- see _SelfUpdatingJobQueue."""
    translator = ht.HoverTranslator.__new__(ht.HoverTranslator)
    translator.running = True
    translator.ui_queue = queue.Queue()
    translator._translation_queue = _SelfUpdatingJobQueue(translator)
    translator._translation_init_event = threading.Event()
    translator._translation_init_error = None
    translator._latest_translation_job_id = jobs[0][0]
    for job in jobs:
        translator._translation_queue.put(job)
    translator._translation_queue.put(None)
    return translator


class FailThenWorkDictionary:
    """Stand-in for LocalJapaneseDictionary: always fails lookups, or always
    succeeds, depending on the flag it's constructed with -- used to prove a
    fresh instance (after the retry-cooldown reopen) can recover."""

    def __init__(self, should_fail):
        self.should_fail = should_fail
        self.closed = False

    def lookup(self, _candidates):
        if self.should_fail:
            raise OSError("dictionary read failed")
        return "MATCH"

    def format_match(self, match):
        return "dict: " + match

    def close(self):
        self.closed = True


class TranslationWorkerResilienceTests(unittest.TestCase):
    def test_missing_dictionary_and_google_client_still_use_offline_model(self):
        translator = bare_translator()
        with mock.patch.object(
            ht, "LocalJapaneseDictionary", side_effect=OSError("missing dictionary")
        ), mock.patch.object(ht, "OfflineJapaneseTranslator", OfflineStub), mock.patch.object(
            ht, "GooglePhraseTranslator", side_effect=OSError("client setup failed")
        ):
            translator._translation_loop()

        self.assertTrue(translator._translation_init_event.is_set())
        self.assertIsNone(translator._translation_init_error)
        self.assertEqual(
            translator.ui_queue.get_nowait(),
            ("translation_ready", 1, "offline: 日本語"),
        )

    def test_dictionary_read_failure_falls_back_to_phrase_translation(self):
        translator = bare_translator()
        with mock.patch.object(
            ht, "LocalJapaneseDictionary", return_value=DictionaryStub()
        ), mock.patch.object(ht, "OfflineJapaneseTranslator", OfflineStub), mock.patch.object(
            ht, "GooglePhraseTranslator", PhraseStub
        ):
            translator._translation_loop()

        self.assertEqual(
            translator.ui_queue.get_nowait(),
            ("translation_ready", 1, "phrase: 日本語"),
        )

    def test_dictionary_failure_does_not_retry_before_cooldown_elapses(self):
        """A second hover shortly after a JMdict failure must not reopen the
        dictionary yet (that would hammer a lock that's likely still held);
        it should keep using phrase translation instead."""
        translator = translator_with_jobs(
            [(1, "日本語", ["日本語"]), (2, "日本語", ["日本語"])]
        )
        dictionary_ctor = mock.MagicMock(
            return_value=FailThenWorkDictionary(should_fail=True)
        )
        with mock.patch.object(
            ht, "LocalJapaneseDictionary", dictionary_ctor
        ), mock.patch.object(ht, "OfflineJapaneseTranslator", OfflineStub), mock.patch.object(
            ht, "GooglePhraseTranslator", PhraseStub
        ):
            translator._translation_loop()

        # Only the initial open -- the mid-loop retry must not have fired
        # within the real (default, 30s) cooldown window this test runs in.
        self.assertEqual(dictionary_ctor.call_count, 1)
        self.assertEqual(
            translator.ui_queue.get_nowait(),
            ("translation_ready", 1, "phrase: 日本語"),
        )
        self.assertEqual(
            translator.ui_queue.get_nowait(),
            ("translation_ready", 2, "phrase: 日本語"),
        )

    def test_dictionary_reopens_and_recovers_once_cooldown_elapses(self):
        """Once the retry cooldown has passed, the worker must reopen the
        dictionary automatically and resume using it -- a transient failure
        (e.g. an AV file-lock) must not degrade word lookups permanently."""
        translator = translator_with_jobs(
            [(1, "日本語", ["日本語"]), (2, "日本語", ["日本語"])]
        )
        dictionary_ctor = mock.MagicMock(
            side_effect=[
                FailThenWorkDictionary(should_fail=True),
                FailThenWorkDictionary(should_fail=False),
            ]
        )
        with mock.patch.object(
            ht, "LocalJapaneseDictionary", dictionary_ctor
        ), mock.patch.object(ht, "OfflineJapaneseTranslator", OfflineStub), mock.patch.object(
            ht, "GooglePhraseTranslator", PhraseStub
        ), mock.patch.object(ht, "DICTIONARY_RETRY_COOLDOWN_SECONDS", 0.0):
            translator._translation_loop()

        self.assertEqual(dictionary_ctor.call_count, 2)
        self.assertEqual(
            translator.ui_queue.get_nowait(),
            ("translation_ready", 1, "phrase: 日本語"),
        )
        self.assertEqual(
            translator.ui_queue.get_nowait(),
            ("translation_ready", 2, "dict: MATCH"),
        )


if __name__ == "__main__":
    unittest.main()
