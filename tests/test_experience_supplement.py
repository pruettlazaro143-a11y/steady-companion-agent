"""Execution/history/scope evidence only; synthetic transport is not inference."""
from contextlib import redirect_stderr
import io
import json
import unittest
from unittest.mock import patch

import test_experience_eval as fixtures
from steady_companion import cli, experience_eval as trial, model_access as access
from steady_companion.experience_scenarios import SCENARIO_IDS, SUPPLEMENTAL
from steady_companion.interaction import InteractionState


class SupplementTests(unittest.TestCase):
    setUp = fixtures.ExperienceTests.setUp
    factory = fixtures.ExperienceTests.factory
    run_trial = fixtures.ExperienceTests.run_trial

    def selected(self, name, **kwargs):
        return self.run_trial(extra=['--scenario', name], **kwargs)

    def test_baseline_script_and_default_selection_preserved(self):
        args = cli.parser().parse_args(['eval-experience'])
        self.assertEqual(args.scenario, 'baseline')
        self.assertEqual(trial.material(), [
            {'turn': i, 'purpose': goal, 'user_input': text}
            for i, (goal, text) in enumerate(trial.SCENARIO, 1)])
        self.assertEqual(trial.material(), trial.material('baseline'))

    def test_each_preview_without_credentials_store_or_requests(self):
        for name in SUPPLEMENTAL:
            with self.subTest(scenario=name), patch.object(trial, 'Store', side_effect=AssertionError('no store')):
                code, events, _, wire, connect = self.selected(name, live=False)
                self.assertEqual(code, 0); connect.assert_not_called()
                self.assertFalse(wire.requests)
                plan = events[0]
                self.assertEqual(plan['scenario_id'], name)
                self.assertEqual(plan['scenario'], trial.material(name))
                self.assertEqual((plan['turn_limit'], plan['request_limit']), (6, 12))
                self.assertEqual(plan['cost']['output_token_ceiling'], 31776)
                self.assertEqual(plan['execution'], 'offline_preview')

    def test_unknown_or_batch_selection_refused_before_connect(self):
        for name in ('all', '../private.json', 'nonexistent'):
            with self.subTest(scenario=name), redirect_stderr(io.StringIO()), self.assertRaises(SystemExit):
                cli.parser().parse_args(['eval-experience', '--scenario', name])
        args = cli.parser().parse_args(['eval-experience', '--confirm-live'])
        args.scenario = 'all'; args.output = self.root / 'no-output'
        with patch.object(access, 'connect') as connect, self.assertRaises(trial.StageError):
            trial.evaluate(args)
        connect.assert_not_called(); self.assertFalse(args.output.exists())

    def test_all_scripts_keep_only_actual_history_and_hide_review_rubric(self):
        for name in SUPPLEMENTAL:
            with self.subTest(scenario=name):
                replies = ['新写受控夹具第' + str(i) + '轮。' for i in range(1, 7)]
                if name == 'factual-correction':
                    replies[0] = '受控错误夹具：这次朗读中断是因为你忘词。'
                code, events, _, wire, _ = self.selected(name, wire=fixtures.Wire(replies))
                self.assertEqual(code, 0)
                generation = [body for _, body, _ in wire.requests if 'response_format' not in body]
                for index, body in enumerate(generation):
                    actual = [m['content'] for m in body['messages'] if m['role'] == 'assistant']
                    self.assertEqual(actual, replies[:index])
                    self.assertEqual(body['messages'][-1]['content'], SUPPLEMENTAL[name][index].user_input)
                for _, body, _ in wire.requests:
                    encoded = json.dumps(body, ensure_ascii=False)
                    for turn in SUPPLEMENTAL[name]:
                        for label in (turn.expected_behavior, turn.failure_criteria, turn.applicability):
                            self.assertNotIn(label, encoded)
                turns = [e for e in events if e['event'] == 'turn']
                self.assertEqual([t['delivered_reply'] for t in turns], replies)
                self.assertTrue(all(t['scenario_id'] == name for t in turns))
                summary = events[-1]
                self.assertEqual(summary['status'], 'completed_pending_human_review')
                self.assertEqual(summary['longitudinal_review']['status'], 'pending_human_reading')
                self.assertTrue(all(d['status'] == 'unreviewed' for d in summary['longitudinal_review']['dimensions'].values()))
                self.assertLessEqual(len(wire.requests), 12)
                self.assertEqual(summary['usage']['calls'], len(wire.requests))

    def test_conditional_correction_stops_if_actual_error_absent(self):
        code, events, terminal, wire, _ = self.selected('factual-correction',
            wire=fixtures.Wire(['音箱故障让朗读中断了。']), answers=['n'])
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 1)
        self.assertEqual(events[-1]['status'], 'not_applicable')
        self.assertEqual(events[-1]['not_completed_turns'], [2, 3, 4, 5, 6])
        applicable = next(e for e in events if e['event'] == 'applicability')
        self.assertEqual(applicable['previous_delivered_turn'], 1)
        self.assertIn('确实把中断归因', applicable['criterion'])
        self.assertIn(applicable['criterion'], terminal)
        self.assertEqual(len(self.agents[-1].history), 2)

    def test_operator_free_text_not_stored_or_sent(self):
        secret_answer = 'PRIVATE_OPERATOR_COMMENT'
        code, events, terminal, wire, _ = self.selected('correction', answers=[secret_answer])
        self.assertEqual(code, 1)
        self.assertNotIn(secret_answer, json.dumps(events) + terminal + str(wire.requests))

    def test_feedback_and_elaboration_reach_real_user_history_not_system(self):
        _, _, _, wire, _ = self.selected('correction')
        generation = [body for _, body, _ in wire.requests if 'response_format' not in body]
        corrected = SUPPLEMENTAL['correction'][1].user_input
        elaborated = SUPPLEMENTAL['correction'][3].user_input
        for index, body in enumerate(generation):
            self.assertNotIn(corrected, body['messages'][0]['content'])
            self.assertNotIn(elaborated, body['messages'][0]['content'])
            if index >= 2:
                self.assertIn(corrected, [m['content'] for m in body['messages'] if m['role'] == 'user'])

    def test_new_selections_cannot_expand_twelve_call_budget(self):
        for name in SUPPLEMENTAL:
            with self.subTest(scenario=name):
                replies = ['请列出过往家庭经历。'] * 6
                if name == 'factual-correction':
                    replies[0] += '受控错误夹具：这次中断是因为你忘词。'
                _, events, _, wire, _ = self.selected(name,
                    wire=fixtures.Wire(replies))
                self.assertEqual(len(wire.requests), 12)
                self.assertEqual(events[-1]['usage']['calls'], 12)
                self.assertEqual(events[-1]['retries'], 0)
                self.assertIsNone(events[-1]['amount'])
                self.assertEqual(events[-1]['human_review']['naturalness_and_helpfulness'], 'pending_human_reading')

    def test_pause_context_retains_other_boundaries_in_checker_data(self):
        _, events, _, wire, _ = self.selected('pause-resume')
        checks = [json.loads(body['messages'][-1]['content'])
                  for _, body, _ in wire.requests if 'response_format' in body]
        self.assertEqual(len(checks), 6)
        by_turn = {'I' + str(i): row for i, row in enumerate(checks, 1)}
        for ref in ('I3', 'I4', 'I5'):
            scope = by_turn[ref]['scope']
            targets = {b['target'] for b in scope['boundaries']}
            self.assertIn('personal_history', targets); self.assertIn('analysis', targets)
            self.assertFalse(scope['permissions']['solicit_personal_information'])
        self.assertTrue(by_turn['I3']['scope']['permissions']['task_clarification'])
        self.assertTrue(by_turn['I4']['scope']['permissions']['task_clarification'])
        self.assertTrue(all(e['check']['status'] == 'pass' for e in events if e['event'] == 'turn'))
        # These pass verdicts are transport fixtures, not the expected human judgment.

    @unittest.expectedFailure
    def test_known_gap_named_topic_pause_should_close_matching_text_task(self):
        """Unresolved dev16 recognizer gap, not a suppressed semantic pass."""
        state = InteractionState()
        for turn in SUPPLEMENTAL['pause-resume'][:2]:
            state.receive(turn.user_input)
        self.assertIn('music', state.boundaries)
        self.assertFalse(state.permissions()['task_clarification'])

    def test_blocked_candidate_after_pause_not_displayed_or_committed(self):
        candidate = '先把你过往的家庭经历告诉我。'
        class SecondCheckConflict(fixtures.Wire):
            checks = 0
            def open(self, request, timeout):
                if 'response_format' in json.loads(request.data):
                    self.checks += 1
                    self.verdict = 'pass' if self.checks == 1 else 'conflict'
                return super().open(request, timeout)
        code, events, terminal, wire, _ = self.selected('pause-resume',
            wire=SecondCheckConflict(['受控合成邀请。', candidate]))
        self.assertEqual(code, 1); self.assertEqual(len(wire.requests), 4)
        turn = [e for e in events if e['event'] == 'turn'][-1]
        self.assertEqual(turn['turn'], 2)
        self.assertIsNone(turn['delivered_reply']); self.assertEqual(turn['withheld_candidate'], candidate)
        self.assertNotIn(candidate, terminal); self.assertEqual(len(self.agents[-1].history), 2)
        self.assertNotIn(candidate, str(self.agents[-1].history))
        self.assertEqual(events[-1]['longitudinal_review']['checker_comparison']['status'], 'unreviewed')

    def test_selected_runs_have_separate_cleaned_synthetic_stores(self):
        locations = []
        def make_store(path):
            locations.append(path); return fixtures.Store(path)
        with patch.object(trial, 'Store', side_effect=make_store):
            for name in ('disagreement', 'correction'):
                _, events, _, _, _ = self.selected(name)
                final = next(e for e in events if e['event'] == 'storage_final')
                self.assertEqual([final[k] for k in ('durable_memories', 'shared_notes', 'automatic_memories')], [0, 0, 0])
                self.assertEqual(events[-1]['usage']['memory_calls'], 0)
                self.assertEqual(events[-1]['usage']['summary_calls'], 0)
        self.assertEqual(len(set(locations)), 2)
        self.assertTrue(all(not path.exists() for path in locations))

    def test_unknown_and_cancel_still_stop_supplemental_run(self):
        for wire, status in ((fixtures.Wire(['请列出过往家庭经历。'], verdict='unknown'), 'checker_unknown'),
                             (fixtures.Wire(failure=('any', KeyboardInterrupt())), 'cancelled')):
            with self.subTest(status=status):
                code, events, _, transport, _ = self.selected('correction', wire=wire)
                self.assertEqual(code, 1); self.assertEqual(events[-1]['status'], status)
                self.assertFalse(self.agents[-1].history)
                self.assertLessEqual(len(transport.requests), 2)

    def test_budget_insufficient_before_required_check_no_delivery(self):
        original = self.factory
        def nearly_spent(*args, **kwargs):
            agent = original(*args, **kwargs); agent.usage.calls = 11
            return agent
        with patch.object(self, 'factory', side_effect=nearly_spent):
            code, events, _, wire, _ = self.selected('pause-resume')
        self.assertEqual(code, 1); self.assertEqual(events[-1]['status'], 'budget_exhausted')
        self.assertFalse(wire.requests); self.assertFalse(self.agents[-1].history)
        self.assertEqual(events[-1]['usage']['calls'], 11)

    def test_original_generation_materials_are_still_selected(self):
        _, _, _, wire, _ = self.selected('disagreement', answers=['n'])
        system = wire.requests[0][1]['messages'][0]['content']
        self.assertIn('COMPANION_RUNTIME_V3', system)
        self.assertIn('R-A', system)
        self.assertEqual(self.agents[-1].companion_runtime_version, 'v3')


if __name__ == '__main__':
    unittest.main()
