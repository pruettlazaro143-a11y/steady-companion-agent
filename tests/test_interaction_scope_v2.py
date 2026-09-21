"""New synthetic scenarios. Fake replies/verdicts test control flow, not model quality."""
import io,json,tempfile,unittest
from copy import deepcopy
from dataclasses import replace
from pathlib import Path
from unittest.mock import patch
from steady_companion import model_access as access
from steady_companion.chat_transport import ChatCompletionsClient
from steady_companion.interaction import InteractionState
from steady_companion.interaction_runtime import InteractionConversation
from steady_companion.provider import ProviderError
from steady_companion.store import Store


def response(text):
    return {'choices':[{'message':{'role':'assistant','content':text if isinstance(text,str) else json.dumps(text)},'finish_reason':'stop'}],
            'usage':{'prompt_tokens':11,'completion_tokens':7,'total_tokens':18}}

class Wire:
    def __init__(self,values):self.values=list(values);self.requests=[]
    def open(self,req,timeout):
        self.requests.append(json.loads(req.data));value=self.values.pop(0)
        if isinstance(value,BaseException):raise value
        class Reply(io.BytesIO):
            status=200
            def geturl(self):return req.full_url
        return Reply(json.dumps(response(value)).encode())

def verdict(ref='I1',state='conflict'):
    return {'verdict':state,'codes':['none'] if state=='pass' else ['uninvited_assessment'],'message_ids':[ref]}

class StateTests(unittest.TestCase):
    def state(self,*messages):
        s=InteractionState()
        for m in messages:s.receive(m)
        return s
    def test_mood_clause_then_pause(self):
        s=self.state('这周工作不顺','有点烦，暂时不谈这个了')
        self.assertIn('work',s.boundaries);self.assertTrue(s.needs_check('从什么时候开始不顺的'))
    def test_named_compound_pause(self):
        s=self.state('我很疲倦，先不聊绩效了')
        self.assertIn('performance',s.boundaries)
    def test_quotes_reported_condition_negation_not_pause(self):
        for text in ('有人说先不谈工作了','如果疲惫，先不聊工作了','他说：“不聊工作了”，然后离开','我并不是说先不谈工作了','小说台词是“不聊工作了”'):
            with self.subTest(text=text):self.assertFalse(self.state(text).boundaries)
    def test_report_then_own_control(self):
        s=self.state('工作有些复杂','同事说先不聊工作了，不过我现在先不聊绩效了')
        self.assertNotIn('work',s.boundaries);self.assertIn('performance',s.boundaries)
    def test_withdrawn_request_does_not_reopen(self):
        for message in ('帮我分析工作安排就算了，我只是想吐槽','帮我分析工作安排，算了','帮我分析工作安排，先不分析了','“帮我分析工作”是台词','如果可以，帮我分析工作安排'):
            with self.subTest(message=message):
                s=self.state('先不分析了','先不聊工作了',message)
                self.assertIn('analysis',s.boundaries);self.assertIn('work',s.boundaries);self.assertFalse(s.permissions()['analysis'])
                self.assertNotIn('user_activity_opened',[e['code'] for e in s.events])
    def test_positive_reopening_is_specific(self):
        s=self.state('先不聊成绩了','先不分析了','现在帮我分析工作安排')
        self.assertNotIn('analysis',s.boundaries);self.assertIn('grades',s.boundaries)
        self.assertEqual(s.permissions()['analysis_object'],'work');self.assertFalse(s.permissions()['solicit_personal_information'])
    def test_task_scope_not_personal_history(self):
        s=self.state('请帮我解释这道题的解法')
        self.assertTrue(s.permissions()['task_clarification']);self.assertFalse(s.permissions()['solicit_personal_information'])
        self.assertTrue(s.needs_check('请列出过往家庭经历'));self.assertFalse(s.needs_check('题目给出的角度是多少？'))
    def test_invited_personal_scope_is_not_global(self):
        s=self.state('帮我分析家庭关系')
        self.assertTrue(s.permissions()['solicit_personal_information']);self.assertEqual(s.permissions()['personal_information_scope'],'personal_family')
        self.assertFalse(s.needs_check('说说家庭经历'));self.assertTrue(s.needs_check('告诉我以前每次考试情况'))
    def test_investigation_paraphrases_without_question_marks(self):
        s=self.state('今天学习不顺利')
        for candidate in ('哪个环节最难，先说说','从什么时候开始的，细说一下','独立完成就很难，还是听讲时能跟上','问题出在你的习惯，先分析原因','先补充过往经历'):
            with self.subTest(candidate=candidate):self.assertTrue(s.needs_check(candidate))
    def test_cross_turn_anaphoric_followup(self):
        s=self.state('工作碰到了麻烦');s.delivered('从什么时候开始的');s.receive('去年开始')
        self.assertTrue(s.needs_check('再早些呢'));self.assertFalse(s.permissions()['analysis'])
    def test_legitimate_continuous_task_questions(self):
        s=self.state('帮我解释这道题')
        for user in ('三十度','长度为五','图上标了直角'):
            self.assertFalse(s.needs_check('另一个已知条件是什么？'));s.delivered('另一个已知条件是什么？');s.receive(user)
            self.assertTrue(s.permissions()['task_clarification'])
    def test_sharing_retrieval_is_not_analysis_permission(self):
        for text in ('最近爱看博物馆历史介绍','聊聊音乐吧','最近在研究陶艺'):
            s=self.state(text);self.assertTrue(s.permissions()['content_retrieval']);self.assertFalse(s.permissions()['assessment_retrieval'])
    def test_ambiguous_quote_never_clears_boundary(self):
        s=self.state('先不分析了','帮我分析“工作')
        self.assertIn('analysis',s.boundaries);self.assertEqual(s.uncertainty,'ambiguous_quote')

class DeliveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
    def make(self,values,budget=8):
        plan=access.resolve('daily-deepseek',Path(__file__).resolve().parents[1]/'configs/deepseek-official.json')
        wire=Wire(values);transport=ChatCompletionsClient(replace(plan.connection,api_key='SYNTHETIC_ONLY'),wire,policy=plan.transport_policy)
        store=Store(self.root/str(len(list(self.root.iterdir()))));self.addCleanup(store.close)
        agent=InteractionConversation(access.AccessClient(plan,transport),store,interaction_check='selective',max_calls=budget,**plan.policy.kwargs())
        return agent,wire
    def test_compound_pause_blocks_delivery_and_commit(self):
        a,w=self.make(['合成普通回应','从什么时候开始的',verdict('I2')])
        a.reply('工作进展不顺');before=deepcopy(a.history)
        with patch.object(a.natural_memory,'save_current',wraps=a.natural_memory.save_current) as save:
            with self.assertRaises(ProviderError):a.reply('我有点倦了，暂时不谈这个了')
        self.assertEqual(a.history,before);save.assert_not_called();self.assertEqual(a.usage.calls,3);self.assertEqual(a.check_status['calls'],1)
        self.assertIn('work',a.interaction.boundaries)
    def test_withdrawn_invitation_retains_boundary_at_delivery(self):
        a,w=self.make(['合成暂停回应',verdict('I1','pass'),'先把家庭经历告诉我',verdict('I2')])
        a.reply('先不分析了')
        with self.assertRaises(ProviderError):a.reply('帮我分析工作安排，算了，我只是吐槽')
        self.assertIn('analysis',a.interaction.boundaries);self.assertEqual(len(a.history),2);self.assertEqual(len(w.requests),4)
    def test_problem_request_cannot_deliver_personal_collection(self):
        a,w=self.make(['请列出过往家庭经历',verdict()])
        with self.assertRaises(ProviderError):a.reply('帮我解释这道题的条件')
        self.assertFalse(a.history);self.assertFalse(a.store.list_memories());self.assertEqual(a.usage.calls,2)
    def test_task_clarification_delivered_without_extra_call(self):
        a,w=self.make(['题目的边长给了多少？'])
        a.reply('帮我解释这道题');self.assertEqual(len(a.history),2);self.assertEqual(len(w.requests),1);self.assertEqual(a.check_status['status'],'not_triggered')
    def test_investigation_candidate_enters_checker_not_automatic_rejection(self):
        for candidate in ('哪个环节最难，先说说','从什么时候开始的','问题出在你的习惯，先分析原因'):
            a,w=self.make([candidate,verdict()])
            with self.assertRaises(ProviderError):a.reply('最近工作很累')
            self.assertFalse(a.history);self.assertEqual(len(w.requests),2)
        a,w=self.make(['从什么时候开始的',verdict(state='pass')]);result=a.reply('最近工作很累')
        self.assertTrue(result.text);self.assertEqual(a.check_status['status'],'pass')
    def test_cross_turn_followup_checker_gets_actual_history(self):
        a,w=self.make(['从什么时候开始的',verdict(state='pass'),'再早些呢',verdict('I2')])
        a.reply('最近工作很累')
        with self.assertRaises(ProviderError):a.reply('去年开始')
        data=json.loads(w.requests[-1]['messages'][-1]['content'])
        self.assertEqual(data['recent_dialogue'],a.history);self.assertEqual(len(w.requests),4)
    def install_library(self,agent,rows):
        from shutil import copytree
        root=self.root/('library'+str(len(list(self.root.iterdir()))));copytree(agent.architecture.base.root,root)
        for path in (root/'topics').glob('*.json'):path.unlink()
        for row in rows:(root/'topics'/(row['id']+'.json')).write_text(json.dumps(row))
        agent.architecture.base.root=root
    def retrieved(self,wire):
        message=next(m for m in wire.requests[0]['messages'] if m.get('name')=='context_data')
        return next(r['records'] for r in json.loads(message['content'].split('\n',1)[1]) if r['source']=='retrieved_topic_material')
    def topic(self,ident,topic,scope='ordinary_topic'):
        return {'id':ident,'topic':topic,'aliases':[topic],'body':'新写合成主题材料。','source':'synthetic fixture','updated_at':'2026-09-21','scope':scope}
    def test_actual_local_library_in_ordinary_sharing_context(self):
        a,w=self.make(['合成主题回应']);self.install_library(a,[self.topic('ceramics','陶艺')])
        a.reply('最近很喜欢陶艺')
        self.assertIn('新写合成主题材料',str(w.requests[0]['messages']));self.assertEqual(len(w.requests),1)
    def test_pause_filters_old_object_but_keeps_new_content(self):
        a,w=self.make(['合成回应',verdict(state='pass')])
        self.install_library(a,[self.topic('work','工作'),self.topic('history','历史')])
        a.reply('先不聊工作了，讲讲历史')
        self.assertEqual([r['id'] for r in self.retrieved(w)],['history']);self.assertIn('work',a.interaction.boundaries)
    def test_assessment_material_needs_object_permission(self):
        a,w=self.make(['合成普通回应']);self.install_library(a,[self.topic('assessment','工作','personal_assessment')])
        a.reply('这周工作很累');self.assertEqual(self.retrieved(w),[])
    def test_selective_budget_exhaustion_never_delivers_unchecked(self):
        a,w=self.make(['从什么时候开始的'],budget=1)
        with self.assertRaises(ProviderError):a.reply('最近工作很累')
        self.assertEqual(len(w.requests),1);self.assertFalse(a.history);self.assertEqual(a.check_status['status'],'budget_blocked')
    def test_technical_failure_no_retry_no_commit(self):
        for failure in (TimeoutError('PRIVATE'),KeyboardInterrupt('PRIVATE')):
            a,w=self.make(['从什么时候开始的',failure])
            with self.assertRaises((ProviderError,KeyboardInterrupt)):a.reply('最近工作很累')
            self.assertEqual(a.usage.calls,2);self.assertFalse(a.history);self.assertNotIn('PRIVATE',str(a.last_diagnostic))
    def test_no_unconditional_topic_retrieval(self):
        a,w=self.make(['合成招呼']);self.install_library(a,[self.topic('ceramics','陶艺')])
        a.reply('你好');self.assertEqual(self.retrieved(w),[]);self.assertEqual(len(w.requests),1)

if __name__=='__main__':unittest.main()
