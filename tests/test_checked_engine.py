"""Checked engine integration with realistic provider JSON and a local store."""

from copy import deepcopy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from steady_companion import engine
from steady_companion.engine import ContextChangedError, Conversation
from steady_companion.provider import ProviderError
from steady_companion.store import Store


USER_TEXT = "今天修好了自行车"


def provider_response(payload):
    return {
        "choices": [{"message": {
            "role": "assistant", "content": json.dumps(payload, ensure_ascii=False),
        }}],
        "usage": {"prompt_tokens": 23, "completion_tokens": 17, "total_tokens": 40},
    }


def generated(first="修好了，明天出门就方便了。", second="自己动手修好，很有成就感。"):
    return provider_response({
        "candidates": [{
            "id": identifier, "action": "contribute", "length": "short",
            "question_limit": 0,
            "evidence": [{"ref": "U0", "quote": "修好了自行车"}],
            "reply": reply,
        } for identifier, reply in (("c1", first), ("c2", second))],
        "selected_id": "c1",
    })


def reviewed(*approved, reject=False):
    return provider_response({
        "approved_ids": list(approved),
        "issues": [{"candidate_id": "c1", "code": "irrelevant"}] if reject else [],
        "repair_hint": "回应修好自行车这件事。" if reject else "",
    })


class CheckedClient:
    def __init__(self, *replies):
        self.replies = list(replies)
        self.calls = []

    def complete(self, messages, *, tools=None, tool_choice=None):
        self.calls.append(deepcopy({
            "messages": messages, "tools": tools, "tool_choice": tool_choice,
        }))
        if not self.replies:
            raise AssertionError("Unexpected extra provider request")
        reply = self.replies.pop(0)
        return reply() if callable(reply) else deepcopy(reply)


