"""P1.2 correctness only. Controlled clients do NOT measure semantic quality."""
from contextlib import redirect_stdout
from copy import deepcopy
import io,json,sqlite3,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
from steady_companion.engine import Conversation,ContextChangedError
from steady_companion.store import Store,StaleRevisionError
from steady_companion.natural_memory import NaturalMemory,plan_update
from steady_companion.semantic_memory import units,metadata,grounded_unit
from steady_companion.continuity import Continuity
from steady_companion.cli import execute_command
from steady_companion.provider import ProviderError


def response(obj):return {'choices':[{'message':{'role':'assistant','content':json.dumps(obj,ensure_ascii=False)}}], 'usage':{'prompt_tokens':11,'completion_tokens':7,'total_tokens':18}}
class Controlled:
 def __init__(self):self.calls=[];self.override=None;self.summary_override=None
 def complete(self,messages,**kwargs):
  self.calls.append((deepcopy(messages),kwargs));system=messages[0]['content']
  if system.startswith('MEMORY_CANDIDATE'):
   if self.override: return self.override(messages)
   data=json.loads(messages[-1]['content'])
   return response({'unit_ids':[e['id'] for e in data['eligible_units']]})
  if system.startswith('CONTINUITY_SELECTION'):
   if self.summary_override:return self.summary_override(messages)
   return response({'sources':json.loads(messages[-1]['content'])})
  return response('[synthetic offline response; not a model result]')

class LoopTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.store=Store(Path(t.name));self.client=Controlled();self.restart();self.command('/memory-mode auto')
 def restart(self):self.agent=Conversation(self.client,self.store,response_mode='direct',max_calls=80)
 def command(self,text):
  with redirect_stdout(io.StringIO()):execute_command(text,self.agent,self.store)
 def enable(self):self.command('/memory-assist on-confirm-cost')
 def say(self,text):return self.agent.reply(text)
 def rows(self):return self.agent.natural_memory.list()
 def context(self):return '\n'.join(m['content'] for m in self.client.calls[-1][0] if m.get('name')=='context_data')
 def test_default_off_and_explicit_cost_ack(self):
  self.say('我喜欢远古历史小说。');self.assertEqual(self.rows(),[]);self.assertEqual(self.agent.usage.memory_calls,0)
  with self.assertRaises(ValueError):self.command('/memory-assist on')
  self.enable();self.say('我喜欢远古历史小说。');self.assertEqual(len(self.rows()),1);self.assertEqual(self.agent.usage.memory_calls,1)
 def test_optin_persists_but_manual_still_does_not_save(self):
  self.enable();self.command('/memory-mode manual');self.say('我喜欢远古历史小说。');self.assertEqual(self.rows(),[]);self.assertEqual(self.agent.usage.memory_calls,0)
  self.restart();self.assertTrue(self.agent.natural_memory.feature('memory_assist'));self.assertEqual(self.agent.natural_memory.mode,'manual')
 def test_known_rule_no_additional_call_even_when_enabled(self):
  self.enable();self.say('我喜欢篮球。');self.say('你好');self.assertEqual(self.agent.usage.calls,2);self.assertEqual(self.agent.usage.memory_calls,0)
 def test_arbitrary_names_no_name_whitelist(self):
  self.enable()
  for label in ('小强','橙色邮差','Zeta17','风铃'):
   self.say(f'我把桌上那盆薄荷叫{label}，名字就是随便起的。')
   self.assertEqual(len(self.rows()),1);self.assertEqual(metadata(self.rows()[0])['value'],label)
  self.say('它今天又蔫了。');self.assertIn('风铃',self.context())
  self.restart();self.say('我那盆薄荷叫什么？');self.assertIn('风铃',self.context())
 def test_interest_exclusion_and_paraphrase_after_restart(self):
  self.enable();self.say('平时挺爱看推理小说的，不过太血腥的我不喜欢。')
  row=self.rows()[0];self.assertEqual(metadata(row)['condition'],'太血腥的我不喜欢')
  self.say('今天的面包很好吃');self.restart();self.say('你还记得我偏爱哪类故事吗？')
  self.assertIn('太血腥的我不喜欢',self.context());self.assertNotIn('人格',row['text'])
 def test_time_attribute_and_condition_do_not_collapse(self):
  self.enable()
  for text in ['我挺喜欢甜咖啡。','最近在戒糖，点咖啡时都选无糖。','三年前喜欢甜咖啡。','我喜欢甜咖啡，不过太苦的我不喜欢。']:self.say(text)
  self.assertEqual(len(self.rows()),4);self.assertEqual(len({r['semantic_key'] for r in self.rows()}),4)
  self.say('帮我想想今天点什么口味。');self.assertIn('戒糖',self.context());self.assertNotIn('三年前',self.context())
 def test_multiple_facts_and_explicit_negation(self):
  self.enable();self.say('我喜欢古代历史小说；我不喜欢深海纪录电影。')
  self.assertEqual({r['meaning'] for r in self.rows()},{'positive','negative'})
  self.assertTrue(all(('；' not in r['evidence']) for r in self.rows()))
 def test_reports_quotes_uncertainty_fiction_skip(self):
  self.enable()
  for text in ['朋友说他喜欢古代历史小说。','“我喜欢古代历史小说”。','我可能喜欢古代历史小说。','假设我喜欢古代历史小说。','我不一定喜欢古代历史小说。']:
   self.say(text);self.assertEqual(self.rows(),[])
  self.assertEqual(self.agent.usage.memory_calls,0)
 def test_sensitive_mixed_unknown_appended_and_naming(self):
  self.enable()
  for text in ['我喜欢历史小说，我的 HIV 结果是阳性。','我把桌上那盆薄荷叫HIV阳性。','以后聊我的检查报告时请别开玩笑。','我喜欢历史小说；我的未知 ABC123 结果别提。']:
   self.say(text);self.assertEqual(self.rows(),[])
  self.assertNotIn(b'HIV',self.store.db_path.read_bytes())
 def test_malicious_forged_source_cannot_commit(self):
  self.enable();self.client.override=lambda m:response({'candidates':[{'evidence':'我喜欢历史小说','operation':'upsert','target_id':None}]})
  t=self.say('我喜欢植物摄影。');self.assertEqual(self.rows(),[]);self.assertIn('skipped',t.architecture['extraction_status'])
 def test_forged_target_and_mutation_op_blocked(self):
  self.enable();self.say('我喜欢篮球。')
  for target,op in ((1,'upsert'),(999,'upsert'),(None,'delete')):
   self.client.override=lambda m,t=target,o=op:response({'candidates':[{'evidence':'我喜欢远古历史小说','operation':o,'target_id':t}]})
   self.say('我喜欢远古历史小说。');self.assertEqual(len(self.rows()),1);self.assertEqual(self.rows()[0]['meaning'],'positive篮球')
 def test_limiting_clause_cannot_be_omitted_from_evidence(self):
  self.enable();self.client.override=lambda m:response({'candidates':[{'evidence':'平时挺爱看推理小说的','operation':'upsert','target_id':None}]})
  self.say('平时挺爱看推理小说的，不过太血腥的我不喜欢。');self.assertEqual(self.rows(),[])
 def test_extra_source_annotation_and_oversized_array_rejected(self):
  self.enable()
  for payload in [{'candidates':[{'evidence':'我喜欢历史小说','operation':'upsert','target_id':None,'source':'trusted'}]}, {'candidates':[{}]*4}]:
   self.client.override=lambda m,p=payload:response(p);self.say('我喜欢历史小说。');self.assertEqual(self.rows(),[])
 def test_semantic_evidence_checked_again_at_transaction(self):
  self.enable();text='我喜欢历史小说。';events=units(text);plan=plan_update(text,[],candidates=events)
  with self.assertRaises(ValueError):self.agent.natural_memory.save_current('我喜欢植物摄影。',self.store.revision(),plan=plan)
  self.assertEqual(self.rows(),[])
 def test_revoked_permission_checked_inside_transaction(self):
  self.enable();text='我喜欢历史小说。';plan=plan_update(text,[],candidates=units(text));self.agent.natural_memory.set_feature('memory_assist',False)
  with self.assertRaises(ValueError):self.agent.natural_memory.save_current(text,self.store.revision(),plan=plan)
  self.assertEqual(self.rows(),[])
 def test_delete_during_candidate_call_discards_everything(self):
  self.enable();self.say('我喜欢篮球。')
  def mutate(m):
   self.agent.natural_memory.forget();return response({'candidates':[{'evidence':'我喜欢历史小说','operation':'upsert','target_id':None}]})
  self.client.override=mutate
  with self.assertRaises(ContextChangedError):self.say('我喜欢历史小说。')
  self.assertEqual(self.rows(),[]);self.assertFalse(self.agent._p1_context);self.assertFalse(self.agent.continuity.summary)
 def test_error_timeout_fallback_and_independent_budget(self):
  self.enable()
  def fail(m):raise TimeoutError('SYNTHETIC_PRIVATE_EXCEPTION')
  self.client.override=fail
  for i in range(6):
   turn=self.say('我喜欢历史小说。');self.assertTrue(turn.text);self.assertNotIn('SYNTHETIC_PRIVATE',str(turn))
  self.assertEqual(self.agent.usage.memory_calls,4);self.assertEqual(self.agent.usage.calls,10);self.assertEqual(self.rows(),[])
 def test_shared_budget_reserves_reply(self):
  self.enable();self.agent.max_calls=1;self.say('我喜欢历史小说。')
  self.assertEqual(self.agent.usage.calls,1);self.assertEqual(self.agent.usage.memory_calls,0);self.assertEqual(self.rows(),[])
 def test_cancelled_extraction_no_write_or_retry(self):
  self.enable();self.client.override=lambda m:(_ for _ in ()).throw(KeyboardInterrupt())
  with self.assertRaises(KeyboardInterrupt):self.say('我喜欢历史小说。')
  self.say('你好');self.assertEqual(self.rows(),[]);self.assertEqual(self.agent.usage.memory_calls,1)
 def test_auxiliary_input_bounded_no_assistant_drafts_or_whole_store(self):
  self.enable();self.say('我喜欢篮球。');self.agent.history[-1]['content']='ASSISTANT_ONLY_MARKER'
  self.say('我喜欢历史小说。')
  req=next(m for m,k in self.client.calls if m[0]['content'].startswith('MEMORY_CANDIDATE'))
  self.assertNotIn('ASSISTANT_ONLY',str(req));self.assertNotIn('篮球',str(req));self.assertLess(len(str(req)),2500)
  kwargs=next(k for m,k in self.client.calls if m[0]['content'].startswith('MEMORY_CANDIDATE'))
  self.assertEqual(kwargs['timeout'],20.0);self.assertEqual(kwargs['max_tokens'],1200)
 def test_semantic_write_failure_repeat_and_restart(self):
  self.enable();self.say('我喜欢历史小说。')
  with patch.object(self.agent.natural_memory,'save_current',side_effect=sqlite3.OperationalError('private')):t=self.say('我不喜欢历史小说。')
  self.assertEqual(t.memory_update['status'],'not_saved');self.assertIn('我不喜欢历史小说',self.context())
  self.say('我不喜欢历史小说。');self.restart();self.assertEqual(self.rows()[0]['meaning'],'negative')
 def test_semantic_delete_propagates_summary_referent_and_overlays(self):
  self.enable();self.say('我把桌上那盆薄荷叫风铃。')
  for _ in range(9):self.say('今天散步看到了云')
  self.assertTrue(self.agent.continuity.summary)
  self.command('/auto-forget all');self.assertEqual(self.rows(),[]);self.assertFalse(self.agent.continuity.summary);self.assertFalse(self.agent.continuity.sources)
  self.restart();self.say('我给薄荷取的名字还记得吗？');self.assertNotIn('风铃',str(self.client.calls[-1]));self.assertNotIn('风铃'.encode(),self.store.db_path.read_bytes())
 def test_cross_user_isolation(self):
  self.enable();self.say('我把桌上那盆薄荷叫风铃。')
  with tempfile.TemporaryDirectory() as d:
   b=Conversation(self.client,Store(Path(d)),response_mode='direct');b.reply('薄荷叫什么？');self.assertNotIn('风铃',str(self.client.calls[-1]));self.assertFalse(b.natural_memory.feature('memory_assist'))
 def test_migration_existing_schema_settings_and_data_preserved(self):
  self.say('我喜欢篮球。');rows=self.rows();rev=self.store.revision();self.restart()
  self.assertEqual(self.rows(),rows);self.assertEqual(self.store.revision(),rev);self.assertFalse(self.agent.natural_memory.feature('memory_assist'))
  self.assertEqual(self.agent.natural_memory.eviction_candidates(),[])
 def test_read_failure_excludes_summary_and_durable_records(self):
  self.enable();self.say('我把桌上那盆薄荷叫风铃。')
  for _ in range(9):self.say('今天散步看到了云')
  with patch.object(self.agent.natural_memory,'list',side_effect=sqlite3.OperationalError('private')):
   t=self.say('它之前叫什么？')
  self.assertTrue(t.memory_update['read_degraded']);self.assertNotIn('风铃',str(self.client.calls[-1]));self.assertFalse(self.agent.continuity.summary)
 def test_applicability_pause_and_topic_shift(self):
  self.say('以后别拿成绩开玩笑。');self.say('聊聊电影');self.assertNotIn('以后别拿成绩',self.context())
  self.say('成绩怎么样');self.assertIn('以后别拿成绩',self.context())
  self.say('先不分析了');self.assertNotIn('以后别拿成绩',self.context())
 def test_short_context_no_summary_call(self):
  self.command('/context-summary on-confirm-cost')
  for _ in range(4):self.say('今天云很好看')
  self.assertEqual(self.agent.usage.summary_calls,0);self.assertFalse(self.agent.continuity.summary)
 def test_capacity_summary_is_verbatim_user_only_bounded_and_volatile(self):
  self.command('/context-summary on-confirm-cost');self.say('今天先别分析，明天继续聊植物。')
  for i in range(24):self.say('今天散步看到了云'+str(i))
  self.assertEqual(self.agent.usage.summary_calls,1);self.assertLessEqual(len(self.agent.history),16);self.assertLessEqual(len(self.agent.continuity.sources),12)
  quotes=[r['quote'] for r in self.agent.continuity.summary];self.assertIn('今天先别分析，明天继续聊植物。',quotes);self.assertNotIn('synthetic offline response',str(quotes))
  self.assertEqual(self.rows(),[]);self.restart();self.assertFalse(self.agent.continuity.summary)
 def test_forged_summary_quote_falls_back_without_persisting(self):
  self.command('/context-summary on-confirm-cost');self.client.summary_override=lambda m:response({'sources':[{'id':1,'quote':'用户是某种人格，已经诊断'}]})
  for i in range(9):self.say('今天读了一页书'+str(i))
  self.assertEqual(self.agent.continuity.status,'summary_failed_extractive_fallback');self.assertNotIn('人格',str(self.agent.continuity.summary));self.assertEqual(self.rows(),[])
 def test_summary_deletion_race_never_restores_local_sources(self):
  self.command('/context-summary on-confirm-cost');self.say('我喜欢篮球。')
  def mutate(m):self.agent.natural_memory.forget();return response({'sources':json.loads(m[-1]['content'])})
  self.client.summary_override=mutate
  for i in range(8):self.say('今天散步'+str(i))
  self.assertEqual(self.rows(),[]);self.assertFalse(self.agent.continuity.summary);self.assertFalse(self.agent.continuity.sources)
 def test_summary_budget_exhaustion_has_no_extra_call(self):
  self.command('/context-summary on-confirm-cost');self.agent.max_calls=9
  for i in range(9):self.say('今天读书'+str(i))
  self.assertEqual(self.agent.usage.calls,9);self.assertEqual(self.agent.usage.summary_calls,0);self.assertIn('fallback',self.agent.continuity.status)
 def test_wipe_resets_auxiliary_optin(self):
  self.enable();self.agent.natural_memory.set_feature('context_summary',True);self.store.wipe_all();self.restart()
  self.assertFalse(self.agent.natural_memory.feature('memory_assist'));self.assertFalse(self.agent.natural_memory.feature('context_summary'))

