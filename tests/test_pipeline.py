"""Executable output-contract tests; these do not measure therapeutic efficacy."""

from copy import deepcopy
import json
import unittest
from unittest.mock import patch

from steady_companion.pipeline import MAX_JSON_CHARS, run_pipeline
from steady_companion.provider import ProviderError


def response(value, *, fenced=False, **message_fields):
    content = value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
    if fenced:
        content = "```json\n" + content + "\n```"
    return {"choices": [{"message": {
        "role": "assistant", "content": content, **message_fields,
    }}]}


def candidate(identifier="c1", reply="Hi there.", *, quote="Hello", ref="U0", **fields):
    return {
        "id": identifier, "action": "acknowledge", "length": "short",
        "question_limit": 0, "evidence": [{"ref": ref, "quote": quote}],
        "reply": reply, **fields,
    }


def conversational_candidate(identifier="c1", reply="Hi there.", *, quote="Hello", ref="U0", **fields):
    return {"id": identifier, "reply": reply,
            "evidence": [{"ref": ref, "quote": quote}], **fields}


def generation(*candidates, selected_id="c1"):
    return {"candidates": list(candidates) or [candidate(), candidate("c2", "Hello there.")],
            "selected_id": selected_id}


def review(approved_ids=None, *, issues=None, repair_hint=""):
    return {"approved_ids": ["c1"] if approved_ids is None else approved_ids,
            "issues": issues or [], "repair_hint": repair_hint}


class FakeCall:
    def __init__(self, *responses):
        self.responses = list(responses)
        self.calls = []

    def __call__(self, messages):
        self.calls.append(deepcopy(messages))
        if not self.responses:
            raise AssertionError("Unexpected extra provider request")
        value = self.responses.pop(0)
        if isinstance(value, Exception):
            raise value
        return deepcopy(value)