class CheckedEngineTests(unittest.TestCase):
    def setUp(self):
        temporary = tempfile.TemporaryDirectory()
        self.addCleanup(temporary.cleanup)
        self.root = Path(temporary.name)
        self.store = Store(self.root / "state")
        self.skill = self.root / "skill"
        (self.skill / "references").mkdir(parents=True)
        self.core = "CHECKED_CORE_SENTINEL"
        self.safety = "CHECKED_SAFETY_SENTINEL"
        (self.skill / "SKILL.md").write_text(self.core, encoding="utf-8")
        (self.skill / "references" / "safety.md").write_text(self.safety, encoding="utf-8")
        for name in engine.MODULES:
            (self.skill / "references" / f"{name}.md").write_text(
                f"CHECKED_MODULE_{name}", encoding="utf-8",
            )
        skill_patch = patch.object(engine, "SKILL_DIR", self.skill)
        skill_patch.start()
        self.addCleanup(skill_patch.stop)

    def test_default_checks_two_calls_with_original_skill_safety_memory_and_dialogue(self):
        memory = self.store.remember("请直接回应我正在聊的话题。", "preference")
        revision = self.store.revision()
        client = CheckedClient(generated(), reviewed("c1", "c2"))
        conversation = Conversation(client, self.store)
        prior = [
            {"role": "user", "content": "这辆车的刹车坏了。"},
            {"role": "assistant", "content": "先别骑上路，等修好刹车再用。"},
        ]
        conversation.history = deepcopy(prior)

        turn = conversation.reply(USER_TEXT)

        self.assertEqual(turn.calls, 2)
        self.assertEqual(turn.inspection["stages"], ["generation", "review"])
        self.assertEqual(turn.memory_ids, [memory["id"]])
        self.assertEqual(len(client.calls), 2)
        original = client.calls[0]["messages"][:-1]
        self.assertEqual(original[2:], prior + [{"role": "user", "content": USER_TEXT}])
        self.assertIn(memory["text"], original[1]["content"])
        for request in client.calls:
            self.assertEqual(request["messages"][:-1], original)
            self.assertIn(self.core, request["messages"][0]["content"])
            self.assertIn(self.safety, request["messages"][0]["content"])
            self.assertIn("Current wishes override old preferences", request["messages"][0]["content"])
            self.assertIsNone(request["tools"])
            self.assertIsNone(request["tool_choice"])
        self.assertEqual(conversation.history, prior + [
            {"role": "user", "content": USER_TEXT},
            {"role": "assistant", "content": turn.text},
        ])
        self.assertEqual(self.store.revision(), revision)
        self.assertEqual(self.store.list_memories(), [memory])
        self.assertEqual(conversation.usage.calls, 2)
        self.assertEqual(conversation.usage.total_tokens, 80)

    def test_rejected_drafts_never_enter_history_during_repair_or_after_approval(self):
        observed_history = []
        conversation = None

        def observe_and_return(payload):
            def reply():
                observed_history.append(deepcopy(conversation.history))
                return payload
            return reply

        client = CheckedClient(*[
            observe_and_return(payload) for payload in (
                generated("REJECTED_DRAFT_PRIVATE", "UNSELECTED_DRAFT_PRIVATE"),
                reviewed(reject=True),
                generated("这下可以安心骑车了。", "REPAIR_ALTERNATIVE_PRIVATE"),
                reviewed("c1"),
            )
        ])
        conversation = Conversation(client, self.store)

        turn = conversation.reply(USER_TEXT)

        self.assertEqual(turn.calls, 4)
        self.assertEqual(observed_history, [[], [], [], []])
        self.assertEqual(conversation.history, [
            {"role": "user", "content": USER_TEXT},
            {"role": "assistant", "content": "这下可以安心骑车了。"},
        ])
        self.assertNotIn("PRIVATE", json.dumps(conversation.history + [turn.inspection]))
        self.assertEqual(self.store.list_memories(), [])

    def test_unapproved_turn_adds_neither_user_nor_drafts_to_existing_history(self):
        client = CheckedClient(generated("REJECTED_DRAFT_PRIVATE"), reviewed(reject=True))
        conversation = Conversation(client, self.store, max_calls=2)
        prior = [{"role": "user", "content": "你好"}, {"role": "assistant", "content": "你好。"}]
        conversation.history = deepcopy(prior)

        with self.assertRaises(ProviderError):
            conversation.reply(USER_TEXT)

        self.assertEqual(len(client.calls), 2)
        self.assertEqual(conversation.history, prior)
        self.assertEqual(self.store.list_memories(), [])

    def test_memory_revision_change_during_review_discards_approval_and_clears_history(self):
        for action in ("correct", "wipe_all"):
            with self.subTest(action=action):
                memory = self.store.remember("OLD_CONTEXT_PRIVATE", "preference")
                other = Store(self.store.data_dir)

                def changed_review():
                    if action == "correct":
                        other.correct(memory["id"], "新偏好")
                    else:
                        other.wipe_all()
                    return reviewed("c1")

                client = CheckedClient(generated("STALE_REPLY_PRIVATE"), changed_review)
                conversation = Conversation(client, self.store)
                conversation.history = [
                    {"role": "user", "content": "OLD_CONTEXT_PRIVATE"},
                    {"role": "assistant", "content": "旧回应"},
                ]

                with self.assertRaises(ContextChangedError) as caught:
                    conversation.reply(USER_TEXT)

                self.assertEqual(len(client.calls), 2)
                self.assertEqual(conversation.usage.calls, 2)
                self.assertEqual(conversation.history, [])
                self.assertNotIn("PRIVATE", str(caught.exception))

    def test_one_request_session_budget_makes_no_network_call(self):
        client = CheckedClient()
        conversation = Conversation(client, self.store, max_calls=1)

        with self.assertRaisesRegex(ProviderError, "at least two"):
            conversation.reply(USER_TEXT)

        self.assertEqual(client.calls, [])
        self.assertEqual(conversation.usage.calls, 0)
        self.assertEqual(conversation.history, [])

    def test_zero_or_one_remaining_requests_do_not_start_another_turn(self):
        for maximum in (2, 3):
            with self.subTest(maximum=maximum):
                client = CheckedClient(generated(), reviewed("c1"))
                conversation = Conversation(client, self.store, max_calls=maximum)
                conversation.reply(USER_TEXT)
                history = deepcopy(conversation.history)

                with self.assertRaisesRegex(ProviderError, "at least two"):
                    conversation.reply("明天准备骑车出门")

                self.assertEqual(len(client.calls), 2)
                self.assertEqual(conversation.usage.calls, 2)
                self.assertEqual(conversation.history, history)

    def test_emoji_are_removed_before_review_and_in_visible_reply_and_history(self):
        client = CheckedClient(generated("一起庆祝一下👩🏽‍💻❤️🇨🇳。"), reviewed("c1"))
        conversation = Conversation(client, self.store)

        turn = conversation.reply(USER_TEXT)

        self.assertEqual(turn.text, "一起庆祝一下。")
        self.assertEqual(conversation.history[-1], {"role": "assistant", "content": turn.text})
        review_request = client.calls[1]["messages"][-1]["content"]
        self.assertIn("一起庆祝一下。", review_request)
        for emoji in ("👩", "🏽", "💻", "❤", "🇨", "🇳", "\ufe0f", "\u200d"):
            self.assertNotIn(emoji, review_request)
            self.assertNotIn(emoji, turn.text)


if __name__ == "__main__":
    unittest.main()