class SummaryValidationTests(unittest.TestCase):
 def test_ids_quotes_count_and_deleted_source(self):
  c=Continuity();r=c.add_source('今天别讨论这个，明天再说。',{3})
  for data in [{'sources':[{'id':999,'quote':r['quote']}]},{'sources':[{'id':r['id'],'quote':'AI猜测'}]}, {'sources':[{'id':r['id'],'quote':r['quote']}]*13}, {'sources':[{'id':r['id'],'quote':r['quote'],'memory_id':3}]}]:
   with self.assertRaises(ValueError):c.validate(response(data))
  c.invalidate()
  with self.assertRaises(ValueError):c.validate(response({'sources':[{'id':r['id'],'quote':r['quote']}]}))

class ScenarioTests(unittest.TestCase):
 def test_all_30_cards_3_negative_controls_and_pilot_actual_store_checks(self):
  from steady_companion.story_eval import validate_story,check_step
  root=Path(__file__).parents[1]/'steady_companion/data'
  passed=total=0
  for file in sorted(root.glob('p12_*.json')):
   with self.subTest(scene=file.name),tempfile.TemporaryDirectory() as d:
    story=validate_story(file);store=Store(Path(d));client=Controlled();agent=Conversation(client,store,response_mode='direct',max_calls=80)
    for name,enabled in story.get('features',{}).items():agent.natural_memory.set_feature(name,enabled)
    agent.clear()
    for step in story['steps']:
     result={}
     if 'command' in step:
      with redirect_stdout(io.StringIO()):execute_command(step['command'],agent,store)
     elif 'new_session' in step:
      usage=agent.usage;agent=Conversation(client,store,response_mode='direct',max_calls=80);agent.usage=usage
     elif 'inject' in step:agent.history[-1]=dict(step['inject'])
     else:
      from dataclasses import asdict
      result=asdict(agent.reply(step['user']))
     checks=check_step(step,agent,result);passed+=checks['passed'];total+=checks['total']
     self.assertEqual(checks['passed'],checks['total'],(file.name,step,checks,agent.natural_memory.list()))
  self.assertGreater(total,40);self.assertEqual(passed,total)
 def test_pilot_checked_requests_budget_trace_and_no_expectations_to_model(self):
  from steady_companion.cli import main
  from steady_companion.provider import Config
  class Checked(Controlled):
   config=Config()
   def complete(self,messages,**kw):
    if messages[0]['content'].startswith(('MEMORY_CANDIDATE','CONTINUITY_SELECTION')):return super().complete(messages,**kw)
    self.calls.append((deepcopy(messages),kw))
    current=next(m['content'] for m in reversed(messages) if m['role']=='user' and m.get('name')!='context_data')
    if 'OUTPUT PIPELINE: GENERATION' in messages[-1]['content']:
     return response({'candidates':[{'id':k,'reply':'[离线夹具] 收到当前内容。','evidence':[{'ref':'U0','quote':current[:50]}]} for k in ('A','B')],'selected_id':'A'})
    return response({'approved_ids':['A'],'issues':[],'repair_hint':''})
  with tempfile.TemporaryDirectory() as d:
   fake=Checked();output=Path(d)/'result.jsonl'
   with patch('steady_companion.cli.configured_client',return_value=fake),redirect_stdout(io.StringIO()):
    code=main(['eval-pilot','--confirm-live','--capture-stages','--output',str(output)])
   self.assertEqual(code,0);rows=[json.loads(line) for line in output.read_text().splitlines()]
   self.assertEqual(rows[-1]['usage']['calls'],16);self.assertEqual(len(fake.calls),16);self.assertEqual(rows[-1]['usage']['memory_calls'],2)
   self.assertEqual(sum('result' in r for r in rows),7)
   self.assertNotIn('assertions',str(fake.calls));self.assertNotIn('failure_class',str(fake.calls))
 def test_pilot_offline_never_configures_client_and_caps(self):
  from steady_companion.cli import main
  with tempfile.TemporaryDirectory() as d,patch('steady_companion.cli.configured_client',side_effect=AssertionError('no API')),redirect_stdout(io.StringIO()):
   p=Path(d)/'offline.json';self.assertEqual(main(['eval-pilot','--output',str(p)]),0);self.assertEqual(json.loads(p.read_text())['actual_calls'],0)
   self.assertEqual(main(['eval-pilot','--max-calls','33','--output',str(Path(d)/'bad.json')]),1)
 def test_mixed_classic_semantic_identity_never_duplicates_legacy_fact(self):
  with tempfile.TemporaryDirectory() as d:
   agent=Conversation(Controlled(),Store(Path(d)),response_mode='direct');agent.natural_memory.set_mode('auto');agent.natural_memory.set_feature('memory_assist',True)
   agent.reply('我喜欢咖啡。');agent.reply('我喜欢咖啡；我喜欢航海故事。');agent.reply('我不喜欢咖啡。')
   rows=agent.natural_memory.list();self.assertEqual(len(rows),2);self.assertEqual(next(r for r in rows if r['semantic_key']=='interest:咖啡:')['meaning'],'negative咖啡')
 def test_open_interest_alias_correction_keeps_identity(self):
  with tempfile.TemporaryDirectory() as d:
   agent=Conversation(Controlled(),Store(Path(d)),response_mode='direct');agent.natural_memory.set_mode('auto');agent.natural_memory.set_feature('memory_assist',True)
   agent.reply('我喜欢看航海故事。');agent.reply('我不喜欢航海故事。');rows=agent.natural_memory.list();self.assertEqual(len(rows),1);self.assertEqual(rows[0]['meaning'],'negative')

