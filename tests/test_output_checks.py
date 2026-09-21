"""Observable presentation contracts; these are not clinical efficacy tests."""

from dataclasses import FrozenInstanceError
import unittest

from steady_companion.output_checks import Issue, inspect_reply, strip_emoji


def codes(text, **kwargs):
    kwargs.setdefault("question_limit", 1)
    return {issue.code for issue in inspect_reply(text, **kwargs)}


class EmojiTests(unittest.TestCase):
    def test_sequences_leave_the_surrounding_text_and_punctuation(self):
        text = "今天👩🏽‍💻，一起🏳️‍🌈聊聊🇨🇳：❤️❀🌸1️⃣！"
        self.assertEqual(strip_emoji(text), "今天，一起聊聊：！")
        self.assertEqual(strip_emoji("家👨‍👩‍👧‍👦。"), "家。")

    def test_tag_flag_components_are_removed_with_the_flag(self):
        flag = "\U0001f3f4\U000e0067\U000e0062\U000e0065\U000e006e\U000e0067\U000e007f"
        self.assertEqual(strip_emoji("开始" + flag + "结束"), "开始结束")

    def test_plain_math_punctuation_and_nonemoji_selectors_are_preserved(self):
        ordinary = "中文，。！？「引用」；x ≤ 3，a × b − 2 ÷ 4 = ∞；A → B；© 2026；#1 * 2"
        ordinary += "字\ufe00字\ufe0f𠀀\U000e0100、می\u200dروم"
        self.assertEqual(strip_emoji(ordinary), ordinary)
        self.assertEqual(strip_emoji("©️➡️1⃣*️⃣#️⃣"), "")

    def test_only_engine_is_responsible_for_terminal_controls(self):
        self.assertEqual(strip_emoji("\x1b[31m你好\x1b[0m\n"), "\x1b[31m你好\x1b[0m\n")

    def test_emoji_preference_is_explicit(self):
        self.assertIn("emoji_not_allowed", codes("你好🌸"))
        self.assertNotIn("emoji_not_allowed", codes("你好🌸", emoji_mode="on"))
        self.assertNotIn("emoji_not_allowed", codes("普通文字。"))


