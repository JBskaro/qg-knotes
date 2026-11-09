# SPDX-License-Identifier: GPL-3.0-only
"""Vocabulary: fixing recognized variants with endings, short words as whole words only, the file."""

import unittest

from knotes import vocabulary

VOCABULARY = [
    vocabulary.Word("ЕГАИС", ("игаис", "егаис", "егаес")),
    vocabulary.Word("Claude", ("Cloud",)),
    vocabulary.Word("ШЕ", ("ше",)),
]


class TestVocabulary(unittest.TestCase):
    def test_variants_with_endings(self):
        self.assertEqual(vocabulary.fix("для игаиса и по Егаису, егаес", VOCABULARY), "для ЕГАИСа и по ЕГАИСу, ЕГАИС")

    def test_latin_and_case(self):
        self.assertEqual(vocabulary.fix("Привет, cloud. Как дела?", VOCABULARY), "Привет, Claude. Как дела?")

    def test_short_variant_only_as_whole_word(self):
        self.assertEqual(vocabulary.fix("ше и шесть", VOCABULARY), "ШЕ и шесть")

    def test_does_not_touch_parts_of_other_words(self):
        self.assertEqual(vocabulary.fix("сигаисом", VOCABULARY), "сигаисом")

    def test_settings_data_and_spellings(self):
        data = vocabulary.to_data([*VOCABULARY, vocabulary.Word("  "), vocabulary.Word("НДФЛ", (" ", "ндфл "))])
        loaded = vocabulary.from_data(data)
        self.assertEqual(loaded, [*VOCABULARY, vocabulary.Word("НДФЛ", ("ндфл",))])
        self.assertEqual(vocabulary.spellings(loaded), ("ЕГАИС", "Claude", "ШЕ", "НДФЛ"))


if __name__ == "__main__":
    unittest.main()
