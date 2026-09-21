"""Consent, provenance, revision and replay contracts for shared-context learning."""
from contextlib import redirect_stdout
from copy import deepcopy
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest

from steady_companion.cli import execute_command
from steady_companion.engine import Conversation, ContextChangedError, select_companion_notes, MAX_COMPANION_CHARS
from steady_companion.learning import parse_proposals
from steady_companion.provider import ProviderError
from steady_companion.store import Store


USER = "以后聊游戏可以轻松一点，谈正事别拿我开玩笑。"


def response(payload):
    return {"choices": [{"message": {"role": "assistant", "content": json.dumps(payload, ensure_ascii=False)}}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 10, "total_tokens": 20}}


def proposed():
    return {"proposals": [
        {"kind": "style", "text": "聊游戏时可以轻松一些。", "scope": "聊游戏时",
         "evidence": {"ref": "U0", "quote": "聊游戏可以轻松一点"}},
        {"kind": "style", "text": "谈正事不要拿用户开玩笑。", "scope": "认真谈事情时",
         "evidence": {"ref": "U0", "quote": "谈正事别拿我开玩笑"}},
    ]}


class Client:
    def __init__(self, *responses):
        self.responses, self.calls = list(responses), []

    def complete(self, messages, **kwargs):
        self.calls.append(deepcopy((messages, kwargs)))
        value = self.responses.pop(0)
        return value() if callable(value) else deepcopy(value)


