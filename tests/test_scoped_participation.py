"""Fresh synthetic counterexamples; no real operator/model transcript fixtures.

Assertions concern source binding, control, real storage and compilation, never
whether a mocked sentence demonstrates friendship or psychological benefit.
"""
import json
from copy import deepcopy
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from steady_companion import model_access
from steady_companion.companion_runtime import validate_materials, validate_baseline, examples_text
from steady_companion.interaction import InteractionState, interpret, move
from steady_companion.source_relations import relations
from steady_companion.provider import ProviderError
import test_interaction_scope_v2 as fixtures
verdict = fixtures.verdict


class ScopeTests(unittest.TestCase):
    def state(self, *texts):
        state = InteractionState()
        for text in texts: state.receive(text)
        return state

    def test_noun_independent_text_operations_have_exact_source_spans(self):
        for text in ('帮我写一封天文社的邀请函', '请帮我起草一段陶艺展解说', '现在帮我翻译这封船票改期邮件',
                     '替我润色一段植物标本的说明', '把展柜标签改成三行', '这封迎新便笺帮我润色一下',
                     '帮我把“慢慢尝尝”改成正式一点的措辞'):
            with self.subTest(text=text):
                s = self.state(text)
                self.assertEqual(s.activity, 'task'); self.assertEqual(s.domain, 'unknown')
                self.assertTrue(s.permissions()['task_clarification'])
                self.assertFalse(s.permissions()['solicit_personal_information'])
                obj = s.task_object
                self.assertEqual(s.sources[obj['source']]['content'][slice(*obj['span'])], text)
                self.assertNotIn(text, s.instruction('R-A'))

    def test_rewrite_pronoun_retains_object_source_and_current_evidence(self):
        s = self.state('帮我写一段展览告示'); obj = deepcopy(s.task_object)
        s.receive('把它改成三行就好')
        self.assertEqual(s.task_object['id'], obj['id'])
        self.assertEqual(s.task_object['source'], 'I1')
        self.assertEqual(s.scope_evidence, ['I1', 'I2'])

    def test_personal_history_limit_and_text_task_coexist_both_orders(self):
        for text in ('别追问我以前的经历了，把告示改成三行', '把告示改成三行，并且别追问我过往的经历',
                     '我的过去就别再问了，帮我写一份报名须知'):
            with self.subTest(text=text):
                s = self.state(text)
                self.assertIn('personal_history', s.boundaries)
                self.assertEqual(s.activity, 'task')
                self.assertTrue(s.permissions()['task_clarification'])
                self.assertFalse(s.permissions()['solicit_personal_information'])
                self.assertEqual(s.boundaries['personal_history']['source'], 'I1')

    def test_compound_pauses_keep_topic_and_analysis_independently(self):
        for pause in ('先不谈这个，也先不分析了', '也先不分析了；先不聊这个', '先不聊这个，并且不要分析'):
            s = self.state('帮我写一段邀请函'); thread = s.thread_id
            s.receive(pause)
            self.assertIn(thread, s.boundaries); self.assertIn('analysis', s.boundaries)
            self.assertFalse(s.permissions()['task_clarification'])
            self.assertEqual({b['source'] for b in s.boundaries.values()}, {'I2'})

    def test_new_named_pause_uses_source_not_domain_dictionary(self):
        s = self.state('帮我写一份昆虫展的邀请函'); thread = s.thread_id
        s.receive('先不谈昆虫展了，也先不分析')
        self.assertIn(thread, s.boundaries); self.assertIn('analysis', s.boundaries)
        self.assertEqual(s.boundaries[thread]['object_ref']['binding'], 'current_task_literal')
        other = self.state('先不聊航海旗语了')
        boundary = next(iter(other.boundaries.values()))
        self.assertEqual(boundary['object_ref']['binding'], 'unresolved_named_object')
        self.assertEqual(boundary['source'], 'I1')
        self.assertTrue(other.needs_check('合成候选'))

    def test_analysis_pause_does_not_cancel_independent_writing(self):
        for text in ('帮我写个通知，也先不分析了', '先不分析了，帮我写个通知', '帮我写个通知，不用分析'):
            s = self.state(text)
            self.assertIn('analysis', s.boundaries)
            self.assertEqual(s.activity, 'task'); self.assertTrue(s.permissions()['task_clarification'])

    def test_later_pause_or_withdrawal_cannot_leave_new_invitation_active(self):
        for text in ('帮我写一段说明，算了', '帮我写一封信，先不谈这个了',
                     '帮我起草邀请函就算了，我只是想吐槽', '不用帮我写便条了', '别帮我写便条了'):
            with self.subTest(text=text):
                s = self.state('先不分析了', text)
                self.assertFalse(s.permissions()['task_clarification'])
                self.assertIn('analysis', s.boundaries)

    def test_report_quotes_condition_and_negated_invitation_do_not_open(self):
        for text in ('她说帮我写一段展览说明', '“帮我写一段说明”是台词', '如果有空，帮我写一段说明',
                     '我不是让你帮我写说明', '不要帮我写说明', '难道要帮我写说明', '我并不是说可以问我的过去了'):
            s = self.state('别追问我过去的经历', text)
            self.assertFalse(s.permissions()['task_clarification'])
            self.assertIn('personal_history', s.boundaries)

    def test_positive_reopen_does_not_lift_other_boundaries(self):
        s = self.state('帮我写一份告示', '先不谈这个，也先不分析', '别追问我过去的经历')
        s.receive('现在帮我写一份新告示')
        self.assertTrue(s.permissions()['task_clarification'])
        self.assertIn('analysis', s.boundaries); self.assertIn('personal_history', s.boundaries)
        s.receive('现在可以问我过去的经历了')
        self.assertNotIn('personal_history', s.boundaries)
        self.assertIn('analysis', s.boundaries)
        # Removing one restriction alone is not a personal-analysis invitation.
        self.assertFalse(s.permissions()['analysis'])

    def test_later_withdrawal_cancels_boundary_reopen(self):
        s = self.state('别追问我过去的经历', '现在可以问我过去的经历了，算了')
        self.assertIn('personal_history', s.boundaries)

    def test_unknown_not_a_claim_of_natural_language_understanding(self):
        for text in ('这事你看着弄', '帮我写', '帮我写“未闭合', 'x' * 1201):
            s = self.state(text)
            self.assertIn(s.interpretation_status, ('unknown', 'ambiguous_quote', 'input_limit_unknown', 'partly_unknown'))
            self.assertFalse(s.permissions()['analysis']); self.assertFalse(s.permissions()['task_clarification'])

    def test_presuppositions_can_trigger_check_without_question_mark(self):
        s = self.state('帮我写一张活动便签')
        for candidate in ('你通常为什么会忘带材料', '为什么你每次都提前退场', '你最容易漏掉哪个细节', '准备活动时最容易忘的是哪个环节'):
            self.assertEqual(move(candidate), 'assessment_advance')
            self.assertTrue(s.needs_check(candidate))
        self.assertFalse(s.needs_check('便签是给谁看的？'))

    def test_quoted_control_inside_requested_text_is_data(self):
        s = self.state('请帮我把“别问我的过去”改成更礼貌的措辞')
        self.assertEqual(s.activity, 'task')
        self.assertNotIn('personal_history', s.boundaries)