class AdditionalBoundaryTests(unittest.TestCase):
 def test_forged_scope_change_cannot_delete_unrelated_target(self):
  from steady_companion.memory_language import parse_events
  with tempfile.TemporaryDirectory() as d:
   store=Store(Path(d));memory=NaturalMemory(store);memory.set_mode('auto');memory.save_current('我喜欢篮球。',store.revision());row=memory.list()[0]
   e=parse_events('以后只在聊成绩时别开玩笑，其他话题可以。')[0]
   plan={'changes':[{'action':'retract','old':row,'key':row['semantic_key'],'event':e}],'outcomes':['retract']}
   with self.assertRaises(ValueError):memory.save_current(e['text'],store.revision(),plan=plan)
   self.assertEqual(memory.list(),[row])
 def test_temporary_summary_quote_expires_without_reauthorizing(self):
  from datetime import date
  c=Continuity();c.compact([{'role':'user','content':'今天可以开玩笑。'}],{})
  for row in c.summary:row['source_day']='2000-01-01'
  self.assertFalse(c.context('之前可以吧？'));self.assertFalse(c.sources)
 def test_tomorrow_thread_expires_but_persistent_boundary_does_not(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d));m=NaturalMemory(s);m.set_mode('auto');m.save_current('明天再聊篮球。',s.revision());m.save_current('以后别开玩笑。',s.revision())
   with s._connection() as con:con.execute("UPDATE p1_memories SET created_at='2000-01-01T00:00:00Z'")
   result=m.select('篮球');self.assertEqual(len(result),1);self.assertEqual(result[0]['kind'],'preference')

