"""P1.2.1 diagnostic fault injection; never real API requests or private data."""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import asdict
import io,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from steady_companion.cli import main
from steady_companion.engine import Conversation,ContextChangedError
from steady_companion.store import Store
from steady_companion.provider import Config,ProviderError,Client
from steady_companion.diagnostics import Trace,capture_response,safe_log
from steady_companion.pipeline import run_pipeline
from test_p12 import Controlled,response

NAME='我把桌上那盆薄荷叫小强，名字就是随便起的。'

def generated(current):return {'candidates':[{'id':k,'reply':'[合成夹具] 收到当前内容。','evidence':[{'ref':'U0','quote':current[:50]}]} for k in ('A','B')],'selected_id':'A'}

def reviewed():return {'approved_ids':['A'],'issues':[],'repair_hint':''}

class FaultClient(Controlled):
 config=Config(api_key='SYNTHETIC_SECRET_TOKEN')
 def __init__(self,fault=None):super().__init__();self.fault=fault;self.gens=0;self.reviews=0
 def complete(self,messages,**kw):
  if messages[0]['content'].startswith(('MEMORY_CANDIDATE','CONTINUITY_SELECTION')):return super().complete(messages,**kw)
  self.calls.append((deepcopy(messages),kw))
  current=next(m['content'] for m in reversed(messages) if m['role']=='user' and m.get('name')!='context_data')
  stage='generation' if 'OUTPUT PIPELINE: GENERATION' in messages[-1]['content'] else 'review'
  if stage=='generation':self.gens+=1;number=self.gens
  else:self.reviews+=1;number=self.reviews
  value=generated(current) if stage=='generation' else reviewed()
  if self.fault:
   changed=self.fault(stage,number,value)
   if changed is not None:return changed
  return response(value)