class PipelineTests(unittest.TestCase):
    def setUp(self):
        self.messages = [
            {"role": "system", "content": "FULL_SKILL_AND_SAFETY_SENTINEL"},
            {"role": "system", "content": "HOST_CAPABILITIES_SENTINEL"},
            {"role": "user", "content": "Hello"},
        ]

    def run_with(self, callback, **options):
        return run_pipeline(callback, self.messages, recent_assistant=[], **options)

    def test_normal_turn_generates_and_reviews_before_returning(self):
        callback = FakeCall(response(generation()), response(review()))
        original = deepcopy(self.messages)
        result = self.run_with(callback)
        self.assertEqual(result.text, "Hi there.")
        self.assertEqual(result.metadata["calls"], 2)
        self.assertEqual(result.metadata["stages"], ["generation", "review"])
        self.assertEqual(result.metadata["selected_action"], "acknowledge")
        self.assertEqual(len(callback.calls), 2)
        self.assertEqual(self.messages, original)
        for messages in callback.calls:
            self.assertEqual(messages[:-1], original)
            self.assertIn("Do not call", messages[-1]["content"].replace("\n", " "))

    def test_minimal_companion_reply_has_no_required_per_turn_action(self):
        callback = FakeCall(response(generation(
            conversational_candidate(), conversational_candidate("c2", "Hello there."))),
            response(review()))
        result = self.run_with(callback)
        self.assertEqual(result.text, "Hi there.")
        self.assertEqual(result.metadata["calls"], 2)
        self.assertEqual(result.metadata["selected_action"], "conversation")
        candidates = result.metadata["generations"][0]["candidates"]
        self.assertTrue(all(item["action"] == "conversation" for item in candidates))
        instruction = callback.calls[0][-1]["content"]
        for field in ('"action":', '"length":', '"question_limit":'):
            self.assertNotIn(field, instruction)

    def test_minimal_and_exact_legacy_candidates_can_coexist(self):
        callback = FakeCall(response(generation(
            conversational_candidate(), candidate("c2", action="contribute"))),
            response(review(["c2"])))
        result = self.run_with(callback)
        self.assertEqual(result.metadata["selected_action"], "contribute")
        self.assertEqual(result.metadata["generations"][0]["candidates"][0]["action"], "conversation")
        # Historical labels are readable metadata, not instructions to the critic.
        review_instruction = callback.calls[1][-1]["content"]
        payload = json.loads(review_instruction.split("UNTRUSTED CANDIDATES AND CHECKS:\n", 1)[1])
        for supplied in payload["candidates"]:
            self.assertEqual(set(supplied), {"id", "reply", "evidence"})

    def test_rejected_candidates_get_exactly_one_generation_and_review_repair(self):
        reject = review([], issues=[{"candidate_id": "c1", "code": "irrelevant"}],
                        repair_hint="Reply to the greeting.")
        callback = FakeCall(response(generation()), response(reject),
                            response(generation(candidate(reply="Hello, good to see you."), candidate("c2"))),
                            response(review()))
        result = self.run_with(callback)
        self.assertEqual(result.text, "Hello, good to see you.")
        self.assertEqual(result.metadata["calls"], 4)
        self.assertEqual(result.metadata["stages"], ["generation", "review", "repair_generation", "repair_review"])
        self.assertEqual(len(callback.calls), 4)
        repair_system = callback.calls[2][-1]["content"]
        self.assertIn("UNTRUSTED DATA", repair_system)
        self.assertIn("Reply to the greeting.", repair_system)
        for messages in callback.calls:
            self.assertEqual(messages[:-1], self.messages)

    def test_repair_does_not_return_without_fresh_approval(self):
        callback = FakeCall(response(generation()), response(review([])),
                            response(generation()), response(review([])))
        with self.assertRaisesRegex(ProviderError, "no reply was completed"):
            self.run_with(callback)
        self.assertEqual(len(callback.calls), 4)

    def test_repair_review_unknown_id_is_rejected(self):
        callback = FakeCall(response(generation()), response(review([])),
                            response(generation()), response(review(["invented"])))
        with self.assertRaises(ProviderError):
            self.run_with(callback)
        self.assertEqual(len(callback.calls), 4)

    def test_fewer_than_two_requests_fails_before_any_provider_call(self):
        for maximum in (0, 1, -1, True, 1.5, "4"):
            with self.subTest(maximum=maximum):
                callback = FakeCall()
                with self.assertRaises(ProviderError):
                    self.run_with(callback, max_calls=maximum)
                self.assertEqual(callback.calls, [])

    def test_two_and_three_call_budgets_do_not_start_unreviewable_repair(self):
        for maximum in (2, 3):
            with self.subTest(maximum=maximum):
                callback = FakeCall(response(generation()), response(review([])))
                with self.assertRaises(ProviderError):
                    self.run_with(callback, max_calls=maximum)
                self.assertEqual(len(callback.calls), 2)

    def test_large_budget_is_still_capped_at_four(self):
        callback = FakeCall(response(generation()), response(review([])),
                            response(generation()), response(review([])))
        with self.assertRaises(ProviderError):
            self.run_with(callback, max_calls=100)
        self.assertEqual(len(callback.calls), 4)

    def test_two_calls_suffice_when_review_approves(self):
        callback = FakeCall(response(generation()), response(review()))
        self.assertEqual(self.run_with(callback, max_calls=2).metadata["calls"], 2)

    def test_single_json_fence_is_supported(self):
        callback = FakeCall(response(generation(), fenced=True), response(review(), fenced=True))
        self.assertEqual(self.run_with(callback).text, "Hi there.")

    def test_malformed_generation_fails_closed_before_review(self):
        malformed = [
            "not JSON", "[]", "Here is JSON: " + json.dumps(generation()),
            "```json\n" + json.dumps(generation()) + "\n```\nextra text",
            "{" * 2000, "x" * (MAX_JSON_CHARS + 1),
            generation(selected_id="missing"),
            generation(candidate()),
            generation(candidate(), candidate()),
            generation(candidate(evidence=[]), candidate("c2")),
            generation(candidate(question_limit=True), candidate("c2")),
            generation(candidate(question_limit=2), candidate("c2")),
            generation(candidate(action="diagnose"), candidate("c2")),
            generation(candidate(action=[]), candidate("c2")),
            generation(candidate(length="brief"), candidate("c2")),
            generation(candidate(reply=[]), candidate("c2")),
            generation(candidate(reply=""), candidate("c2")),
            generation(candidate(reply="x" * 12001), candidate("c2")),
            generation(candidate(reasoning="PRIVATE_REASONING"), candidate("c2")),
            generation(conversational_candidate(action="listen"), conversational_candidate("c2")),
            generation(conversational_candidate(length="short", question_limit=0), conversational_candidate("c2")),
            generation(conversational_candidate(reasoning="PRIVATE_REASONING"), conversational_candidate("c2")),
            {**generation(), "trust_score": 0.7},
        ]
        for value in malformed:
            with self.subTest(value=str(value)[:100]):
                callback = FakeCall(response(value))
                with self.assertRaises(ProviderError):
                    self.run_with(callback, format_recovery=False)
                self.assertEqual(len(callback.calls), 1)

    def test_duplicate_json_keys_are_rejected(self):
        content = json.dumps(generation())
        content = content[:-1] + ', "selected_id": "c2"}'
        callback = FakeCall(response(content))
        with self.assertRaises(ProviderError):
            self.run_with(callback, format_recovery=False)
        self.assertEqual(len(callback.calls), 1)

    def test_non_json_numbers_are_rejected(self):
        content = json.dumps(generation()).replace('"question_limit": 0', '"question_limit": NaN')
        callback = FakeCall(response(content))
        with self.assertRaises(ProviderError):
            self.run_with(callback)

    def test_fabricated_or_inexact_evidence_is_rejected(self):
        anchors = [
            {"quote": "A fabricated memory"}, {"quote": "hello"}, {"quote": ""},
            {"quote": " "}, {"ref": "U1"}, {"ref": "system"},
            {"quote": "FULL_SKILL_AND_SAFETY_SENTINEL"},
            {"quote": "Hello" * 30},
        ]
        for anchor in anchors:
            with self.subTest(anchor=anchor):
                callback = FakeCall(response(generation(candidate(**anchor), candidate("c2", **anchor))))
                with self.assertRaises(ProviderError):
                    self.run_with(callback)
                self.assertEqual(len(callback.calls), 1)

    def test_evidence_references_only_last_eight_actual_user_messages(self):
        self.messages = [self.messages[0]]
        for index in range(10):
            self.messages.extend([
                {"role": "user", "content": f"Visible user turn {index:02d}"},
                {"role": "assistant", "content": "Assistant claim, not user evidence"},
            ])
        callback = FakeCall(response(generation(
            candidate(ref="U0", quote="Visible user turn 09"),
            candidate("c2", ref="U7", quote="Visible user turn 02"))), response(review()))
        result = self.run_with(callback)
        self.assertEqual(result.metadata["generations"][0]["candidates"][1]["evidence_refs"], ["U7"])
        system = callback.calls[0][-1]["content"]
        self.assertIn('"U0": "Visible user turn 09"', system)
        self.assertIn('"U7": "Visible user turn 02"', system)
        self.assertNotIn('"U8":', system)
        bad = FakeCall(response(generation(candidate(quote="Assistant claim", ref="U0"), candidate("c2"))))
        with self.assertRaises(ProviderError):
            self.run_with(bad)

    def test_malformed_review_and_unknown_ids_or_codes_are_blocked(self):
        bad_reviews = [
            review(["absent"]), review(["c1", "c1"]), review("c1"),
            review(issues=[{"candidate_id": "absent", "code": "irrelevant"}]),
            review(issues=[{"candidate_id": "c1", "code": "diagnosis"}]),
            review(issues=[{"candidate_id": "c1", "code": "irrelevant", "reasoning": "PRIVATE"}]),
            review(issues=[{"candidate_id": [], "code": "irrelevant"}]),
            review(repair_hint="x" * 501), review(repair_hint=None),
            {**review(), "reasoning": "PRIVATE_REASONING"}, "no review", [],
        ]
        for value in bad_reviews:
            with self.subTest(value=value):
                callback = FakeCall(response(generation()), response(value))
                with self.assertRaises(ProviderError):
                    self.run_with(callback)
                self.assertEqual(len(callback.calls), 2)

    def test_reviewer_order_overrides_generator_preference(self):
        for approved, chosen in ((["c2", "c1"], "c2"), (["c2"], "c2")):
            with self.subTest(approved=approved):
                callback = FakeCall(response(generation()), response(review(approved)))
                self.assertEqual(self.run_with(callback).metadata["selected_id"], chosen)

    def test_review_cannot_both_approve_and_report_a_defect_for_same_candidate(self):
        for code in ("missed_risk", "boundary_violation", "unnecessary_question"):
            with self.subTest(code=code):
                inconsistent = review(["c1"], issues=[{"candidate_id": "c1", "code": code}])
                callback = FakeCall(response(generation()), response(inconsistent))
                with self.assertRaises(ProviderError):
                    self.run_with(callback)
                self.assertEqual(len(callback.calls), 2)

    def test_legacy_quota_does_not_impose_a_question_flag(self):
        callback = FakeCall(response(generation(candidate(reply="Want to talk?"), candidate("c2"))),
                            response(review()))
        result = self.run_with(callback)
        self.assertEqual(result.text, "Want to talk?")
        check = result.metadata["generations"][0]["candidates"][0]["checks"]
        self.assertNotIn({"code": "question_budget", "severity": "soft"}, check)
        self.assertNotIn('"question_budget"', callback.calls[1][-1]["content"])

    def test_invited_questions_and_depth_have_no_numeric_style_quota(self):
        self.messages[-1]["content"] = "帮我详细准备面试，列几个练习问题。"
        reply = "可以从这些问题练习：你怎样定义这个项目的目标？遇到意见不一致时如何处理？" + "说明评价依据。" * 120
        for make in (conversational_candidate, candidate):
            with self.subTest(shape=make.__name__):
                callback = FakeCall(response(generation(
                    make(reply=reply, quote="列几个练习问题"),
                    make("c2", quote="准备面试"))), response(review()))
                result = self.run_with(callback)
                self.assertEqual(result.text, reply)
                flags = result.metadata["generations"][0]["candidates"][0]["checks"]
                self.assertFalse(any(flag["code"] in {"question_budget", "length_hint"} for flag in flags))

    def test_repetition_remains_advisory_and_review_can_allow_it_in_context(self):
        callback = FakeCall(response(generation(
            conversational_candidate(reply="我在这里陪你。"), conversational_candidate("c2"))),
            response(review()))
        result = run_pipeline(callback, self.messages, recent_assistant=["我在这里陪你。"])
        check = result.metadata["generations"][0]["candidates"][0]["checks"]
        self.assertIn({"code": "repeated_invitation", "severity": "soft"}, check)
        self.assertEqual(result.text, "我在这里陪你。")

    def test_shared_topic_can_anchor_fiction_without_requiring_a_help_action(self):
        self.messages.insert(1, {"role": "system", "content": "USER_CONTROLLED_SHARED_NOTES_SENTINEL"})
        self.messages[-1]["content"] = "接着讲那个怕水的海盗故事。"
        story = "海盗把浴缸装上了船。他宣布：海不归我管，但这一缸归我管。"
        callback = FakeCall(response(generation(
            conversational_candidate(reply=story, quote="海盗故事"),
            conversational_candidate("c2", reply="海盗又把地图拿反了。", quote="海盗故事"))),
            response(review()))
        result = self.run_with(callback)
        self.assertEqual(result.text, story)
        self.assertEqual(result.metadata["selected_action"], "conversation")
        for request in callback.calls:
            self.assertEqual(request[:-1], self.messages)

    def test_hard_empty_reply_cannot_be_selected_even_when_reviewer_approves_it(self):
        callback = FakeCall(response(generation(candidate(reply="🙂"), candidate("c2", "Hello."))),
                            response(review(["c1", "c2"])))
        result = self.run_with(callback)
        self.assertEqual(result.metadata["selected_id"], "c2")
        self.assertEqual(result.text, "Hello.")

    def test_default_emoji_are_removed_before_review_and_output(self):
        callback = FakeCall(response(generation(candidate(reply="Hello. 🙂 ❤️"), candidate("c2"))),
                            response(review()))
        result = self.run_with(callback)
        self.assertEqual(result.text, "Hello.")
        self.assertNotIn("🙂", callback.calls[1][-1]["content"])
        self.assertNotIn("❤️", callback.calls[1][-1]["content"])

    def test_light_mode_keeps_contextual_emoji_for_review(self):
        callback = FakeCall(response(generation(candidate(reply="Hello. 🙂"), candidate("c2"))),
                            response(review()))
        result = self.run_with(callback, emoji_mode="light")
        self.assertEqual(result.text, "Hello. 🙂")
        self.assertIn("use sparse emoji", callback.calls[1][-1]["content"])

    def test_terminal_controls_are_removed_before_review(self):
        callback = FakeCall(response(generation(candidate(reply="\x1b[31mHi.\x1b[0m\x07"), candidate("c2"))),
                            response(review()))
        self.assertEqual(self.run_with(callback).text, "Hi.")
        self.assertNotIn("\\u001b", callback.calls[1][-1]["content"])

    def test_tool_calls_are_never_executed_or_accepted(self):
        tool_call = [{"id": "c1", "type": "function", "function": {"name": "save_memory", "arguments": "{}"}}]
        callback = FakeCall(response(generation(), tool_calls=tool_call))
        with self.assertRaises(ProviderError):
            self.run_with(callback)
        self.assertEqual(len(callback.calls), 1)

    def test_reasoning_is_not_returned_or_substituted_for_content(self):
        callback = FakeCall(response(generation(), reasoning_content="PRIVATE_REASONING"),
                            response(review(), reasoning="PRIVATE_REVIEW_REASONING"))
        result = self.run_with(callback)
        self.assertNotIn("PRIVATE", result.text + json.dumps(result.metadata))
        callback = FakeCall(response(generation(candidate(reply="<think>PRIVATE</think>Hello"), candidate("c2"))))
        with self.assertRaises(ProviderError) as caught:
            self.run_with(callback)
        self.assertNotIn("PRIVATE", str(caught.exception))

    def test_metadata_excludes_quotes_drafts_and_repair_hint(self):
        self.messages[-1]["content"] = "SENSITIVE_USER_DETAIL"
        generated = generation(candidate(reply="FIRST_PRIVATE_DRAFT", quote="SENSITIVE_USER_DETAIL"),
                               candidate("c2", reply="SECOND_PRIVATE_DRAFT", quote="SENSITIVE_USER_DETAIL"))
        callback = FakeCall(response(generated), response(review([], repair_hint="PRIVATE_EDIT_HINT")),
                            response(generated), response(review()))
        result = self.run_with(callback)
        metadata = json.dumps(result.metadata)
        for private in ("SENSITIVE_USER_DETAIL", "FIRST_PRIVATE_DRAFT", "SECOND_PRIVATE_DRAFT", "PRIVATE_EDIT_HINT"):
            self.assertNotIn(private, metadata)
        self.assertIn('"evidence_refs": ["U0"]', metadata)

    def test_pipeline_performs_no_file_writes(self):
        callback = FakeCall(response(generation()), response(review()))
        with patch("builtins.open", side_effect=AssertionError("Pipeline must not persist data")), \
                patch("pathlib.Path.write_text", side_effect=AssertionError("Pipeline must not persist data")), \
                patch("pathlib.Path.write_bytes", side_effect=AssertionError("Pipeline must not persist data")):
            self.assertEqual(self.run_with(callback).text, "Hi there.")

    def test_provider_failure_preserves_providererror_and_does_not_retry(self):
        error = ProviderError("Transport unavailable")
        callback = FakeCall(error)
        with self.assertRaises(ProviderError) as caught:
            self.run_with(callback)
        self.assertIs(caught.exception, error)
        self.assertEqual(len(callback.calls), 1)

    def test_unexpected_callback_failure_is_safely_reported(self):
        callback = FakeCall(ValueError("PRIVATE_ERROR_DETAIL"))
        with self.assertRaises(ProviderError) as caught:
            self.run_with(callback)
        self.assertNotIn("PRIVATE_ERROR_DETAIL", str(caught.exception))
        self.assertEqual(len(callback.calls), 1)

    def test_bad_configuration_or_no_visible_user_fails_before_request(self):
        callback = FakeCall()
        with self.assertRaises(ProviderError):
            self.run_with(callback, emoji_mode="many")
        self.messages = self.messages[:-1]
        with self.assertRaises(ProviderError):
            self.run_with(callback)
        self.assertEqual(callback.calls, [])


if __name__ == "__main__":
    unittest.main()