class MixedOperationTests(unittest.TestCase):
 def test_temporary_and_new_fact_in_one_semantic_selection(self):
  with tempfile.TemporaryDirectory() as d:
   a=Conversation(Controlled(),Store(Path(d)),response_mode='direct');a.natural_memory.set_mode('auto');a.natural_memory.set_feature('memory_assist',True)
   a.reply('以后别开玩笑。');a.reply('今天可以开玩笑；我喜欢航海故事。')
   self.assertEqual(len(a.natural_memory.list()),2);self.assertEqual(set(a._temporary_overrides),{'boundary:joke:*'})
   self.assertTrue(all(k.startswith('boundary:') for k in a._temporary_overrides))
 def test_prepared_safe_classic_fragment_cannot_bypass_sensitive_whole_input(self):
  with tempfile.TemporaryDirectory() as d:
   s=Store(Path(d));m=NaturalMemory(s);m.set_mode('auto');p=plan_update('我喜欢篮球。',[])
   with self.assertRaises(ValueError):m.save_current('我喜欢篮球。我的 HIV 结果是阳性。',s.revision(),plan=p)
   self.assertEqual(m.list(),[])

def make_comparison_baseline(path):
 # Minimal synthetic package envelope, only for mocked comparison-driver tests.
 # No legacy code is executed by these tests or bundled in the new delivery.
 import zipfile
 root=Path(__file__).parents[1]
 with zipfile.ZipFile(path,'w') as archive:
  archive.writestr('steady-companion-agent/steady_companion/__init__.py','__version__ = "0.4.2"\n')
  for rel in ('core.md','roles/R-A.json'):
   archive.writestr('steady-companion-agent/steady_companion/skill/'+rel,(root/'steady_companion/skill'/rel).read_bytes())
 return path