class LearningTests(unittest.TestCase):
    def setUp(self):
        self.folder = TemporaryDirectory()
        self.addCleanup(self.folder.cleanup)
        self.store = Store(Path(self.folder.name))

    def conversation(self, client):
        conversation = Conversation(client, self.store, response_mode="direct")
        conversation.history = [{"role": "user", "content": USER},
                                {"role": "assistant", "content": "好，谈正事就认真说。"}]
        return conversation

    def test_proposal_is_volatile_until_individual_acceptance_then_replayed_after_restart(self):
        client = Client(response(proposed()))
        conversation = self.conversation(client)
        prior = deepcopy(conversation.history)
        revision = self.store.revision()
        proposals = conversation.learn()
        self.assertEqual(len(proposals), 2)
        self.assertEqual(self.store.list_notes(), [])
        self.assertEqual(self.store.revision(), revision)
        self.assertEqual(conversation.history, prior)
        self.assertEqual(conversation.usage.calls, 1)
        self.assertIsNone(client.calls[0][1]["tools"])
        self.assertIn("Current wishes override old preferences", client.calls[0][0][0]["content"])
        first = conversation.keep_learning(proposals[0]["id"])
        self.assertEqual(len(self.store.list_notes()), 1)
        self.assertEqual(first["source"], "approved_learning")
        self.assertEqual(first["evidence"], proposed()["proposals"][0]["evidence"]["quote"])
        self.assertEqual(conversation.history, prior)
        self.assertEqual(len(conversation.pending_learning), 1)
        self.assertTrue(conversation.drop_learning(proposals[1]["id"]))
        self.assertEqual(len(client.calls), 1)
        new_client = Client({"choices": [{"message": {"content": "聊游戏。"}}]})
        restarted = Conversation(new_client, Store(self.store.data_dir), response_mode="direct")
        turn = restarted.reply("今天聊聊游戏")
        self.assertEqual(turn.companion_note_ids, [first["id"]])
        self.assertIn(first["text"], json.dumps(new_client.calls[0][0], ensure_ascii=False))
        self.assertNotIn(proposed()["proposals"][1]["text"], json.dumps(new_client.calls[0][0], ensure_ascii=False))

    def test_new_user_turn_and_new_session_invalidate_pending_suggestions(self):
        for change in ("reply", "clear"):
            with self.subTest(change=change):
                client = Client(response(proposed()), {"choices": [{"message": {"content": "知道了。"}}]})
                conversation = self.conversation(client)
                identifier = conversation.learn()[0]["id"]
                if change == "reply":
                    conversation.reply("刚才那个偏好先别记了。")
                else:
                    conversation.clear()
                with self.assertRaises(ValueError):
                    conversation.keep_learning(identifier)
                self.assertEqual(self.store.list_notes(), [])

    def test_external_mutation_blocks_proposals_during_request_and_at_acceptance(self):
        other = Store(self.store.data_dir)

        def mutate():
            other.remember("新边界", "boundary")
            return response(proposed())

        conversation = self.conversation(Client(mutate))
        with self.assertRaises(ContextChangedError):
            conversation.learn()
        self.assertEqual(conversation.pending_learning, [])
        conversation = self.conversation(Client(response(proposed())))
        identifier = conversation.learn()[0]["id"]
        other.remember_note("其他话题", "thread")
        with self.assertRaises(ValueError):
            conversation.keep_learning(identifier)
        self.assertEqual([n["text"] for n in self.store.list_notes()], ["其他话题"])

    def test_no_history_or_exhausted_budget_never_calls_provider(self):
        client = Client()
        empty = Conversation(client, self.store)
        with self.assertRaises(ValueError):
            empty.learn()
        conversation = self.conversation(client)
        conversation.usage.calls = conversation.max_calls
        with self.assertRaises(ProviderError):
            conversation.learn()
        self.assertEqual(client.calls, [])

    def test_fabricated_quote_and_hidden_fields_fail_without_saving_or_exposing_payload(self):
        variants = []
        invalid = proposed()
        invalid["proposals"][0]["evidence"]["quote"] = "PRIVATE_INVENTED_SOURCE"
        variants.append(invalid)
        invalid = proposed()
        invalid["proposals"][0]["diagnosis"] = "PRIVATE_HIDDEN_LABEL"
        variants.append(invalid)
        invalid = proposed()
        invalid["proposals"][0]["scope"] = "<think>PRIVATE_ANALYSIS</think>"
        variants.append(invalid)
        for payload in variants:
            with self.subTest(payload=payload):
                conversation = self.conversation(Client(response(payload)))
                with self.assertRaises(ProviderError) as caught:
                    conversation.learn()
                self.assertNotIn("PRIVATE", str(caught.exception))
                self.assertEqual(self.store.list_notes(), [])
                self.assertEqual(conversation.pending_learning, [])

    def test_empty_proposals_and_duplicate_existing_notes_need_no_confirmation(self):
        conversation = self.conversation(Client(response({"proposals": []})))
        self.assertEqual(conversation.learn(), [])
        for item in proposed()["proposals"]:
            self.store.remember_note(item["text"], item["kind"], scope=item["scope"])
        conversation = self.conversation(Client(response(proposed())))
        self.assertEqual(conversation.learn(), [])

    def test_different_learning_runs_do_not_reuse_ids(self):
        conversation = self.conversation(Client(response(proposed()), response(proposed())))
        old = conversation.learn()[0]["id"]
        new = conversation.learn()[0]["id"]
        self.assertNotEqual(old, new)
        with self.assertRaises(ValueError):
            conversation.keep_learning(old)

    def test_context_selection_is_bounded_and_whole_with_one_optional_open_thread(self):
        for i in range(9):
            self.store.remember_note("不要讲笑话" * 120, "style", scope="认真讨论" + str(i))
        old = self.store.remember_note("纸箱城堡", "thread")
        new = self.store.remember_note("明天讲猫", "thread")
        notes = self.store.list_notes()
        chosen = select_companion_notes(notes, "你好")
        self.assertLessEqual(len(chosen), 6)
        self.assertLessEqual(len(json.dumps(chosen, ensure_ascii=False)), MAX_COMPANION_CHARS)
        for item in chosen:
            self.assertEqual(item["text"], next(n["text"] for n in notes if n["id"] == item["id"]))
        threads_only = select_companion_notes([old, new], "你好")
        self.assertEqual([n["id"] for n in threads_only], [new["id"]])

    def test_cli_manual_note_learning_acceptance_correction_and_deletion(self):
        conversation = self.conversation(Client(response(proposed())))
        initial = deepcopy(conversation.history)
        output = StringIO()
        with redirect_stdout(output):
            execute_command("/note thread 下次继续纸箱城堡", conversation, self.store)
            self.assertEqual(conversation.history, initial)
            execute_command("/learn", conversation, self.store)
            execute_command("/keep L1", conversation, self.store)
            execute_command("/notes", conversation, self.store)
            execute_command("/revise N1 这个故事已经讲完", conversation, self.store)
            self.assertEqual(conversation.history, [])
            execute_command("/unlearn N1", conversation, self.store)
        self.assertIn("你的原话", output.getvalue())
        self.assertIn("适用范围", output.getvalue())
        self.assertEqual([n["kind"] for n in self.store.list_notes()], ["style"])
        self.assertEqual(len(conversation.client.calls), 1)

    def test_corrected_note_is_not_replayed_from_stale_history(self):
        note = self.store.remember_note("OLD_PRIVATE", "style")
        client = Client({"choices": [{"message": {"content": "好的。"}}]})
        conversation = self.conversation(client)
        conversation.history.append({"role": "user", "content": "OLD_PRIVATE"})
        Store(self.store.data_dir).correct_note(note["id"], "现在简短一点")
        turn = conversation.reply("你好")
        self.assertTrue(turn.context_reset)
        self.assertNotIn("OLD_PRIVATE", json.dumps(client.calls))
        self.assertIn("现在简短一点", json.dumps(client.calls, ensure_ascii=False))

    def test_explicit_budget_increase_preserves_history_and_makes_no_request(self):
        conversation = self.conversation(Client())
        conversation.usage.calls = conversation.max_calls
        previous = deepcopy(conversation.history)
        with redirect_stdout(StringIO()):
            execute_command("/budget 80", conversation, self.store)
        self.assertEqual(conversation.max_calls, 80)
        self.assertEqual(conversation.history, previous)
        self.assertEqual(conversation.client.calls, [])
        with self.assertRaises(ValueError):
            execute_command("/budget 5", conversation, self.store)


if __name__ == "__main__":
    unittest.main()