class DiagnosticTests(unittest.TestCase):
 def setUp(self):
  t=tempfile.TemporaryDirectory();self.addCleanup(t.cleanup);self.root=Path(t.name)
 def agent(self,client=None,**kw):
  a=Conversation(client or FaultClient(),Store(self.root/'data'),**kw);a.natural_memory.set_mode('auto');a.natural_memory.set_feature('memory_assist',True);return a
 def run_scene(self,client,extra=()):
  output=self.root/('result'+str(len(list(self.root.glob('result*'))))+'.jsonl');terminal=io.StringIO()
  with patch('steady_companion.cli.configured_client',return_value=client),redirect_stdout(terminal):
   code=main(['eval-diagnose','--confirm-live','--capture-stages','--output',str(output),*extra])
  return code,[json.loads(x) for x in output.read_text().splitlines()],terminal.getvalue(),output
 def test_generation_errors_distinct_and_completed_memory_retained(self):
  def missing(v):v.pop('selected_id');return response(v)
  def count(v):v['candidates']=v['candidates'][:1];return response(v)
  def fields(v):v['candidates'][0].pop('reply');return response(v)
  def reference(v):
   for c in v['candidates']:c['evidence'][0]['ref']='U999'
   return response(v)
  def quote(v):
   for c in v['candidates']:c['evidence'][0]['quote']='NOT_IN_USER'
   return response(v)
  def visible(v):v['candidates'][0]['reply']='<analysis>PRIVATE</analysis>';return response(v)
  def truncated(v):r=response(v);r['choices'][0]['finish_reason']='length';return r
  variants={'json_parse':lambda v:response('{bad JSON'), 'candidate_fields':missing,'candidate_count':count,
            'evidence_reference':reference,'evidence_quote':quote,'visible_text':visible,'response_truncated':truncated}
  # response() encodes strings too: explicitly construct malformed raw content.
  variants['json_parse']=lambda v:{'choices':[{'message':{'role':'assistant','content':'{"candidates":'}}],'usage':{'prompt_tokens':11,'completion_tokens':7,'total_tokens':18}}
  for expected,mutate in variants.items():
   with self.subTest(error=expected):
    client=FaultClient(lambda stage,n,v:mutate(v) if stage=='generation' else None)
    code,rows,terminal,out=self.run_scene(client);r=rows[-1]
    recovered_attempt = expected in ('json_parse','candidate_fields','candidate_count')
    stage='repair_generation' if recovered_attempt else 'generation';calls=3 if recovered_attempt else 2
    self.assertEqual(code,1);self.assertEqual(r['failed_stage'],stage);self.assertEqual(r['error_code'],'no_eligible_candidates' if expected.startswith('evidence_') else expected)
    if expected.startswith('evidence_'):
     rejected=r['diagnostics']['stages'][1]['candidate_validation']['rejected_candidates']
     self.assertEqual([c['code'] for c in rejected],[expected,expected])
    self.assertIn('memory',r['diagnostics']['completed_stages']);self.assertNotIn('generation',r['diagnostics']['completed_stages'])
    self.assertEqual(sum(s['calls'] for s in r['diagnostics']['stages']),calls);self.assertEqual(r['step_usage']['calls'],calls);self.assertEqual(r['usage']['calls'],calls);self.assertEqual(r['usage']['total_tokens'],18*calls)
    state=r['synthetic_state'];self.assertEqual(state['memory_progress']['validated_name_candidates'],1);self.assertEqual(state['memory_progress']['validated_candidate_count'],1);self.assertEqual(state['memory_progress']['candidate_kinds'],['shared'])
    self.assertFalse(state['memory_progress']['commit_attempted']);self.assertEqual(state['durable']['records'],[]);self.assertEqual(len(state['session_overrides']),1)
    self.assertNotIn('result',r);self.assertIn('阶段='+stage,terminal);self.assertIn('no_eligible_candidates' if expected.startswith('evidence_') else expected,terminal);self.assertIn(str(calls)+'/8',terminal);self.assertIn(str(out),terminal)
    self.assertEqual(client.reviews,0);self.assertNotIn('PRIVATE',out.read_text())
 def test_review_failure_retains_completed_generation_capture(self):
  client=FaultClient(lambda stage,n,v:response({'approved_ids':['UNKNOWN'],'issues':[],'repair_hint':''}) if stage=='review' else None)
  code,rows,terminal,out=self.run_scene(client);r=rows[-1]
  self.assertEqual(r['failed_stage'],'review');self.assertEqual(r['error_code'],'review_reference');self.assertEqual(r['usage']['calls'],3)
  self.assertEqual(r['diagnostics']['completed_stages'],['memory','generation']);self.assertEqual(client.gens,1)
  self.assertIn('response_capture',r['diagnostics']['stages'][1]);self.assertFalse(r['synthetic_state']['memory_progress']['commit_attempted'])
 def test_repair_generation_and_review_stages_remain_distinct(self):
  for target in ('repair_generation','repair_review'):
   def fault(stage,n,v):
    if stage=='review' and n==1:return response({'approved_ids':[],'issues':[],'repair_hint':''})
    if (target=='repair_generation' and stage=='generation' and n==2) or (target=='repair_review' and stage=='review' and n==2):return response({})
   client=FaultClient(fault);code,rows,_,_=self.run_scene(client);r=rows[-1]
   self.assertEqual(r['failed_stage'],target);self.assertEqual(r['usage']['calls'],4 if target=='repair_generation' else 5)
   self.assertIn('review',r['diagnostics']['completed_stages']);self.assertFalse(r['synthetic_state']['durable']['records'])
 def test_cancel_timeout_http_transport_are_separate_no_retry(self):
  for exc,code in [(KeyboardInterrupt(),'cancelled'),(TimeoutError('PRIVATE'),'timeout'),(ConnectionError('PRIVATE'),'transport'),(ProviderError('PRIVATE',code='http'),'http')]:
   def fault(stage,n,v,e=exc):raise e
   client=FaultClient(fault);rc,rows,terminal,out=self.run_scene(client)
   self.assertEqual(rc,1);self.assertEqual(rows[-1]['error_code'],code);self.assertEqual(rows[-1]['usage']['calls'],2);self.assertEqual(client.gens,1)
   self.assertEqual(rows[-1]['usage']['unreported_calls'],1);self.assertNotIn('PRIVATE',out.read_text()+terminal)
 def test_context_change_has_own_code_and_clears_candidate(self):
  client=FaultClient();a=self.agent(client)
  def mutate(stage,n,v):a.natural_memory.forget();a.natural_memory.set_mode('manual');return response(v)
  client.fault=mutate
  with self.assertRaises(ContextChangedError):a.reply(NAME)
  self.assertEqual(a.last_diagnostic['failure'],{'stage':'generation','code':'context_invalidated'});self.assertFalse(a._memory_overrides);self.assertFalse(a._p1_context)
 def test_budget_stops_before_generation_without_mislabeling_memory(self):
  a=self.agent(max_calls=1)
  with self.assertRaises(ProviderError):a.reply(NAME)
  self.assertEqual(a.last_diagnostic['failure'],{'stage':'generation','code':'budget'});self.assertEqual(a.usage.calls,0)
 def test_commit_failure_is_nonfatal_and_in_diagnostics(self):
  a=self.agent()
  with patch.object(a.natural_memory,'save_current',side_effect=sqlite3.OperationalError('PRIVATE')):turn=a.reply(NAME)
  self.assertTrue(turn.text);self.assertEqual(turn.memory_update['status'],'not_saved')
  self.assertEqual(a.last_diagnostic['failure'],{'stage':'commit','code':'storage'});self.assertTrue(a.last_diagnostic['memory_progress']['commit_attempted'])
  self.assertEqual(a.last_diagnostic['memory_progress']['commit_status'],'not_saved');self.assertEqual(a.usage.calls,3)
 def test_memory_validation_failure_reported_without_stopping_reply(self):
  client=FaultClient();client.override=lambda m:response({'candidates':[{'evidence':'FORGED','operation':'upsert','target_id':None}]});a=self.agent(client)
  self.assertTrue(a.reply(NAME).text);self.assertEqual(a.last_diagnostic['failure'],{'stage':'memory','code':'memory_validation'});self.assertEqual(a.synthetic_state()['durable']['records'],[])
 def test_summary_validation_failure_recorded_in_successful_turn(self):
  client=FaultClient();client.summary_override=lambda m:response({'sources':[{'id':999,'quote':'FORGED'}]});a=self.agent(client,max_calls=40);a.natural_memory.set_feature('context_summary',True)
  for i in range(9):a.reply('今天散步看云'+str(i))
  self.assertEqual(a.last_diagnostic['failure'],{'stage':'summary','code':'summary_validation'});self.assertEqual(a.usage.summary_calls,1)
 def test_capture_off_has_no_candidates_and_no_persistence(self):
  a=self.agent(FaultClient(lambda stage,n,v:response({}) if stage=='generation' else None),capture_stages=False)
  with self.assertRaises(ProviderError):a.reply(NAME)
  raw=json.dumps(a.last_diagnostic,ensure_ascii=False)
  self.assertNotIn('response_capture',raw);self.assertNotIn('小强',raw);self.assertNotIn('薄荷',raw);self.assertFalse(a.natural_memory.list())
 def test_sensitive_capture_whitelist_rejects_reasoning_keys_headers_and_secret(self):
  payload=response({'candidates':[{'id':'A','reply':'safe visible','evidence':[{'ref':'U0','quote':'safe quote'}], 'reasoning_content':'PRIVATE_REASONING','headers':{'Authorization':'PRIVATE_HEADER'}},{'id':'B','reply':'<analysis>PRIVATE_ANALYSIS</analysis>'},{'id':'C','reply':'SYNTHETIC_SECRET_TOKEN'}], 'analysis':'TOP_PRIVATE'})
  payload['choices'][0]['message']['reasoning']='PROVIDER_PRIVATE'
  captured=safe_log(capture_response(payload,('SYNTHETIC_SECRET_TOKEN',)))
  raw=json.dumps(captured);self.assertIn('safe visible',raw)
  for secret in ('PRIVATE_REASONING','PRIVATE_HEADER','PRIVATE_ANALYSIS','SYNTHETIC_SECRET_TOKEN','TOP_PRIVATE','PROVIDER_PRIVATE'):self.assertNotIn(secret,raw)
 def test_malformed_payload_only_size_and_structure_no_raw_fragment(self):
  payload={'choices':[{'message':{'content':'{"reply":"<analysis>PRIVATE_STUFF'},'finish_reason':'length'}]}
  result=capture_response(payload);self.assertEqual(result['finish_reason'],'length');self.assertEqual(result['content_chars'],len(payload['choices'][0]['message']['content']));self.assertNotIn('PRIVATE',str(result));self.assertEqual(result['capture'],'structure_only')
 def test_successful_short_scene_has_exact_record_assertions_and_five_calls(self):
  client=FaultClient();code,rows,_,out=self.run_scene(client)
  self.assertEqual(code,0);self.assertEqual(rows[-1]['usage']['calls'],5);self.assertEqual(rows[-1]['usage']['total_tokens'],90)
  self.assertEqual(rows[1]['program_checks']['passed'],1);self.assertEqual(rows[-1]['program_checks']['passed'],1)
  self.assertEqual(rows[1]['result']['memory_update']['ids'],rows[-1]['result']['natural_memory_ids'])
  self.assertEqual(rows[1]['synthetic_state']['memory_progress']['commit_status'],'saved')
  self.assertNotIn('assertions',str(client.calls));self.assertNotIn('saved_exact',str(client.calls))
 def test_no_candidate_is_not_memory_success_even_with_reviewed_reply(self):
  client=FaultClient();client.override=lambda m:response({'unit_ids':[]})
  code,rows,_,_=self.run_scene(client);self.assertEqual(code,1);self.assertEqual(rows[1]['program_checks']['passed'],0);self.assertEqual(rows[-1]['program_checks']['passed'],0)
 def test_diagnostic_offline_and_fixed_caps_without_client(self):
  with patch('steady_companion.cli.configured_client',side_effect=AssertionError('no API')),redirect_stdout(io.StringIO()):
   self.assertEqual(main(['eval-diagnose','--output',str(self.root/'offline.json')]),0)
   self.assertEqual(main(['eval-diagnose','--max-calls','9','--output',str(self.root/'bad.json')]),1)
   self.assertEqual(main(['eval-diagnose','--context','pilot','--max-calls','33','--output',str(self.root/'bad2.json')]),1)
 def test_log_failure_stops_safely_after_counted_requests(self):
  from steady_companion import story_eval
  original=story_eval._write_record
  def fail(output,record,secrets=()):
   if record.get('step')==2:raise OSError('PRIVATE_IO_DETAILS')
   return original(output,record,secrets)
  client=FaultClient()
  with patch('steady_companion.story_eval._write_record',side_effect=fail):code,rows,terminal,out=self.run_scene(client)
  self.assertEqual(code,1);self.assertIn('logging_write',terminal);self.assertIn('3/8',terminal);self.assertEqual(client.gens,1);self.assertNotIn('PRIVATE_IO',terminal)
 def test_output_exists_is_never_overwritten_or_paid(self):
  output=self.root/'preserve.jsonl';output.write_text('ORIGINAL_FAILED_LOG')
  with patch('steady_companion.cli.configured_client',side_effect=AssertionError('no API')),redirect_stdout(io.StringIO()) as term:
   self.assertEqual(main(['eval-diagnose','--confirm-live','--output',str(output)]),1)
  self.assertEqual(output.read_text(),'ORIGINAL_FAILED_LOG');self.assertIn('logging_write',term.getvalue())
 def test_original_pilot_user_order_unchanged_and_assertions_added(self):
  root=Path(__file__).parents[1];current=json.loads((root/'steady_companion/data/p12_pilot.json').read_text())
  expected=[NAME,'我那盆薄荷叫什么？']
  self.assertEqual(current['steps'][4]['user'],expected[0]);self.assertEqual(current['steps'][6]['user'],expected[1])
  self.assertEqual(sum('user' in s for s in current['steps']),7);self.assertEqual(sum(len(s.get('assertions',[])) for s in current['steps']),4)

