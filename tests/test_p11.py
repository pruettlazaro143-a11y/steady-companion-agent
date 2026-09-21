"""P1.1 regressions: synthetic storage, retrieval, context and failure behavior."""
from copy import deepcopy
from contextlib import redirect_stdout
import io
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from steady_companion.engine import Conversation
from steady_companion.store import Store, StaleRevisionError
from steady_companion.natural_memory import NaturalMemory, plan_update
from steady_companion.memory_language import parse_events, supported_record
from steady_companion.cli import execute_command, main
from steady_companion.provider import ProviderError


class Fake:
    def __init__(self): self.requests=[]
    def complete(self,messages,**kwargs):
        self.requests.append(deepcopy(messages))
        return {'choices':[{'message':{'role':'assistant','content':'[离线夹具，无模型效果结论]'}}]}


class MemoryCorrectnessTests(unittest.TestCase):
    def setUp(self):
        temp=tempfile.TemporaryDirectory(); self.addCleanup(temp.cleanup)
        self.root=Path(temp.name); self.store=Store(self.root/'user-a'); self.fake=Fake()
        self.agent=Conversation(self.fake,self.store,response_mode='direct')
        self.command('/memory-mode auto')

    def command(self,text):
        with redirect_stdout(io.StringIO()): execute_command(text,self.agent,self.store)

    def saved(self): return self.agent.natural_memory.list()
    def request(self): return json.dumps(self.fake.requests[-1],ensure_ascii=False)
    def context(self):
        return '\n'.join(m['content'] for m in self.fake.requests[-1] if m.get('name')=='context_data')
    def restart(self):
        self.fake=Fake();self.agent=Conversation(self.fake,Store(self.store.data_dir),response_mode='direct')

    def test_repro1_sensitive_health_preference_does_not_persist_any_field(self):
        raw='以后别提我的 HIV 阳性检测结果。'
        turn=self.agent.reply(raw)
        self.assertEqual(self.saved(),[])
        self.assertIn(raw,self.request())  # Current boundary is still given to the model.
        self.assertNotIn('HIV',self.context())
        self.assertNotIn('HIV',json.dumps(turn.memory_update))
        self.restart();self.agent.reply('你好');self.assertNotIn('HIV',self.request())
        self.assertNotIn(b'HIV',self.store.db_path.read_bytes())

    def test_all_auto_categories_fail_closed_for_sensitive_or_unknown_free_text(self):
        cases=['以后别提我的 HIV 阳性检测结果。','我们把 HIV 阳性的人叫“隐形学霸”。',
               '我们把先说自己不会、结果考第一的人叫“HIV阳性”。','我把薄荷取名“HIV阳性”。',
               '下次再聊我的 HIV 检测结果。','以后别提我的未知检测项目 ABC123 结果。',
               '我们管隐瞒某种检查结果的人叫“隐形学霸”。','以后别提我的秘密检查结论。',
               '我喜欢篮球，也不想提检查结果。','以后别拿成绩开玩笑，我的 HIV 结果是阳性。',
               '我们把服用特殊药物的人叫“隐形学霸”。']
        for text in cases:
            with self.subTest(text=text):
                self.agent.reply(text);self.assertEqual(self.saved(),[])
        self.assertEqual(len(self.fake.requests),len(cases))

    def test_storage_revalidates_evidence_scope_not_only_extractor_output(self):
        p=plan_update('我喜欢篮球。',[])
        p['changes'][0]['event']['evidence']='我喜欢篮球，我的 HIV 是阳性。'
        with self.assertRaises(ValueError):
            self.agent.natural_memory.save_current('我喜欢篮球。',self.store.revision(),plan=p)
        self.assertEqual(self.saved(),[])
        e=parse_events('以后别拿成绩开玩笑。')[0]
        self.assertFalse(supported_record(dict(e,source='HIV positive')))

    def test_repro2_manual_topic_replacement_rekeys_without_old_topic_overwrite(self):
        self.agent.reply('我喜欢篮球。');self.command('/auto-correct A1 我喜欢游泳。')
        self.assertEqual(self.saved()[0]['semantic_key'],'interest:游泳:')
        self.agent.reply('我不喜欢篮球。')
        rows=self.saved();self.assertEqual(len(rows),2)
        self.assertEqual({r['meaning'] for r in rows},{'positive游泳','negative篮球'})
        self.restart();self.agent.reply('篮球和游泳');c=self.context()
        self.assertIn('我喜欢游泳',c);self.assertIn('我不喜欢篮球',c);self.assertNotIn('我喜欢篮球',c)

    def test_manual_reclassification_updates_kind_scope_key_and_source(self):
        self.agent.reply('我喜欢篮球。');self.command('/auto-correct A1 以后别拿成绩开玩笑。')
        row=self.saved()[0]
        self.assertEqual(row['kind'],'preference');self.assertEqual(row['semantic_key'],'boundary:joke:成绩')
        self.assertEqual(row['source'],'user_command');self.assertIn('joke:成绩',row['scope'])
        self.agent.reply('我不喜欢篮球。');self.assertEqual(len(self.saved()),2)

    def test_uncertain_manual_correction_detaches_from_auto_merge(self):
        self.agent.reply('我喜欢篮球。');self.command('/auto-correct A1 最近运动偏好比较复杂，先照我的当下要求。')
        old=self.saved()[0];self.assertEqual(old['merge_policy'],'manual');self.assertEqual(old['kind'],'manual')
        self.assertTrue(old['semantic_key'].startswith('manual:'))
        self.agent.reply('我不喜欢篮球。')
        self.assertEqual(len(self.saved()),2);self.assertEqual(self.saved()[0],old)

    def test_manual_key_collision_is_atomic_and_does_not_overwrite(self):
        self.agent.reply('我喜欢篮球。');self.agent.reply('我喜欢游泳。')
        before=deepcopy(self.saved());rev=self.store.revision()
        with self.assertRaises(ValueError): self.command('/auto-correct A1 我不喜欢游泳。')
        self.assertEqual(self.saved(),before);self.assertEqual(self.store.revision(),rev)

    def test_same_fact_manual_update_keeps_id_and_canonical_identity(self):
        self.agent.reply('我喜欢篮球。');self.command('/auto-correct A1 平时挺喜欢打篮球的。')
        row=self.saved()[0];self.assertEqual(row['id'],1);self.assertEqual(row['semantic_key'],'interest:篮球:')
        self.agent.reply('我不喜欢篮球。');self.assertEqual(len(self.saved()),1)
        self.assertEqual(self.saved()[0]['meaning'],'negative篮球')

    def test_repro3_retraction_removes_ban_before_generation_and_after_restart(self):
        self.agent.reply('以后别拿成绩开玩笑。')
        self.agent.pending_learning=[{'id':'L1','text':'旧禁令'}]
        turn=self.agent.reply('现在可以拿成绩开玩笑了。')
        self.assertIn('retract',turn.memory_update['outcomes'])
        self.assertEqual(self.saved(),[]);self.assertNotIn('以后别拿成绩',self.request())
        self.assertFalse(self.agent._p1_context)
        self.assertFalse(self.agent.pending_learning)
        self.restart();self.agent.reply('成绩');self.assertNotIn('别拿成绩',self.request())

    def test_preference_restore_after_retraction_is_new_valid_ban(self):
        self.agent.reply('以后别拿成绩开玩笑。');self.agent.reply('现在可以拿成绩开玩笑了。')
        self.agent.reply('以后不要拿成绩开玩笑。')
        self.restart();self.agent.reply('成绩')
        self.assertIn('不要拿成绩',self.context());self.assertEqual(len(self.saved()),1)

    def test_temporary_exception_does_not_change_durable_setting(self):
        self.agent.reply('以后别拿成绩开玩笑。');before=deepcopy(self.saved());rev=self.store.revision()
        turn=self.agent.reply('今天可以，平时还是别说。')
        self.assertIn('temporary',turn.memory_update['outcomes']);self.assertEqual(self.saved(),before)
        self.assertEqual(self.store.revision(),rev);self.assertNotIn('以后别拿成绩',self.request())
        self.agent.reply('继续聊成绩');self.assertNotIn('以后别拿成绩',self.context())
        self.restart();self.agent.reply('成绩');self.assertIn('以后别拿成绩',self.context())

    def test_permanent_retraction_after_temporary_exception_finds_durable_target(self):
        self.agent.reply('以后别拿成绩开玩笑。');self.agent.reply('今天可以，平时还是别说。')
        self.agent.reply('现在可以拿成绩开玩笑了。')
        self.assertEqual(self.saved(),[])
        self.restart();self.agent.reply('成绩');self.assertNotIn('别拿成绩',self.context())

    def test_ambiguous_reference_does_not_batch_retract(self):
        self.agent.reply('以后别拿成绩开玩笑。');self.agent.reply('以后别每句都问问题。')
        before=deepcopy(self.saved());turn=self.agent.reply('撤回刚才的要求。')
        self.assertEqual(self.saved(),before);self.assertIn('no_save_ambiguous_target',turn.memory_update['outcomes'])
        self.assertIn('撤回刚才的要求',self.request())

    def test_explicit_scope_change_preserves_unrelated_preferences(self):
        self.agent.reply('以后别开玩笑。');self.agent.reply('以后别每句都问问题。')
        turn=self.agent.reply('以后只在聊成绩时别开玩笑，其他话题可以。')
        self.assertIn('scope_change',turn.memory_update['outcomes'])
        keys={r['semantic_key'] for r in self.saved()}
        self.assertEqual(keys,{'boundary:joke:成绩','boundary:questions:every_turn'})
        self.restart();self.agent.reply('你好');self.assertNotIn('以后别开玩笑。',self.context())

    def test_similar_but_distinct_subject_preferences_do_not_merge(self):
        self.agent.reply('以后别拿成绩开玩笑。');self.agent.reply('以后别拿游戏开玩笑。')
        self.agent.reply('现在可以拿成绩开玩笑了。')
        self.assertEqual(len(self.saved()),1);self.assertIn('游戏',self.saved()[0]['semantic_key'])

    def test_repro4_write_failure_preserves_reply_and_session_override(self):
        self.agent.reply('我喜欢篮球。');before=deepcopy(self.saved())
        with patch.object(self.agent.natural_memory,'save_current',side_effect=sqlite3.OperationalError('SYNTHETIC_PRIVATE_HIV')) as writer:
            turn=self.agent.reply('我不喜欢篮球。')
            self.assertEqual(writer.call_count,1)
        self.assertTrue(turn.text);self.assertEqual(turn.memory_update['status'],'not_saved')
        self.assertEqual(self.saved(),before);self.assertNotIn('我喜欢篮球',self.request())
        self.assertIn('我不喜欢篮球',self.context())
        self.assertNotIn('SYNTHETIC_PRIVATE_HIV',json.dumps(turn.memory_update))
        with patch.object(self.agent.natural_memory,'save_current',side_effect=AssertionError('must not retry')):
            self.agent.reply('篮球');self.assertNotIn('我喜欢篮球',self.request())
        self.restart();self.agent.reply('篮球');self.assertIn('我喜欢篮球',self.context())

    def test_retraction_write_failure_suppresses_ban_without_pretending_persistence(self):
        self.agent.reply('以后别拿成绩开玩笑。')
        with patch.object(self.agent.natural_memory,'save_current',side_effect=sqlite3.OperationalError('private')):
            turn=self.agent.reply('现在可以拿成绩开玩笑了。')
        self.assertEqual(turn.memory_update['status'],'not_saved');self.assertEqual(len(self.saved()),1)
        self.agent.reply('成绩');self.assertNotIn('别拿成绩',self.request())
        self.restart();self.agent.reply('成绩');self.assertIn('别拿成绩',self.context())

    def test_read_failure_drops_durable_context_but_reply_and_current_correction_work(self):
        self.agent.reply('我喜欢篮球。');self.store.remember('OLD_LEGACY_PRIVATE','preference')
        self.agent._revision=self.store.revision()
        with patch.object(self.agent.natural_memory,'list',side_effect=sqlite3.OperationalError('PRIVATE_DB_ERROR')):
            turn=self.agent.reply('我不喜欢篮球。')
        self.assertTrue(turn.text);self.assertTrue(turn.memory_update['read_degraded'])
        self.assertNotIn('OLD_LEGACY_PRIVATE',self.request());self.assertNotIn('我喜欢篮球',self.request())
        self.agent.reply('篮球');self.assertNotIn('我喜欢篮球',self.request())
        self.assertIn('我不喜欢篮球',self.context())

    def test_revision_read_failure_degrades_and_does_not_retry_each_stage(self):
        self.agent.reply('我喜欢篮球。')
        with patch.object(self.store,'revision',side_effect=sqlite3.OperationalError('SECRET')) as read:
            turn=self.agent.reply('我不喜欢篮球。')
        self.assertTrue(turn.text);self.assertTrue(turn.memory_update['read_degraded'])
        self.assertLessEqual(read.call_count,1);self.assertNotIn('SECRET',json.dumps(turn.memory_update))
        self.assertNotIn('我喜欢篮球',self.context())

    def test_colloquial_variant_and_multi_fact_have_exact_separate_evidence(self):
        self.agent.reply('平时挺喜欢打篮球的。');self.assertEqual(len(self.saved()),1)
        turn=self.agent.reply('我喜欢篮球，也喜欢游泳。')
        rows=self.saved();self.assertEqual(len(rows),2)
        self.assertIn('duplicate',turn.memory_update['outcomes']);self.assertIn('add',turn.memory_update['outcomes'])
        swim=next(r for r in rows if '游泳' in r['semantic_key'])
        self.assertEqual(swim['evidence'],'也喜欢游泳');self.assertNotIn('篮球',json.dumps(swim,ensure_ascii=False))

    def test_temporal_qualifier_inherited_but_different_ranges_not_merged(self):
        self.agent.reply('我以前喜欢篮球，也喜欢游泳。')
        rows=self.saved();self.assertEqual(len(rows),2)
        self.assertTrue(all('以前' in r['scope'] for r in rows))
        self.agent.reply('我现在不喜欢篮球。');self.agent.reply('我最近喜欢篮球。')
        self.assertEqual(len(self.saved()),4)
        self.assertEqual(len({r['semantic_key'] for r in self.saved()}),4)

    def test_negative_multi_fact_and_no_ai_evidence(self):
        self.agent.reply('我不喜欢篮球，也喜欢游泳。')
        self.assertEqual({r['meaning'] for r in self.saved()},{'negative篮球','positive游泳'})
        self.agent.history[-1]['content']='我喜欢钢琴。'
        self.agent.reply('嗯');self.assertEqual(len(self.saved()),2)

    def test_quotes_reports_fiction_uncertainty_and_contradiction_skip_all(self):
        texts=['朋友说平时挺喜欢打篮球的。','我朋友喜欢篮球，也喜欢游泳。','“我喜欢篮球，也喜欢游泳。”',
               '假如我喜欢篮球，也喜欢游泳。','我可能喜欢篮球，也喜欢游泳。','我喜欢篮球吗？',
               '我喜欢篮球，也不喜欢篮球。','I said I like basketball.','我喜欢篮球，但是有件事我不确定。']
        for text in texts:
            with self.subTest(text=text):self.agent.reply(text);self.assertEqual(self.saved(),[])

    def test_missing_joke_definition_is_volatile_and_next_user_explanation_links(self):
        self.agent.reply('我们管这种人叫“隐形学霸”。')
        self.assertEqual(self.saved(),[]);self.assertIsNotNone(self.agent._pending_joke_label)
        self.agent.reply('就是说先说自己不会、结果考第一的人。')
        self.assertEqual(len(self.saved()),1);row=self.saved()[0]
        self.assertEqual(row['kind'],'joke');self.assertEqual(row['source'],'user_linked_turns')
        self.assertIn('我们管这种人',row['evidence']);self.assertIn('就是说先说',row['evidence'])
        self.restart();self.agent.reply('隐形学霸');self.assertIn('考第一',self.context())

    def test_joke_link_cannot_take_ai_definition_or_survive_delete_or_unrelated_turn(self):
        self.agent.reply('我们管这种人叫“隐形学霸”。')
        self.agent.history[-1]['content']='就是说先说自己不会、结果考第一的人。'
        self.agent.reply('好');self.assertEqual(self.saved(),[])
        self.agent.reply('就是说先说自己不会、结果考第一的人。');self.assertEqual(self.saved(),[])
        self.agent.reply('我们管这种人叫“隐形学霸”。');self.command('/auto-forget all')
        self.agent.reply('就是说先说自己不会、结果考第一的人。');self.assertEqual(self.saved(),[])

    def test_linked_sensitive_explanation_is_not_saved_in_any_field(self):
        self.agent.reply('我们管这种人叫“隐形学霸”。')
        self.agent.reply('就是说 HIV 阳性但平时不声张的人。')
        self.assertEqual(self.saved(),[]);self.assertIsNone(self.agent._pending_joke_label)

    def test_deletion_after_failed_correction_clears_overlay_and_stale_sources(self):
        self.agent.reply('我喜欢篮球。')
        with patch.object(self.agent.natural_memory,'save_current',side_effect=sqlite3.OperationalError('private')):
            self.agent.reply('我不喜欢篮球。')
        self.command('/auto-forget A1');self.agent.reply('篮球')
        self.assertEqual(self.saved(),[]);self.assertFalse(self.agent._memory_overrides)
        self.assertNotIn('我喜欢篮球',self.request());self.assertNotIn('我不喜欢篮球',self.request())

    def test_delete_one_multifact_record_leaves_no_deleted_fact_in_other_evidence(self):
        self.agent.reply('我喜欢篮球，也喜欢游泳。');self.command('/auto-forget A1')
        self.assertEqual(len(self.saved()),1);self.assertNotIn('篮球',json.dumps(self.saved(),ensure_ascii=False))
        self.restart();self.agent.reply('篮球和游泳');self.assertNotIn('我喜欢篮球',self.context())

    def test_budget_and_isolation_and_no_storage_on_failed_generation(self):
        self.agent.max_calls=1;self.agent.reply('我喜欢篮球，也喜欢游泳。')
        self.assertEqual(len(self.fake.requests),1)
        with self.assertRaises(ProviderError):self.agent.reply('我喜欢钢琴。')
        self.assertEqual(len(self.saved()),2)
        other=NaturalMemory(Store(self.root/'user-b'));self.assertEqual(other.mode,'manual');self.assertEqual(other.list(),[])

    def test_extraction_count_and_batch_capacity_are_bounded_and_atomic(self):
        self.agent.reply('我喜欢篮球，也喜欢游泳，也喜欢钢琴，也喜欢摄影。')
        self.assertEqual(self.saved(),[])
        with patch('steady_companion.natural_memory.MAX_RECORDS',1):
            turn=self.agent.reply('我喜欢篮球，也喜欢游泳。')
        self.assertEqual(self.saved(),[]);self.assertEqual(turn.memory_update['status'],'storage_limit')

    def test_legacy_migration_preserves_but_excludes_unsafe_auto_record(self):
        folder=self.root/'legacy';folder.mkdir();db=folder/'companion.sqlite3'
        with sqlite3.connect(db) as c:
            c.execute('CREATE TABLE p1_memories(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,text TEXT,scope TEXT,source TEXT,evidence TEXT,created_at TEXT,updated_at TEXT,status TEXT,semantic_key TEXT UNIQUE,meaning TEXT)')
            raw='以后别提我的 HIV 阳性检测结果。'
            c.execute('INSERT INTO p1_memories VALUES(1,?,?,?,?,?,?,?,?,?,?)',('preference',raw,'old','user_current_turn',raw,'2026','2026','active','old-key','old'))
        store=Store(folder);memory=NaturalMemory(store);row=memory.list()[0]
        self.assertEqual(row['text'],raw);self.assertEqual(row['evidence'],raw)
        self.assertEqual(row['merge_policy'],'excluded_legacy');self.assertEqual(memory.select('你好'),[])
        rev=store.revision();NaturalMemory(store);self.assertEqual(store.revision(),rev)

    def test_fresh_user_fact_after_delete_can_be_explicitly_saved_again(self):
        self.agent.reply('我喜欢篮球。');self.command('/auto-forget A1');self.agent.reply('嗯')
        self.assertEqual(self.saved(),[])
        self.agent.reply('我喜欢篮球。');self.assertEqual(len(self.saved()),1)

    def test_persistent_record_payload_cannot_bypass_scope_gate(self):
        plan=plan_update('我喜欢篮球。',[])
        plan['changes'][0]['record']['evidence']='HIV 阳性'
        with self.assertRaises(ValueError):
            self.agent.natural_memory.save_current('我喜欢篮球。',self.store.revision(),plan=plan)
        self.assertEqual(self.saved(),[])

    def test_invalid_second_record_rolls_back_whole_batch(self):
        plan=plan_update('我喜欢篮球，也喜欢游泳。',[])
        plan['changes'][1]['record']['scope']='SENSITIVE_FACT'
        with self.assertRaises(ValueError):
            self.agent.natural_memory.save_current('我喜欢篮球，也喜欢游泳。',self.store.revision(),plan=plan)
        self.assertEqual(self.saved(),[])

    def test_this_turn_exception_expires_at_next_user_turn(self):
        self.agent.reply('以后别拿成绩开玩笑。')
        self.agent.reply('这次可以拿成绩开玩笑。')
        self.assertNotIn('以后别拿成绩',self.context())
        self.agent.reply('接下来聊成绩');self.assertIn('以后别拿成绩',self.context())
        self.assertNotIn('这次可以拿成绩',self.request())

    def test_today_exception_expires_at_day_boundary(self):
        self.agent.reply('以后别拿成绩开玩笑。')
        with patch('steady_companion.engine.date') as day:
            day.today.return_value.isoformat.return_value='2030-01-01'
            self.agent.reply('今天可以拿成绩开玩笑，平时还是别说。')
        with patch('steady_companion.engine.date') as day:
            day.today.return_value.isoformat.return_value='2030-01-02'
            self.agent.reply('成绩')
        self.assertIn('以后别拿成绩',self.context());self.assertNotIn('今天可以拿成绩',self.request())

    def test_explicit_restore_wording(self):
        self.agent.reply('以后别拿成绩开玩笑。');self.agent.reply('现在可以拿成绩开玩笑了。')
        self.agent.reply('以后还是别拿成绩开玩笑。')
        self.assertEqual(len(self.saved()),1);self.assertEqual(self.saved()[0]['meaning'],'deny')

    def test_new_scenario_offline_and_checked_fixture_path(self):
        from steady_companion.provider import Config
        scenario=Path(__file__).parents[1]/'evals/p11_story.json'
        out=self.root/'offline.json'
        with patch('steady_companion.cli.configured_client',side_effect=AssertionError('no network')),redirect_stdout(io.StringIO()):
            self.assertEqual(main(['eval-story','--scenario',str(scenario),'--output',str(out)]),0)
        self.assertEqual(json.loads(out.read_text())['actual_calls'],0)
        class CheckedFake:
            config=Config()
            def __init__(self): self.calls=0
            def complete(self,messages,**kwargs):
                self.calls+=1
                current=next(m['content'] for m in reversed(messages) if m['role']=='user' and m.get('name')!='context_data')
                if 'OUTPUT PIPELINE: GENERATION' in messages[-1]['content']:
                    content={'candidates':[{'id':k,'reply':'[离线夹具] 当前输入已接收。','evidence':[{'ref':'U0','quote':current[:60]}]} for k in ('A','B')],'selected_id':'A'}
                else: content={'approved_ids':['A'],'issues':[],'repair_hint':''}
                return {'choices':[{'message':{'role':'assistant','content':json.dumps(content,ensure_ascii=False)}}]}
        fake=CheckedFake();result=self.root/'fixture.jsonl'
        with patch('steady_companion.cli.configured_client',return_value=fake),redirect_stdout(io.StringIO()):
            code=main(['eval-story','--scenario',str(scenario),'--confirm-live','--capture-stages','--output',str(result),'--max-calls','80'])
        self.assertEqual(code,0)
        rows=[json.loads(line) for line in result.read_text().splitlines()]
        self.assertLessEqual(fake.calls,80);self.assertEqual(rows[-1]['usage']['calls'],fake.calls)
        self.assertTrue(any(row.get('result',{}).get('memory_update',{}).get('status')=='retracted' for row in rows))
        self.assertFalse(any('error' in row for row in rows))

    def test_known_revision_change_rejects_prepared_update_after_delete(self):
        self.agent.reply('我喜欢篮球。');rows=self.saved();rev=self.store.revision()
        plan=plan_update('我不喜欢篮球。',rows)
        self.agent.natural_memory.forget(1)
        with self.assertRaises(StaleRevisionError):
            self.agent.natural_memory.save_current('我不喜欢篮球。',rev,plan=plan)
        self.assertEqual(self.saved(),[])

    def test_schema_migration_rekeys_legacy_manual_correction_and_preserves_conflict(self):
        for conflict in (False,True):
            folder=self.root/('old-'+str(conflict));folder.mkdir();db=folder/'companion.sqlite3'
            with sqlite3.connect(db) as c:
                c.execute('CREATE TABLE p1_memories(id INTEGER PRIMARY KEY AUTOINCREMENT,kind TEXT,text TEXT,scope TEXT,source TEXT,evidence TEXT,created_at TEXT,updated_at TEXT,status TEXT,semantic_key TEXT UNIQUE,meaning TEXT)')
                raw='我喜欢游泳。'
                c.execute('INSERT INTO p1_memories VALUES(1,?,?,?,?,?,?,?,?,?,?)',('interest',raw,'old scope','user_command',raw,'2026','2026','active','interest:篮球:','stale'))
                if conflict:
                    c.execute('INSERT INTO p1_memories VALUES(2,?,?,?,?,?,?,?,?,?,?)',('interest',raw,'old scope','user_current_turn',raw,'2026','2026','active','interest:游泳:','positive游泳'))
            store=Store(folder);m=NaturalMemory(store);rows=m.list()
            self.assertEqual(rows[0]['text'],raw);self.assertEqual(rows[0]['evidence'],raw)
            self.assertNotEqual(rows[0]['semantic_key'],'interest:篮球:')
            self.assertEqual(len(rows),2 if conflict else 1)
            m.set_mode('auto');m.save_current('我不喜欢篮球。',store.revision())
            self.assertEqual(len(m.list()),3 if conflict else 2)

    def test_source_fields_from_previous_safe_note_not_reattached_after_sensitive_skip(self):
        self.agent.reply('以后别拿成绩开玩笑。')
        before=deepcopy(self.saved())
        self.agent.reply('以后别拿成绩开玩笑，因为我的 HIV 检测阳性。')
        self.assertEqual(self.saved(),before)
        self.assertNotIn('HIV',json.dumps(self.saved(),ensure_ascii=False))

    def test_same_message_synonyms_are_deduplicated_before_transaction(self):
        turn=self.agent.reply('我喜欢篮球，也喜欢打篮球，也喜欢游泳。')
        self.assertEqual(len(self.saved()),2)
        self.assertEqual(turn.memory_update['status'],'saved')
        self.assertIn('duplicate',turn.memory_update['outcomes'])

    def test_explicit_past_and_present_in_one_message_keep_separate_ranges(self):
        self.agent.reply('我以前喜欢篮球，现在不喜欢篮球。')
        self.assertEqual(len(self.saved()),2)
        self.assertEqual({r['semantic_key'] for r in self.saved()},{'interest:篮球:以前','interest:篮球:'})
        self.assertEqual({r['meaning'] for r in self.saved()},{'positive篮球','negative篮球'})