class RuntimeTests(unittest.TestCase):
    # Reuse only fixture setup; do not import/recollect the old test class.
    setUp = fixtures.DeliveryTests.setUp
    make = fixtures.DeliveryTests.make


class RuntimeBehaviorTests(RuntimeTests):
    def test_legitimate_new_object_clarification_delivers(self):
        agent, wire = self.make(['这份通知是给新成员还是全体成员？'])
        result = agent.reply('帮我写一份天文社的通知')
        self.assertEqual(result.text, agent.history[-1]['content'])
        self.assertEqual(len(wire.requests), 1)
        self.assertEqual(agent.interaction.activity, 'task')
        data = next(m for m in wire.requests[0]['messages'] if m.get('name') == 'interaction_object_data')
        obj = json.loads(data['content'].split('\n', 1)[1])
        self.assertEqual(obj['source_quote'], '帮我写一份天文社的通知')
        self.assertNotIn(obj['source_quote'], wire.requests[0]['messages'][0]['content'])

    def test_local_boundary_checks_allow_valid_task_clarification(self):
        agent, wire = self.make(['通知的日期是周六吗？', verdict('I1', 'pass')])
        agent.reply('别追问我以前的经历，帮我写一份通知')
        self.assertEqual(len(wire.requests), 2); self.assertEqual(len(agent.history), 2)
        data = json.loads(wire.requests[-1]['messages'][-1]['content'])
        self.assertTrue(data['scope']['permissions']['task_clarification'])
        self.assertFalse(data['scope']['permissions']['solicit_personal_information'])
        self.assertIn('personal_history', [b['target'] for b in data['scope']['boundaries']])

    def test_conflicting_personal_inquiry_not_delivered_under_writing_invitation(self):
        agent, wire = self.make(['请列出以往家庭经历', verdict()])
        with self.assertRaises(ProviderError): agent.reply('帮我写一份入社须知，别追问我过去的经历')
        self.assertFalse(agent.history); self.assertEqual(len(wire.requests), 2)

    def test_unknown_cancel_budget_do_not_commit_under_new_scope(self):
        for result in ({'verdict':'unknown','codes':['uncertain'],'message_ids':['I1']}, KeyboardInterrupt()):
            agent, wire = self.make(['继续问你的以往经历', result])
            with self.assertRaises((ProviderError, KeyboardInterrupt)):
                agent.reply('别问我过去的经历，帮我写一封邀请函')
            self.assertFalse(agent.history); self.assertEqual(len(wire.requests), 2)
        agent, wire = self.make([], budget=1)
        with self.assertRaises(ProviderError): agent.reply('别问我过去的经历，帮我写一封邀请函')
        self.assertFalse(agent.history); self.assertEqual(len(wire.requests), 0)
        self.assertIn('personal_history', agent.interaction.boundaries)

    def test_revision_invalidation_removes_task_object_not_boundary(self):
        agent, _ = self.make(['一段合成告示。'])
        agent.reply('帮我写一段告示')
        agent.interaction.receive('别问我的过去')
        agent.clear()
        self.assertIsNone(agent.interaction.task_object)
        self.assertIn('personal_history', agent.interaction.boundaries)
        self.assertTrue(all(s['content'] is None for s in agent.interaction.sources.values()))

    def test_presupposed_question_reaches_checker_and_can_be_blocked(self):
        agent, wire = self.make(['你通常为什么会忘带材料', verdict()])
        with self.assertRaises(ProviderError): agent.reply('帮我写一份入社须知')
        self.assertEqual(len(wire.requests), 2); self.assertFalse(agent.history)

    def test_assistant_proposal_and_adoption_never_auto_save_user_fact(self):
        agent, wire = self.make(['我想叫它星屑邮局，邮差喜欢收集风筝。', '沿用这个设想。', '收到你的实际偏好。'])
        agent.natural_memory.set_mode('auto')
        agent.reply('给虚构的空间站编一家店吧')
        agent.reply('就用“星屑邮局”这个设定')
        self.assertEqual(agent.natural_memory.list(), [])
        self.assertEqual(agent.store.list_memories(), [])
        agent.reply('我喜欢游泳。')
        rows = agent.natural_memory.list()
        self.assertEqual(len(rows), 1)
        self.assertIn('游泳', rows[0]['evidence'])
        self.assertNotIn('风筝', str(rows)); self.assertNotIn('星屑邮局', str(rows))
        self.assertEqual(agent.usage.memory_calls, 0)

    def test_actual_roles_and_adoption_links_are_untrusted_and_not_history(self):
        agent, wire = self.make(['我提议用雾灯钟塔。', '沿用这个提案。'])
        agent.reply('给虚构城市想座建筑')
        agent.reply('就用“雾灯钟塔”这个设定')
        messages = wire.requests[-1]['messages']
        actual = [m['content'] for m in messages if m['role'] == 'assistant']
        self.assertEqual(actual, ['我提议用雾灯钟塔。'])
        context = json.loads(next(m['content'] for m in messages if m.get('name') == 'context_data').split('\n', 1)[1])
        links = next(r['records'] for r in context if r['source'] == 'request_local_dialogue_source_relations')
        self.assertEqual(links['adoptions'][0]['fragment'], '雾灯钟塔')
        self.assertEqual(links['adoptions'][0]['status'], 'explicit_fragment_adoption_for_current_task_only')
        self.assertNotIn('雾灯钟塔', messages[0]['content'])
        self.assertFalse(agent.store.list_memories()); self.assertFalse(agent.natural_memory.list())


