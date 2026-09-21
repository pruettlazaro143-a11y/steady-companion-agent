"""Evaluation integration: never contact a provider from tests."""
from contextlib import redirect_stdout
from io import StringIO
import json
from pathlib import Path
from tempfile import TemporaryDirectory
import unittest
from unittest.mock import patch
from steady_companion import cli
from steady_companion.provider import Config


class FakeClient:
    config = Config()

    def __init__(self):
        self.requests = []

    def complete(self, messages, **kwargs):
        self.requests.append(messages)
        return {"choices": [{"message": {"role": "assistant", "content": "Synthetic response."}}]}


class EvaluationTests(unittest.TestCase):
    def test_default_three_conditions_use_checked_pipeline_for_third(self):
        class CheckedClient(FakeClient):
            def complete(self, messages, **kwargs):
                self.requests.append(messages)
                stage = messages[-1]["content"]
                if stage.startswith("OUTPUT PIPELINE: GENERATION"):
                    quote = next(m["content"] for m in reversed(messages) if m["role"] == "user")[:30]
                    candidates = [{"id": ident, "action": "acknowledge", "length": "short",
                                   "question_limit": 0, "evidence": [{"ref": "U0", "quote": quote}],
                                   "reply": "Synthetic candidate " + ident} for ident in ("c1", "c2")]
                    content = json.dumps({"candidates": candidates, "selected_id": "c1"})
                elif stage.startswith("OUTPUT PIPELINE: REVIEW"):
                    content = json.dumps({"approved_ids": ["c2"], "issues": [], "repair_hint": ""})
                else:
                    content = "Synthetic direct response."
                return {"choices": [{"message": {"content": content}}]}

        with TemporaryDirectory() as folder:
            output = Path(folder) / "three.jsonl"
            args = cli.parser().parse_args(["eval", "--confirm-live", "--limit", "1", "--output", str(output)])
            client = CheckedClient()
            with patch.object(cli, "configured_client", return_value=client), redirect_stdout(StringIO()):
                self.assertEqual(cli.evaluate(args), 0)
            rows = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual([row["condition"] for row in rows], ["baseline", "skill", "checked"])
            self.assertEqual([row["usage"]["calls"] for row in rows], [1, 1, 2])
            self.assertEqual(rows[2]["text"], "Synthetic candidate c2")
            self.assertEqual(rows[2]["inspection"]["stages"], ["generation", "review"])
            self.assertNotIn("reply", json.dumps(rows[2]["inspection"]))

    def test_inspect_does_not_prompt_or_call_provider_or_reveal_key(self):
        output = StringIO()
        secret = "test-key-do-not-display"
        with patch.object(cli.Config, "from_env", return_value=Config(api_key=secret)), \
             patch.object(cli, "configured_client") as configure, redirect_stdout(output):
            self.assertEqual(cli.main(["inspect"]), 0)
        configure.assert_not_called()
        self.assertNotIn(secret, output.getvalue())
        status = json.loads(output.getvalue())
        self.assertTrue(status["key_configured"])
        self.assertEqual(status["response_mode"], "checked")
        self.assertEqual(status["emoji_mode"], "off")
        self.assertTrue(Path(status["package_path"]).is_absolute())

    def test_both_conditions_receive_identical_memory_snapshot_and_messages(self):
        with TemporaryDirectory() as folder:
            output = Path(folder) / "out.jsonl"
            args = cli.parser().parse_args(["eval", "--confirm-live", "--limit", "12", "--conditions", "baseline", "skill", "--output", str(output)])
            client = FakeClient()
            with patch.object(cli, "configured_client", return_value=client), redirect_stdout(StringIO()):
                self.assertEqual(cli.evaluate(args), 0)
            records = [json.loads(line) for line in output.read_text().splitlines()]
            self.assertEqual(len(records), 24)
            self.assertEqual(len(client.requests), 24)
            for i in range(0, 24, 2):
                self.assertEqual(records[i]["condition"], "baseline")
                self.assertEqual(records[i + 1]["condition"], "skill")
                self.assertEqual(records[i]["memory_ids"], records[i + 1]["memory_ids"])
                self.assertEqual(client.requests[i][1:], client.requests[i + 1][1:])

    def test_malformed_cases_fail_before_credentials_or_network(self):
        with TemporaryDirectory() as folder:
            path = Path(folder) / "bad.jsonl"
            args = cli.parser().parse_args(["eval", "--confirm-live", "--cases", str(path)])
            for case in ({"messages": ["bad"]},
                         {"messages": [{"role": "user", "content": "hi"}], "memories": [{}]}):
                with self.subTest(case=case):
                    path.write_text(json.dumps(case))
                    with patch.object(cli, "configured_client") as configure:
                        with self.assertRaises(ValueError):
                            cli.evaluate(args)
                        configure.assert_not_called()
