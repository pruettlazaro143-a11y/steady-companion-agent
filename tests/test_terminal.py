"""Terminal resilience contracts with fake providers; no remote requests."""
from contextlib import redirect_stderr, redirect_stdout
from io import StringIO
import sqlite3
from tempfile import TemporaryDirectory
import threading
import unittest
from unittest.mock import patch

from steady_companion import cli
from steady_companion.engine import Conversation
from steady_companion.provider import Config, ProviderError
from steady_companion.terminal import WaitingStatus


class TTY(StringIO):
    def isatty(self):
        return True


class WaitingStatusTests(unittest.TestCase):
    def test_redirected_output_has_no_status_bytes_or_thread(self):
        output = StringIO()
        with WaitingStatus(output, interval=0.01) as status:
            self.assertIsNone(status._thread)
        self.assertEqual(output.getvalue(), "")

    def test_worker_only_displays_fixed_status_and_is_joined_on_interrupt(self):
        output = TTY()
        with self.assertRaises(KeyboardInterrupt):
            with WaitingStatus(output, interval=0.01) as status:
                self.assertTrue(status._thread.is_alive())
                raise KeyboardInterrupt
        self.assertFalse(status._thread.is_alive())
        self.assertIn("等待回复…", output.getvalue())
        self.assertIn("Ctrl+C取消本轮", output.getvalue())
        self.assertNotIn("\x1b", output.getvalue())
        self.assertNotIn("\n", output.getvalue())

    def test_worker_is_joined_on_regular_error_and_success(self):
        for failure in (None, RuntimeError("fixture-only")):
            with self.subTest(failure=failure):
                status = WaitingStatus(TTY(), interval=0.01)
                try:
                    with status:
                        if failure:
                            raise failure
                except RuntimeError:
                    pass
                self.assertFalse(status._thread.is_alive())

    def test_closed_progress_stream_does_not_abort_request(self):
        output = TTY()
        output.close()
        with WaitingStatus(output) as status:
            self.assertIsNone(status._thread)


class SequenceClient:
    config = Config()

    def __init__(self, outcomes):
        self.outcomes = iter(outcomes)
        self.requests = []
        self.thread_ids = []

    def complete(self, messages, **kwargs):
        self.requests.append(messages)
        self.thread_ids.append(threading.get_ident())
        outcome = next(self.outcomes)
        if isinstance(outcome, BaseException):
            raise outcome
        return {"choices": [{"message": {"role": "assistant", "content": outcome}}]}


class ChatResilienceTests(unittest.TestCase):
    def run_chat(self, outcomes, lines):
        """Use the real engine and store so failure history checks are meaningful."""
        client, agents, statuses = SequenceClient(outcomes), [], []

        def conversation(*args, **kwargs):
            agent = Conversation(*args, **kwargs)
            agents.append(agent)
            return agent

        def waiting():
            status = WaitingStatus(TTY(), interval=0.01)
            statuses.append(status)
            return status

        output = StringIO()
        with TemporaryDirectory() as folder:
            args = cli.parser().parse_args(["--data-dir", folder, "chat", "--response-mode", "direct"])
            with patch.object(cli, "configured_client", return_value=client), \
                 patch.object(cli, "Conversation", side_effect=conversation), \
                 patch.object(cli, "WaitingStatus", side_effect=waiting), \
                 patch("builtins.input", side_effect=lines), redirect_stdout(output):
                result = cli.chat(args)
        return result, output.getvalue(), client, agents, statuses

    def test_request_interrupt_returns_to_prompt_without_failed_turn_history(self):
        result, output, client, agents, statuses = self.run_chat(
            [KeyboardInterrupt(), "接下来的回复"], ["取消这句", "下一句", "/quit"])
        self.assertEqual(result, 0)
        self.assertIn("已取消本轮", output)
        self.assertIn("接下来的回复", output)
        self.assertEqual(len(client.requests), 2)
        self.assertEqual(client.thread_ids, [threading.get_ident()] * 2)
        self.assertEqual(agents[0].history,
                         [{"role": "user", "content": "下一句"}, {"role": "assistant", "content": "接下来的回复"}])
        self.assertTrue(all(not status._thread.is_alive() for status in statuses))

    def test_provider_failure_is_not_retried_and_next_input_can_succeed(self):
        result, output, client, agents, _ = self.run_chat(
            [ProviderError("Fixture timeout; no automatic retry."), "恢复回复"], ["第一句", "第二句", "/quit"])
        self.assertEqual(result, 0)
        self.assertIn("Fixture timeout", output)
        self.assertEqual(len(client.requests), 2)
        self.assertNotIn("第一句", str(agents[0].history))

    def test_local_or_unexpected_error_is_redacted_and_loop_survives(self):
        secret = "PRIVATE_MESSAGE_KEY_AND_PATH"
        for failure in (OSError(secret), sqlite3.OperationalError(secret), RuntimeError(secret)):
            with self.subTest(failure=type(failure).__name__):
                result, output, client, agents, _ = self.run_chat(
                    [failure, "之后还能聊"], ["失败输入", "继续", "/quit"])
                self.assertEqual(result, 0)
                self.assertNotIn(secret, output)
                self.assertNotIn("Traceback", output)
                self.assertIn(type(failure).__name__, output)
                self.assertIn("之后还能聊", output)
                self.assertEqual(len(client.requests), 2)
                self.assertNotIn("失败输入", str(agents[0].history))

    def test_input_interrupt_exits_without_provider_call(self):
        result, output, client, agents, statuses = self.run_chat([], [KeyboardInterrupt()])
        self.assertEqual(result, 0)
        self.assertIn("已结束", output)
        self.assertEqual(client.requests, [])
        self.assertEqual(statuses, [])

    def test_reminder_database_error_does_not_block_input(self):
        with patch.object(cli, "show_due", side_effect=sqlite3.OperationalError("PRIVATE_REMINDER")):
            result, output, client, agents, _ = self.run_chat(["正常回复"], ["你好", "/quit"])
        self.assertEqual(result, 0)
        self.assertIn("正常回复", output)
        self.assertNotIn("PRIVATE_REMINDER", output)
        self.assertEqual(len(client.requests), 1)

    def test_startup_exception_is_redacted_without_traceback(self):
        output = StringIO()
        with patch.object(cli, "configured_client", side_effect=RuntimeError("PRIVATE_KEY")), \
             redirect_stderr(output), redirect_stdout(StringIO()):
            result = cli.main(["chat"])
        self.assertEqual(result, 1)
        self.assertIn("RuntimeError", output.getvalue())
        self.assertNotIn("PRIVATE_KEY", output.getvalue())
        self.assertNotIn("Traceback", output.getvalue())