class ProviderDiagnosticTests(unittest.TestCase):
 def test_reported_tokens_count_once_when_envelope_validation_fails(self):
  from test_provider import Response
  payload={'choices':[{'message':{'role':'other','content':'PRIVATE_BODY'}}], 'usage':{'prompt_tokens':11,'completion_tokens':7,'total_tokens':18}}
  requests=[]
  def opener(request,timeout):requests.append(1);return Response(payload)
  with tempfile.TemporaryDirectory() as d:
   a=Conversation(Client(Config(api_key='SYNTHETIC'),opener),Store(Path(d)),capture_stages=True)
   with self.assertRaises(ProviderError):a.reply('普通合成输入')
   self.assertEqual(a.usage.calls,1);self.assertEqual(a.usage.total_tokens,18);self.assertEqual(a.usage.unreported_calls,0);self.assertEqual(len(requests),1)
   self.assertEqual(a.last_diagnostic['failure']['code'],'response_shape');self.assertNotIn('PRIVATE_BODY',str(a.last_diagnostic))
 def test_http_status_and_timeout_adapter_codes_without_body(self):
  import urllib.error
  from test_provider import Response
  for error,code in [(urllib.error.HTTPError('https://example.invalid/private',429,'PRIVATE',{},None),'http'),(TimeoutError('PRIVATE'),'timeout'),(urllib.error.URLError('PRIVATE'),'transport')]:
   def opener(request,timeout,e=error):raise e
   client=Client(Config(api_key='SYNTHETIC_SECRET'),opener)
   with self.assertRaises(ProviderError) as caught:client.complete([{'role':'user','content':'合成'}])
   self.assertEqual(caught.exception.code,code);self.assertNotIn('PRIVATE',str(caught.exception));self.assertNotIn('SYNTHETIC_SECRET',str(caught.exception))
   if code=='http':self.assertEqual(caught.exception.http_status,429)
 def test_setup_failure_has_safe_log_and_terminal_without_request(self):
  with tempfile.TemporaryDirectory() as d,patch('steady_companion.cli.configured_client',side_effect=ProviderError('PRIVATE_SETUP')),redirect_stdout(io.StringIO()) as terminal:
   out=Path(d)/'failed.jsonl';self.assertEqual(main(['eval-diagnose','--confirm-live','--output',str(out)]),1)
   row=json.loads(out.read_text());self.assertEqual(row['error_code'],'configuration');self.assertEqual(row['usage']['calls'],0);self.assertNotIn('PRIVATE_SETUP',out.read_text()+terminal.getvalue());self.assertIn('0/8',terminal.getvalue())

class AdditionalTraceTests(unittest.TestCase):
 def test_wrapped_socket_timeout_is_not_generic_transport(self):
  import urllib.error
  def opener(request,timeout):raise urllib.error.URLError(TimeoutError('PRIVATE'))
  with self.assertRaises(ProviderError) as caught:Client(Config(api_key='SYNTHETIC'),opener).complete([{'role':'user','content':'合成'}])
  self.assertEqual(caught.exception.code,'timeout');self.assertNotIn('PRIVATE',str(caught.exception))
 def test_empty_raw_candidate_is_generation_error(self):
  def call(messages):
   v=generated('合成');v['candidates'][0]['reply']='';return response(v)
  with self.assertRaises(ProviderError) as caught:run_pipeline(call,[{'role':'user','content':'合成'}],recent_assistant=[])
  self.assertEqual(caught.exception.pipeline_metadata['failure'],{'stage':'generation','code':'visible_text'})
 def test_capture_is_bounded_even_for_large_valid_json(self):
  v={'candidates':[{'id':'A','reply':'合成文本'*4000,'evidence':[{'ref':'U0','quote':'合成'*2000}]}]*3}
  captured=capture_response(response(v));self.assertLess(len(json.dumps(captured,ensure_ascii=False)),6000)