class OutputCheckTests(unittest.TestCase):
    def test_issue_is_immutable_and_severity_is_restricted(self):
        issue = Issue("question_budget", "soft")
        with self.assertRaises(FrozenInstanceError):
            issue.code = "changed"
        with self.assertRaises(ValueError):
            Issue("anything", "clinical")

    def test_empty_reply_is_hard(self):
        self.assertEqual(inspect_reply(" \n\t", question_limit=0), [Issue("empty_reply", "hard")])

    def test_simple_quoted_questions_do_not_use_the_budget(self):
        text = '他问“怎么回事？”，我回「还好吗？」。书里写 "Why?"，又写 \'Really?\'。你怎么看？'
        self.assertNotIn("question_budget", codes(text))
        self.assertIn("question_budget", codes(text, question_limit=0))
        self.assertNotIn("question_budget", codes("`why?` 只是一个字符串。", question_limit=0))

    def test_apostrophes_unmatched_quotes_and_repeated_marks_are_counted(self):
        self.assertIn("question_budget", codes("Don't worry? What's next?"))
        self.assertIn("question_budget", codes("他说“真的？还有呢？"))
        self.assertIn("question_budget", codes("真的吗？？"))
        self.assertNotIn("question_budget", codes("这是陈述句。你看，标点都保留了！", question_limit=0))

    def test_length_boundaries_are_characters_and_soft(self):
        for hint, limit in (("short", 240), ("normal", 700), ("long", 3000)):
            with self.subTest(hint=hint):
                self.assertNotIn("length_hint", codes("字" * limit, length_hint=hint))
                issues = inspect_reply("字" * (limit + 1), question_limit=0, length_hint=hint)
                self.assertEqual(issues, [Issue("length_hint", "soft")])

    def test_default_inspection_has_no_question_or_length_quota(self):
        text = "第一步怎么做？如果失败呢？你会怎么处理？" + "详细说明。" * 200
        self.assertEqual(inspect_reply(text), [])
        self.assertEqual(inspect_reply(text, question_limit=None, length_hint=None), [])
        constrained = inspect_reply(text, question_limit=1, length_hint="short")
        self.assertEqual({issue.code for issue in constrained}, {"question_budget", "length_hint"})

    def test_disabling_style_quotas_retains_explicit_preferences_and_advisory_repetition(self):
        issues = inspect_reply("我在这里陪你。🌸你好吗？还有呢？", question_limit=None,
                               length_hint=None, recent_assistant=["我在这里陪你。"])
        self.assertEqual(set(issues), {Issue("emoji_not_allowed", "hard"),
                                      Issue("repeated_invitation", "soft")})
        self.assertIn(Issue("premature_closing", "soft"), inspect_reply("祝你今天愉快。"))

    def test_explicit_question_quota_rejects_invalid_configuration(self):
        for invalid in (True, False, -1, "1", 1.5):
            with self.subTest(value=invalid), self.assertRaises(ValueError):
                inspect_reply("普通文字。", question_limit=invalid)

    def test_single_invitation_or_ordinary_empathy_is_not_repetition(self):
        self.assertNotIn("repeated_invitation", codes("如果还有想法，随时告诉我。"))
        self.assertNotIn("repeated_invitation", codes(
            "这确实很难。", recent_assistant=["这确实很难。", "这确实很难。"],
        ))
        self.assertNotIn("repeated_invitation", codes(
            "我在这里等公交车。", recent_assistant=["我在这里等公交车。"],
        ))

    def test_current_invitation_must_repeat_a_recognized_wording_family(self):
        recent = ["你愿意说说？", "如果你不想说也没关系。"]
        self.assertIn("repeated_invitation", codes("暂时不想说也没关系。", recent_assistant=recent))
        self.assertNotIn("repeated_invitation", codes("我们聊电影吧。", recent_assistant=recent * 2))
        self.assertNotIn("repeated_invitation", codes("我在这里陪你。", recent_assistant=recent))
        self.assertIn("repeated_invitation", codes("随时告诉我。还有想法，随时告诉我。"))

    def test_quoted_boilerplate_is_not_an_invitation_or_closing(self):
        self.assertNotIn("repeated_invitation", codes(
            "你不喜欢听到“随时告诉我”。", recent_assistant=["随时告诉我。"],
        ))
        self.assertNotIn("premature_closing", codes("她最后说了“祝你今天愉快”。"))

    def test_history_is_bounded_and_inputs_are_not_mutated(self):
        recent = ["随时告诉我。"] + ["聊聊这部电影。"] * 8
        original = recent.copy()
        text = "随时告诉我🌸。"
        self.assertNotIn("repeated_invitation", codes(text, recent_assistant=recent))
        self.assertEqual(recent, original)
        self.assertEqual(text, "随时告诉我🌸。")

    def test_premature_closing_respects_explicit_permission(self):
        for text in ("祝你接下来愉快！", "剩下的时间过得开心。", "Have a nice day!"):
            with self.subTest(text=text):
                self.assertIn("premature_closing", codes(text))
                self.assertNotIn("premature_closing", codes(text, allow_closing=True))
        self.assertNotIn("premature_closing", codes("剩下的三个苹果分给大家。"))

    def test_reflection_shape_is_a_soft_heuristic_and_needs_repetition(self):
        text = "听起来，这件事让你有些失望。"
        self.assertNotIn("repeated_reflection_heuristic", codes(text))
        recent = ["听起来，你觉得很累。", "你的意思是，你需要休息。"]
        issues = inspect_reply(text, question_limit=0, recent_assistant=recent)
        self.assertEqual(issues, [Issue("repeated_reflection_heuristic", "soft")])
        self.assertNotIn("repeated_reflection_heuristic", codes(
            "听起来很难。我们可以把步骤写下来。", recent_assistant=recent,
        ))

    def test_only_empty_and_emoji_can_be_hard_no_semantic_medical_gate(self):
        self.assertEqual(inspect_reply("我患有抑郁症。", question_limit=0), [])
        issues = inspect_reply(
            "随时告诉我。随时告诉我。你好吗？还有什么？祝你今天愉快。" + "字" * 240,
            question_limit=0, length_hint="short",
        )
        self.assertEqual({issue.severity for issue in issues}, {"soft"})
        self.assertEqual(len({issue.code for issue in issues}), len(issues))


if __name__ == "__main__":
    unittest.main()
