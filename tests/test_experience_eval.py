"""Authored synthetic replies verify execution, not companionship or verdict accuracy."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io
import json
from pathlib import Path
import tempfile
import unittest
import urllib.error
from unittest.mock import patch

from steady_companion import cli, experience_eval as trial, model_access as access
from steady_companion.chat_transport import ChatCompletionsClient
from steady_companion.interaction_runtime import InteractionConversation
from steady_companion.store import Store

ROOT = Path(__file__).resolve().parents[1]
CONFIG = ROOT / 'configs/deepseek-official.json'
KEY = 'SYNTHETIC_SECRET_DO_NOT_CAPTURE'


class Wire:
    def __init__(self, replies=None, verdict='pass', failure=None, usage=True):
        self.requests = []
        self.replies = iter(replies or ['受控合成回复，仅供程序验证。'] * 6)
        self.verdict, self.failure, self.usage = verdict, failure, usage

    def open(self, request, timeout):
        assert request.get_method() == 'POST'
        body = json.loads(request.data)
        self.requests.append((request.full_url, body, timeout))
        structured = 'response_format' in body
        if self.failure and (self.failure[0] == 'any' or structured):
            raise self.failure[1]
        if structured:
            data = json.loads(body['messages'][-1]['content'])
            content = json.dumps({'verdict': self.verdict,
                                 'codes': ['none'] if self.verdict == 'pass' else ['uncertain'] if self.verdict == 'unknown' else ['uninvited_assessment'],
                                 'message_ids': [next(iter(data['sources']))]})
        else:
            content = next(self.replies)
        payload = {'model': 'deepseek-flash', 'choices': [{'message': {'role': 'assistant', 'content': content,
                    'reasoning_content': 'HIDDEN_REASONING_NEVER_CAPTURE'}, 'finish_reason': 'stop'}]}
        if self.usage:
            payload['usage'] = {'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14}
        class Reply(io.BytesIO):
            status = 200
            def geturl(self): return request.full_url
        return Reply(json.dumps(payload).encode())


class ExperienceTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name)
        self.plan = access.resolve('daily-deepseek', CONFIG)
        self.agents = []

    def factory(self, *args, **kwargs):
        agent = InteractionConversation(*args, **kwargs)
        self.agents.append(agent)
        return agent

    def run_trial(self, wire=None, live=True, answers=None, output=None, extra=None):
        wire = wire or Wire()
        client = access.AccessClient(self.plan, ChatCompletionsClient(replace(self.plan.connection, api_key=KEY), wire,
                                    policy=self.plan.transport_policy))
        output = output or self.root / ('report' + str(len(list(self.root.iterdir()))))
        args = cli.parser().parse_args(['eval-experience', '--connection', str(CONFIG), '--output', str(output),
                                       *(['--confirm-live'] if live else []), *(extra or [])])
        text = io.StringIO()
        with patch.object(access, 'connect', return_value=client) as connect, patch.object(trial, 'InteractionConversation', side_effect=self.factory):
            code = trial.evaluate(args, ask=unittest.mock.Mock(side_effect=answers if answers is not None else ['y'] * 5),
                                  say=lambda line: print(line, file=text))
        events = [json.loads(line) for line in (output / 'events.jsonl').read_text().splitlines()]
        return code, events, text.getvalue(), wire, connect

    def test_preview_never_connects_or_opens_store(self):
        with patch.object(trial, 'Store', side_effect=AssertionError('no store')):
            code, events, _, wire, connect = self.run_trial(live=False)
        self.assertEqual(code, 0); connect.assert_not_called(); self.assertFalse(wire.requests)
        self.assertEqual(events[0]['execution'], 'offline_preview')
        self.assertEqual(len(events[0]['scenario']), 6)

    def test_existing_output_refused_before_credentials(self):
        output = self.root / 'existing'; output.mkdir(); (output / 'keep').write_text('unchanged')
        with patch.object(access, 'connect') as connect:
            with self.assertRaises(FileExistsError): self.run_trial(output=output)
            connect.assert_not_called()
        self.assertEqual((output / 'keep').read_text(), 'unchanged')

    def test_invalid_config_before_output_or_credential(self):
        config = self.root / 'invalid.json'
        spec = json.loads(CONFIG.read_text()); spec['model'] = 'different-route'; config.write_text(json.dumps(spec))
        args = cli.parser().parse_args(['eval-experience', '--connection', str(config), '--output', str(self.root/'no'), '--confirm-live'])
        with patch.object(access, 'connect') as connect:
            with self.assertRaises(Exception): trial.evaluate(args)
            connect.assert_not_called()
        self.assertFalse((self.root/'no').exists())

    def test_caps_and_modes_cannot_be_expanded_from_cli(self):
        for extra in (['--max-calls', '20'], ['--interaction-check', 'off'], ['--interaction-control', 'off']):
            with redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args(['eval-experience', *extra])

    def test_six_actual_turns_one_context_and_exact_usage(self):
        replies = ['受控合成回复第' + str(i) + '轮。' for i in range(1, 7)]
        code, events, _, wire, _ = self.run_trial(Wire(replies))
        self.assertEqual(code, 0)
        turns = [e for e in events if e['event'] == 'turn']
        self.assertEqual([t['delivered_reply'] for t in turns], replies)
        generation = [body for _, body, _ in wire.requests if 'response_format' not in body]
        for index, body in enumerate(generation):
            actual = [m for m in body['messages'] if m['role'] == 'assistant']
            self.assertEqual([m['content'] for m in actual], replies[:index])
            self.assertNotIn('purpose', str(body['messages']))
        summary = events[-1]
        self.assertEqual(summary['completed_user_turns'], 6)
        self.assertEqual(summary['status'], 'completed_pending_human_review')
        self.assertEqual(summary['usage']['calls'], len(wire.requests))
        self.assertEqual(summary['usage']['total_tokens'], len(wire.requests) * 14)
        self.assertEqual(summary['usage']['memory_calls'], 0); self.assertEqual(summary['usage']['summary_calls'], 0)
        self.assertIsNone(summary['amount'])
        self.assertEqual(turns[0]['check']['status'], 'not_triggered')
        self.assertFalse(turns[0]['check_triggered'])
        self.assertEqual(turns[-1]['check']['status'], 'pass')

    def test_generation_and_check_reach_twelve_without_recovery(self):
        code, events, _, wire, _ = self.run_trial(Wire(['请把过往家庭经历告诉我。'] * 6))
        self.assertEqual(code, 0); self.assertEqual(len(wire.requests), 12)
        self.assertEqual(events[-1]['usage']['calls'], 12)
        self.assertEqual(events[-1]['usage']['total_tokens'], 168)
        for row in [e for e in events if e['event'] == 'turn']:
            for stage in ('generation', 'interaction_check'):
                self.assertEqual(row['stages'][stage]['requests'], 1)
                self.assertGreaterEqual(row['stages'][stage]['elapsed_seconds'], 0)
                self.assertEqual(row['stages'][stage]['reported_usage'], [{'prompt_tokens': 10, 'completion_tokens': 4, 'total_tokens': 14}])

    def test_actual_official_transport_parameters_and_aux_limit(self):
        _, _, _, wire, _ = self.run_trial(Wire(['请把过往家庭经历告诉我。'] * 6))
        for url, body, timeout in wire.requests:
            self.assertEqual(url, 'https://api.deepseek.com/chat/completions')
            self.assertEqual(body['model'], 'deepseek-flash'); self.assertEqual(body['thinking'], {'type': 'disabled'})
            for key in ('store', 'temperature', 'top_p', 'tools', 'tool_choice'): self.assertNotIn(key, body)
            if 'response_format' in body:
                self.assertEqual(body['response_format'], {'type': 'json_object'})
                self.assertEqual((body['max_tokens'], timeout), (1200, 20))
            else: self.assertEqual((body['max_tokens'], timeout), (4096, 90))

    def test_not_applicable_stops_no_replacement_history(self):
        code, events, _, wire, _ = self.run_trial(answers=['cannot fit'])
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 1)
        self.assertEqual(events[-1]['status'], 'not_applicable'); self.assertEqual(len(self.agents[-1].history), 2)
        self.assertNotIn('cannot fit', json.dumps(events))

    def test_uncertain_confirmation_and_eof_stop(self):
        for answer in ('', EOFError()):
            with self.subTest(answer=answer):
                code, events, _, wire, _ = self.run_trial(answers=[answer])
                self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 1)
                self.assertEqual(events[-1]['status'], 'not_applicable')

    def test_confirmation_cancel_is_distinct_from_inapplicability(self):
        code, events, _, wire, _ = self.run_trial(answers=[KeyboardInterrupt()])
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 1)
        self.assertEqual(events[-1]['status'], 'cancelled')

    def test_conflict_withheld_in_report_not_terminal_or_history(self):
        candidate = '请把过往家庭经历告诉我。'
        code, events, terminal, wire, _ = self.run_trial(Wire([candidate], 'conflict'))
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 2)
        row = next(e for e in events if e['event'] == 'turn')
        self.assertEqual(row['withheld_candidate'], candidate); self.assertIsNone(row['delivered_reply'])
        self.assertNotIn(candidate, terminal); self.assertFalse(self.agents[-1].history)
        self.assertEqual(row['check']['status'], 'conflict')
        self.assertEqual(row['human_review']['generation_content_problem'], 'not_assessed_by_human')
        self.assertEqual(row['human_review']['possible_checker_false_positive'], 'needs_human_review')

    def test_unknown_is_retained_and_not_pass(self):
        code, events, _, wire, _ = self.run_trial(Wire(['请把过往家庭经历告诉我。'], 'unknown'))
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 2)
        self.assertEqual(events[-1]['status'], 'checker_unknown'); self.assertFalse(self.agents[-1].history)

    def test_invalid_checker_protocol_is_technical_failure(self):
        code, events, _, wire, _ = self.run_trial(Wire(['请把过往家庭经历告诉我。'], 'invalid'))
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 2)
        self.assertEqual(events[-1]['status'], 'technical_failure')
        self.assertEqual(events[-1]['error']['code'], 'interaction_validation')

    def test_timeout_network_and_cancel_no_retry(self):
        for failure, expected in ((TimeoutError('PRIVATE_ERROR'), 'timeout'), (OSError('PRIVATE_ERROR'), 'transport'), (KeyboardInterrupt('PRIVATE_ERROR'), 'cancelled')):
            with self.subTest(code=expected):
                code, events, _, wire, _ = self.run_trial(Wire(failure=('any', failure)))
                self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 1)
                self.assertEqual(events[-1]['error']['code'], expected)
                self.assertEqual(events[-1]['usage']['unreported_calls'], 1)
                self.assertNotIn('PRIVATE_ERROR', json.dumps(events)); self.assertFalse(self.agents[-1].history)

    def test_check_transport_failure_keeps_generation_usage(self):
        code, events, _, wire, _ = self.run_trial(Wire(['请把过往家庭经历告诉我。'], failure=('check', TimeoutError())))
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 2)
        self.assertEqual(events[-1]['usage']['calls'], 2); self.assertEqual(events[-1]['usage']['total_tokens'], 14)
        self.assertEqual(events[-1]['usage']['unreported_calls'], 1)
        self.assertFalse(self.agents[-1].history)

    def test_http_failure_is_counted_without_retry_or_error_body(self):
        error = urllib.error.HTTPError('https://api.deepseek.com/chat/completions', 429, 'PRIVATE_ERROR', {}, io.BytesIO(b'PRIVATE_BODY'))
        code, events, terminal, wire, _ = self.run_trial(Wire(failure=('any', error)))
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 1)
        self.assertEqual(events[-1]['error']['code'], 'http')
        self.assertEqual(events[-1]['error']['http_status'], 429)
        self.assertEqual(events[-1]['usage']['unreported_calls'], 1)
        self.assertNotIn('PRIVATE', json.dumps(events) + terminal)

    def test_empty_reply_preserves_returned_usage_without_recovery(self):
        code, events, _, wire, _ = self.run_trial(Wire(['']))
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 1)
        self.assertEqual(events[-1]['error']['code'], 'visible_text')
        self.assertEqual(events[-1]['usage']['total_tokens'], 14)
        self.assertEqual(events[-1]['usage']['unreported_calls'], 0)
        self.assertFalse(self.agents[-1].history)

    def test_missing_usage_unknown_not_free(self):
        code, events, _, wire, _ = self.run_trial(Wire(usage=False), answers=['n'])
        self.assertEqual(code, 1); self.assertEqual(events[-1]['usage']['unreported_calls'], 1)
        request = next(e for e in events if e['event'] == 'request')
        self.assertIsNone(request['service_usage']); self.assertIsNone(request['amount'])

    def test_hidden_text_credentials_and_unsafe_visible_content_omitted(self):
        for candidate in ('<analysis>HIDDEN_SECRET</analysis>hello', '这是凭据 ' + KEY):
            code, events, terminal, wire, _ = self.run_trial(Wire([candidate]))
            self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 1)
            rendered = json.dumps(events) + terminal
            for secret in (KEY, 'HIDDEN_SECRET', 'HIDDEN_REASONING_NEVER_CAPTURE'): self.assertNotIn(secret, rendered)
            self.assertFalse(self.agents[-1].history)

    def test_budget_exhaustion_preserved_no_unchecked_delivery(self):
        original = self.factory
        def near_limit(*args, **kwargs):
            agent = original(*args, **kwargs); agent.usage.calls = 11
            return agent
        with patch.object(self, 'factory', side_effect=near_limit):
            code, events, _, wire, _ = self.run_trial(Wire(['请把过往家庭经历告诉我。']))
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 1)
        self.assertEqual(events[-1]['status'], 'budget_exhausted')
        self.assertEqual(events[-1]['usage']['calls'], 12); self.assertFalse(self.agents[-1].history)

    def test_new_temporary_store_no_user_memory_access_or_writes(self):
        locations = []
        def make_store(path):
            locations.append(path); return Store(path)
        with patch.object(trial, 'Store', side_effect=make_store):
            _, events, _, _, _ = self.run_trial()
        self.assertTrue(locations); self.assertTrue(all(not p.exists() for p in locations))
        final = next(e for e in events if e['event'] == 'storage_final')
        self.assertEqual([final[k] for k in ('durable_memories', 'shared_notes', 'automatic_memories')], [0, 0, 0])
        self.assertEqual(final['memory_mode'], 'manual')

    def test_logging_failure_stops_before_next_paid_request(self):
        emit = trial.Report.emit
        def break_request(report, event):
            if event['event'] == 'request': raise trial.StageError('logging', 'logging_write')
            return emit(report, event)
        with patch.object(trial.Report, 'emit', break_request):
            code, events, terminal, wire, _ = self.run_trial()
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 1)
        self.assertEqual(events[-1]['usage']['total_tokens'], 14)
        self.assertEqual(events[-1]['error'], {'stage': 'logging', 'code': 'logging_write'})
        self.assertFalse(self.agents[-1].history)
        self.assertIn('logging_write', terminal)


if __name__ == '__main__':
    unittest.main()
