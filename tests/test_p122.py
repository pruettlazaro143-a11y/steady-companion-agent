"""P1.2.2 offline faults; fixtures are not model-quality evaluations."""
from contextlib import redirect_stdout
from copy import deepcopy
from dataclasses import replace
import io,json,os,tempfile,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
from steady_companion.provider import Client,Config,ProviderError
from steady_companion.response_formats import for_stage
from steady_companion.pipeline import run_pipeline
from steady_companion.diagnostics import Trace,capture_response,safe_log
from steady_companion.cli import main,runtime_status
from steady_companion.engine import Conversation,ContextChangedError
from steady_companion.store import Store
from test_p121 import FaultClient,generated,reviewed,NAME
from test_p12 import response
from test_provider import Response


def malformed(text='{"candidates":'):
    r=response({});r['choices'][0]['message']['content']=text;r['choices'][0]['finish_reason']='stop';return r

class FormatTests(unittest.TestCase):
    def test_wire_modes_and_stage_schemas(self):
        for mode in ('off','json_object','json_schema'):
            bodies=[]
            def opener(req,timeout):bodies.append(json.loads(req.data));return Response(response(generated(NAME)))
            client=Client(Config(api_key='SYNTHETIC',response_format=mode),opener)
            for stage in ('generation','review','repair_generation','repair_review','memory','summary','direct'):
                fmt=for_stage(mode,stage)
                client.complete([{'role':'user','content':NAME}],response_format=fmt)
                body=bodies[-1]
                if fmt is None:self.assertNotIn('response_format',body)
                else:
                    self.assertEqual(body['response_format'],fmt)
                    if mode=='json_schema':
                        schema=body['response_format']['json_schema']
                        self.assertTrue(schema['strict']);self.assertFalse(schema['schema']['additionalProperties'])
                        self.assertEqual(set(schema['schema']['required']),{'approved_ids','issues','repair_hint'} if stage.endswith('review') else {'candidates','selected_id'})
            client.complete([{'role':'user','content':NAME}])
            self.assertNotIn('response_format',bodies[-1])
    def test_config_env_and_inspect(self):
        for mode in ('off','json_object','json_schema'):
            with patch.dict(os.environ,{'NEBIUS_RESPONSE_FORMAT':mode,'NEBIUS_API_KEY':'SYNTHETIC_KEY'}):
                cfg=Config.from_env();self.assertEqual(cfg.response_format,mode)
                status=runtime_status(cfg);self.assertEqual(status['structured_output']['mode'],mode)
                self.assertEqual(status['structured_output']['endpoint_compatibility'],'unverified')
                self.assertNotIn('SYNTHETIC_KEY',str(status))
        with self.assertRaises(ProviderError):Config(response_format='guess')
    def test_invalid_format_never_sent(self):
        client=Client(Config(api_key='SYNTHETIC'),lambda *a,**k:self.fail('must not send'))
        with self.assertRaises(ProviderError):client.complete([{'role':'user','content':NAME}],response_format={'type':'bad','secret':'PRIVATE'})
    def test_http_configuration_hint_no_switch_or_probe(self):
        requests=[]
        def opener(req,timeout):
            requests.append(req);raise urllib.error.HTTPError(req.full_url,400,'PRIVATE',{},None)
        client=Client(Config(api_key='SYNTHETIC'),opener);self.assertFalse(requests)
        with self.assertRaises(ProviderError) as caught:client.complete([{'role':'user','content':NAME}],response_format=for_stage('json_schema','generation'))
        self.assertEqual(len(requests),1);self.assertEqual(caught.exception.code,'http')
        self.assertEqual(caught.exception.configuration_hint,'check_response_format_capability');self.assertNotIn('PRIVATE',str(caught.exception))

class RecoveryTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
    def agent(self,fault=None,max_calls=16,capture=True,mode='json_schema'):
        client=FaultClient(fault);client.config=replace(client.config,response_format=mode)
        a=Conversation(client,Store(self.root/str(len(list(self.root.iterdir())))),max_calls=max_calls,capture_stages=capture)
        a.natural_memory.set_mode('auto');a.natural_memory.set_feature('memory_assist',True)
        return a,client
    def test_syntax_recovery_review_commit_once_and_count_once(self):
        a,c=self.agent(lambda stage,n,v:malformed() if stage=='generation' and n==1 else None)
        with patch.object(a.natural_memory,'save_current',wraps=a.natural_memory.save_current) as commit:
            turn=a.reply(NAME);self.assertEqual(commit.call_count,1)
        d=a.last_diagnostic;self.assertTrue(turn.text);self.assertEqual(d['outcome'],'recovered');self.assertIsNone(d['failure'])
        self.assertEqual(d['failure_history'],[{'stage':'generation','code':'json_parse'}])
        self.assertEqual(a.usage.calls,4);self.assertEqual(a.usage.memory_calls,1);self.assertEqual(a.usage.total_tokens,72)
        self.assertEqual(sum(s['calls'] for s in d['stages']),4);self.assertEqual(a.usage.unreported_calls,0)
        self.assertEqual(d['completed_stages'],['memory','repair_generation','repair_review','commit'])
        self.assertEqual(d['stages'][1]['recovery_outcome'],'recovered')
        self.assertEqual(len(a.natural_memory.list()),1)
        self.assertEqual([kw.get('response_format',{}).get('type','off') for _,kw in c.calls],['json_schema','json_schema','json_schema','json_schema'])
    def test_candidate_structure_recovery(self):
        for field in ('selected_id','candidates'):
            def fault(stage,n,v):
                if stage=='generation' and n==1:v.pop(field);return response(v)
            a,c=self.agent(fault);a.reply(NAME);self.assertEqual(c.gens,2);self.assertEqual(c.reviews,1);self.assertIsNone(a.last_diagnostic['failure'])
    def test_recovery_still_invalid_is_terminal_no_review_no_commit(self):
        a,c=self.agent(lambda stage,n,v:malformed() if stage=='generation' else None)
        with self.assertRaises(ProviderError):a.reply(NAME)
        self.assertEqual(c.gens,2);self.assertEqual(c.reviews,0);self.assertEqual(a.usage.calls,3)
        d=a.last_diagnostic;self.assertEqual(d['outcome'],'terminal_failure');self.assertEqual(len(d['failure_history']),2)
        self.assertFalse(d['memory_progress']['commit_attempted']);self.assertFalse(a.natural_memory.list());self.assertFalse(a.history)
    def test_recovered_generation_rejected_cannot_content_repair(self):
        def fault(stage,n,v):
            if stage=='generation' and n==1:return malformed()
            if stage=='review':return response({'approved_ids':[],'issues':[],'repair_hint':''})
        a,c=self.agent(fault)
        with self.assertRaises(ProviderError):a.reply(NAME)
        self.assertEqual((c.gens,c.reviews,a.usage.calls),(2,1,4));self.assertEqual(a.last_diagnostic['failure']['code'],'review_rejected')
        self.assertFalse(a.last_diagnostic['memory_progress']['commit_attempted']);self.assertEqual(a.last_diagnostic['outcome'],'terminal_failure')
    def test_insufficient_recovery_budget_no_unreviewed_generation(self):
        a,c=self.agent(lambda stage,n,v:malformed(),max_calls=3)
        with self.assertRaises(ProviderError):a.reply(NAME)
        self.assertEqual((c.gens,c.reviews,a.usage.calls),(1,0,2));d=a.last_diagnostic
        self.assertEqual(d['failure']['code'],'budget');self.assertEqual(d['failure_history'][0]['code'],'json_parse')
        self.assertEqual(d['stages'][1]['recovery_outcome'],'not_started_budget');self.assertFalse(a.history)
    def test_content_repair_retains_original_four_call_limit(self):
        def fault(stage,n,v):
            if stage=='review' and n==1:return response({'approved_ids':[],'issues':[],'repair_hint':'synthetic observable edit'})
        a,c=self.agent(fault);a.reply(NAME)
        self.assertEqual((c.gens,c.reviews,a.usage.calls),(2,2,5));self.assertEqual(a.usage.memory_calls,1)
    def test_content_repair_then_syntax_fault_cannot_retry(self):
        def fault(stage,n,v):
            if stage=='review':return response({'approved_ids':[],'issues':[],'repair_hint':''})
            if n==2:return malformed()
        a,c=self.agent(fault)
        with self.assertRaises(ProviderError):a.reply(NAME)
        self.assertEqual((c.gens,c.reviews,a.usage.calls),(2,1,4));self.assertEqual(a.last_diagnostic['failure']['stage'],'repair_generation')
    def test_transport_http_timeout_cancel_context_do_not_retry(self):
        for exc in (ProviderError('PRIVATE',code='http'),ConnectionError('PRIVATE'),TimeoutError('PRIVATE'),KeyboardInterrupt(),ContextChangedError('PRIVATE')):
            def fault(stage,n,v):raise exc
            a,c=self.agent(fault)
            with self.assertRaises((ProviderError,KeyboardInterrupt,ConnectionError,TimeoutError)):a.reply(NAME)
            self.assertEqual(c.gens,1);self.assertEqual(c.reviews,0);self.assertEqual(a.usage.calls,2)
            self.assertNotIn('PRIVATE',json.dumps(safe_log(a.last_diagnostic)))
    def test_evidence_quote_and_reference_do_not_retry(self):
        for field,value in (('ref','U999'),('quote','UNSEEN')):
            def fault(stage,n,v):
                for candidate in v['candidates']:candidate['evidence'][0][field]=value
                return response(v)
            a,c=self.agent(fault)
            with self.assertRaises(ProviderError):a.reply(NAME)
            self.assertEqual((c.gens,c.reviews),(1,0));self.assertEqual(a.last_diagnostic['failure']['code'],'no_eligible_candidates')
            self.assertEqual(len(a.last_diagnostic['stages'][1]['candidate_validation']['rejected_candidates']),2)
    def test_failed_raw_text_never_enters_reprompt_or_log(self):
        bad='{"candidates":<analysis>PRIVATE_REASONING</analysis> "api_key":"SECRET_PAYLOAD"'
        a,c=self.agent(lambda stage,n,v:malformed(bad) if stage=='generation' and n==1 else None)
        a.reply(NAME)
        for data in (json.dumps(safe_log(a.last_diagnostic)),str(c.calls)):
            self.assertNotIn('PRIVATE_REASONING',data);self.assertNotIn('SECRET_PAYLOAD',data)
        repair=c.calls[2][0];self.assertIn('HOST FORMAT ERROR: json_parse',repair[-1]['content'])
        self.assertEqual(c.calls[1][0][:-1],repair[:-1])
    def test_default_capture_does_not_save_candidates_even_on_failure(self):
        a,c=self.agent(lambda stage,n,v:malformed(),capture=False)
        with self.assertRaises(ProviderError):a.reply(NAME)
        self.assertNotIn('response_capture',str(a.last_diagnostic));self.assertNotIn(NAME,str(a.last_diagnostic))
    def test_off_mode_still_has_bounded_recovery(self):
        a,c=self.agent(lambda stage,n,v:malformed() if stage=='generation' and n==1 else None,mode='off')
        a.reply(NAME);self.assertEqual(a.last_diagnostic['outcome'],'recovered')
        self.assertTrue(all('response_format' not in kw for _,kw in c.calls))
    def test_plain_chat_and_auxiliary_do_not_inherit_schema(self):
        from test_p12 import Controlled
        client=Controlled();client.config=Config(api_key='SYNTHETIC',response_format='json_schema')
        a=Conversation(client,Store(self.root/'direct'),response_mode='direct');a.reply('普通合成输入')
        self.assertTrue(all('response_format' not in kw for _,kw in client.calls))
        a._diagnostics.start('summary');a._call([{'role':'system','content':'CONTINUITY_SELECTION'}, {'role':'user','content':'{"sources":[]}'}],allow_tools=False,purpose='summary')
        self.assertNotIn('response_format',client.calls[-1][1])
    def test_ordered_pilot_fault_has_prior_interest_session_switch_and_four_assertions(self):
        client=FaultClient(lambda stage,n,v:malformed() if stage=='generation' and n==3 else None)
        output=self.root/'pilot-new.jsonl';terminal=io.StringIO()
        with patch('steady_companion.cli.configured_client',return_value=client),redirect_stdout(terminal):
            code=main(['eval-pilot','--confirm-live','--capture-stages','--max-calls','32','--output',str(output)])
        rows=[json.loads(line) for line in output.read_text().splitlines()]
        self.assertEqual(code,0);self.assertEqual(len(rows),11);self.assertEqual(sum('result' in row for row in rows),7)
        self.assertEqual(sum(r['program_checks']['passed'] for r in rows),4)
        self.assertEqual(rows[2]['event'],'new_session_same_user');self.assertIn(1,rows[3]['result']['natural_memory_ids'])
        failed=rows[4];self.assertEqual(failed['input']['user'],NAME);self.assertEqual(failed['diagnostics']['outcome'],'recovered')
        self.assertEqual(failed['step_usage']['calls'],4);self.assertEqual(failed['step_usage']['memory_calls'],1)
        self.assertTrue(failed['synthetic_state']['memory_progress']['commit_attempted'])
        self.assertNotIn('未完成',terminal.getvalue());self.assertLessEqual(rows[-1]['usage']['calls'],32)
        # Keep assertions strictly host-side; raw original inputs remain intact.
        for messages,_ in client.calls:self.assertNotIn('saved_exact',str(messages));self.assertNotIn('retrieved_exact',str(messages))

    def test_recovery_followed_by_invalid_review_stops(self):
        def fault(stage,n,v):
            if stage=='generation' and n==1:return malformed()
            if stage=='review':return response({'approved_ids':['UNKNOWN'],'issues':[],'repair_hint':''})
        a,c=self.agent(fault)
        with self.assertRaises(ProviderError):a.reply(NAME)
        self.assertEqual((c.gens,c.reviews,a.usage.calls),(2,1,4))
        self.assertEqual(a.last_diagnostic['failure']['code'],'review_reference')
        self.assertEqual(len(a.last_diagnostic['failure_history']),2);self.assertFalse(a.history)
    def test_length_marker_never_auto_recovers(self):
        def fault(stage,n,v):
            r=response(v);r['choices'][0]['finish_reason']='length';return r
        a,c=self.agent(fault)
        with self.assertRaises(ProviderError):a.reply(NAME)
        self.assertEqual((c.gens,c.reviews),(1,0));self.assertEqual(a.last_diagnostic['failure']['code'],'response_truncated')
    def test_provider_envelope_json_fault_does_not_trigger_inner_recovery(self):
        calls=[]
        def opener(req,timeout):calls.append(1);return Response(b'{invalid envelope')
        a=Conversation(Client(Config(api_key='SYNTHETIC'),opener),Store(self.root/'envelope'))
        with self.assertRaises(ProviderError):a.reply('普通合成输入')
        self.assertEqual(len(calls),1);self.assertEqual(a.usage.calls,1);self.assertEqual(a.usage.unreported_calls,1)
    def test_session_budget_after_previous_usage_reserves_both_calls(self):
        a,c=self.agent(lambda stage,n,v:malformed(),max_calls=8);a.usage.calls=5
        with self.assertRaises(ProviderError):a.reply(NAME)
        self.assertEqual(a.usage.calls,7);self.assertEqual(c.gens,1);self.assertEqual(c.reviews,0)
        self.assertEqual(a.last_diagnostic['turn_usage']['calls'],2)
    def test_exact_remaining_budget_completes_review(self):
        a,c=self.agent(lambda stage,n,v:malformed() if stage=='generation' and n==1 else None,max_calls=4)
        a.reply(NAME);self.assertEqual(a.usage.calls,4);self.assertEqual(a.last_diagnostic['outcome'],'recovered')
    def test_successful_format_recovery_does_not_mask_commit_failure(self):
        import sqlite3
        a,c=self.agent(lambda stage,n,v:malformed() if stage=='generation' and n==1 else None)
        with patch.object(a.natural_memory,'save_current',side_effect=sqlite3.OperationalError('PRIVATE')):
            result=a.reply(NAME)
        self.assertTrue(result.text);d=a.last_diagnostic
        self.assertEqual(d['failure'],{'stage':'commit','code':'storage'});self.assertEqual(d['outcome'],'completed_with_warnings')
        self.assertEqual(d['stages'][1]['recovery_outcome'],'recovered');self.assertFalse(a.natural_memory.list())
        self.assertNotIn('PRIVATE',str(d))

class ParseMetadataTests(unittest.TestCase):
    def test_safe_parser_categories_and_positions(self):
        for raw,category in [('{} {}','trailing_data'),('{"a":1 "b":2}','expected_delimiter'),('{"a":"x','unterminated_string'),('{"a":"\\q"}','invalid_escape'),('{','unknown')]:
            capture=capture_response(malformed(raw));self.assertEqual(capture['json_parse_category'],category)
            self.assertIn('json_error_position',capture);self.assertEqual(capture['content_chars'],len(raw));self.assertEqual(capture['finish_reason'],'stop')
            self.assertNotIn('reply',capture);self.assertNotIn('content',capture)
    def test_fenced_positions_are_explicitly_normalized(self):
        capture=capture_response(malformed('```json\n{} {}\n```'))
        self.assertEqual(capture['json_error_position']['offset'],3)

if __name__=='__main__':unittest.main()