class SourceAndGuidanceTests(unittest.TestCase):
    def test_only_exact_explicit_unambiguous_adoption_links(self):
        history = [{'role':'user','content':'编一个车站'}, {'role':'assistant','content':'我提议叫它回声站。'}]
        for text in ('好啊', '不采用“回声站”', '就用“回声站”，算了', '假如采用“回声站”', '就用“另一个名称”',
                     '就用“回声站”吗', '就用“回声站”？', '就用“回声站”，不过我还没决定'):
            self.assertEqual(relations(history, text)['adoptions'], [])
        result = relations(history, '就用“回声站”这个设定')
        self.assertEqual(result['adoptions'][0]['assistant_ref'], 'H1')
        self.assertFalse(result['memory_write_source'])
        self.assertEqual(relations(history + [history[-1]], '就用“回声站”')['adoptions'], [])
        self.assertEqual(relations([], '就用“回声站”')['adoptions'], [])

    def test_role_registry_bounded_and_no_assistant_as_user_fact(self):
        history = [{'role':'assistant' if i % 2 else 'user','content':'新写合成文本'} for i in range(40)]
        result = relations(history, '再想一个')
        self.assertEqual(len(result['refs']), 12)
        self.assertTrue(all(r['status'] == 'assistant_contribution_not_user_evidence' for r in result['refs'] if r['role'] == 'assistant'))

    def test_versioned_guidance_and_all_contrast_categories_compile(self):
        spec = validate_materials(); validate_baseline()
        self.assertIn('companion-v3.md', spec['files'])
        examples = json.loads(examples_text('v3'))
        focuses = {f for case in examples['cases'] for f in case['focus']}
        self.assertTrue({'sharing','co_creation','explicit_options','setback','specific_help','narrow_scope','pause'} <= focuses)
        self.assertLessEqual(len(examples_text('v3')), 3000)
        self.assertIn('not_model_outputs', examples['provenance'])
        root = Path(__file__).resolve().parents[1]
        plan = model_access.resolve('daily-deepseek', root/'configs/deepseek-official.json')
        self.assertEqual(plan.policy.companion_runtime_version, 'v3')
        self.assertFalse(plan.policy.allow_recovery)
        with patch.dict('os.environ', {}, clear=True):
            self.assertEqual(model_access.resolve('competition-nebius').policy.companion_runtime_version, 'v2')


if __name__ == '__main__': unittest.main()
