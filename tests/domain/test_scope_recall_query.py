"""RG08 R-B follow-up: deterministic focus-prefix query variants."""

from __future__ import annotations

import unittest

from quintessence_pcs.domains.memory.scope_recall_query import focus_prefix


class FocusPrefixTests(unittest.TestCase):
    def test_normal_question_keeps_a_three_character_content_prefix(self):
        self.assertEqual("我喜欢", focus_prefix("你记得我喜欢什么颜色吗"))
        self.assertEqual("我喜欢", focus_prefix("我喜欢什么颜色"))
        self.assertEqual("我养过", focus_prefix("你记得我养过什么宠物吗"))

    def test_negation_person_and_time_are_retained(self):
        self.assertEqual("我不喜欢", focus_prefix("你记得我不喜欢什么颜色吗"))
        self.assertEqual("张三喜欢", focus_prefix("你记得张三喜欢什么颜色吗"))
        self.assertEqual("昨天我喜欢", focus_prefix("昨天我喜欢什么"))

    def test_short_pronoun_plus_function_word_is_refused(self):
        self.assertIsNone(focus_prefix("你记得我对什么过敏吗"))
        self.assertIsNone(focus_prefix("你记得我想什么吗"))

    def test_hard_tail_is_refused_not_silently_dropped(self):
        self.assertIsNone(
            focus_prefix("你记得我喜欢什么颜色，比如 Pantone 123 吗")
        )
        self.assertIsNone(focus_prefix("你记得我喜欢什么，昨天吗"))

    def test_marked_statement_without_question_tail_is_not_a_variant(self):
        self.assertEqual("我喜欢红色", focus_prefix("你记得我喜欢红色吗"))
        self.assertIsNone(focus_prefix("我喜欢红色"))

    def test_no_hardcoded_topic(self):
        self.assertEqual("我养过", focus_prefix("你记得我养过什么宠物吗"))
        self.assertNotEqual("我喜欢红色", focus_prefix("你记得我喜欢什么颜色吗"))


if __name__ == "__main__":
    unittest.main()
