import unittest

import fugashi

from dictionary_lookup import LocalJapaneseDictionary
import hover_translate as ht


class DictionaryTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.dictionary = LocalJapaneseDictionary()

    @classmethod
    def tearDownClass(cls):
        cls.dictionary.close()

    def test_common_word_returns_real_jmdict_definition(self):
        match = self.dictionary.lookup(["食べる"])
        self.assertIsNotNone(match)
        rendered = self.dictionary.format_match(match)
        self.assertIn("食べる", rendered)
        self.assertIn("to eat", rendered)
        self.assertIn("JMdict", rendered)

    def test_candidate_order_prefers_first_matching_form(self):
        match = self.dictionary.lookup(["存在しない語形", "日本語"])
        self.assertIsNotNone(match)
        self.assertEqual(match.query, "日本語")

    def test_inflected_word_includes_dictionary_form_candidate(self):
        translator = ht.HoverTranslator.__new__(ht.HoverTranslator)
        translator._tagger = fugashi.Tagger()
        candidates = translator.dictionary_candidates("食べました")
        self.assertIn("食べる", candidates)

    def test_sentence_is_not_misclassified_as_single_word(self):
        translator = ht.HoverTranslator.__new__(ht.HoverTranslator)
        translator._tagger = fugashi.Tagger()
        self.assertEqual(translator.dictionary_candidates("日本語を勉強します。"), [])


if __name__ == "__main__":
    unittest.main()