class ComparisonDriverTests(unittest.TestCase):
 def test_two_isolated_conditions_share_cap_and_fixed_config(self):
  from steady_companion.cli import main
  from steady_companion.provider import Config
  from types import SimpleNamespace
  root=Path(__file__).parents[1];limits=[];locations=[]
  def fake_run(command,**kwargs):
   limits.append(int(command[command.index('--max-calls')+1]));locations.append(kwargs['env']['PYTHONPATH'])
   out=Path(command[command.index('--output')+1]);count=12 if len(limits)==1 else 9
   out.write_text(json.dumps({'usage':{'calls':count}})+'\n');return SimpleNamespace(returncode=0)
  with tempfile.TemporaryDirectory() as d,patch('steady_companion.compare_eval.subprocess.run',side_effect=fake_run),patch('steady_companion.compare_eval.Config.from_env',return_value=Config(api_key='SYNTHETIC_TEST_TOKEN')),redirect_stdout(io.StringIO()):
   out=Path(d)/'compare.json'
   code=main(['eval-compare','--baseline',str(make_comparison_baseline(Path(d)/'baseline.zip')),'--confirm-live','--output',str(out)])
   self.assertEqual(code,0);report=json.loads(out.read_text());self.assertEqual(report['actual_calls'],21);self.assertEqual(limits,[32,20]);self.assertNotEqual(locations[0],locations[1]);self.assertNotIn('SYNTHETIC_TEST_TOKEN',out.read_text())
 def test_comparison_stops_after_failed_condition_no_retry(self):
  from steady_companion.cli import main
  from steady_companion.provider import Config
  from types import SimpleNamespace
  root=Path(__file__).parents[1]
  def fail(command,**kwargs):
   Path(command[command.index('--output')+1]).write_text(json.dumps({'usage':{'calls':2},'error':'synthetic'})+'\n')
   return SimpleNamespace(returncode=1)
  with tempfile.TemporaryDirectory() as d,patch('steady_companion.compare_eval.subprocess.run',side_effect=fail) as run,patch('steady_companion.compare_eval.Config.from_env',return_value=Config(api_key='SYNTHETIC')),redirect_stdout(io.StringIO()):
   self.assertEqual(main(['eval-compare','--baseline',str(make_comparison_baseline(Path(d)/'baseline.zip')),'--confirm-live','--output',str(Path(d)/'compare.json')]),1);self.assertEqual(run.call_count,1)
