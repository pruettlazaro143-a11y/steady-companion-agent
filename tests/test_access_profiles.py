"""Official policy wire tests: synthetic requests only, no API or account access."""
from contextlib import redirect_stdout, redirect_stderr
from dataclasses import replace
import io,json,os,socket,tempfile,unittest,urllib.error
from pathlib import Path
from unittest.mock import patch
from steady_companion import cli,model_access as a
from steady_companion.chat_transport import ChatCompletionsClient,DEEPSEEK_POLICY,GENERIC_POLICY
from steady_companion.provider import ProviderError,Config,Client
from steady_companion.engine import Conversation
from steady_companion.store import Store
from synthetic_transport import Wire,raw
ROOT=Path(__file__).resolve().parents[1]
OFFICIAL=ROOT/'configs/deepseek-official.json'
def selector(body):
    data=json.loads(body['messages'][-1]['content'])
    return raw({'unit_ids':[data['eligible_units'][0]['id']]})
class OfficialTests(unittest.TestCase):
    def setUp(self):
        tmp=tempfile.TemporaryDirectory();self.addCleanup(tmp.cleanup);self.root=Path(tmp.name)
        self.data=json.loads(OFFICIAL.read_text());self.path=self.root/'connection.json';self.write()
        block=patch.object(socket,'create_connection',side_effect=AssertionError('offline only'));block.start();self.addCleanup(block.stop)
    def write(self):self.path.write_text(json.dumps(self.data))
    def plan(self):return a.resolve('daily-deepseek',self.path)
    def client(self,values=None,plan=None):
        plan=plan or self.plan();wire=Wire(values)
        transport=ChatCompletionsClient(replace(plan.connection,api_key='SYNTHETIC_ONLY'),wire,policy=plan.transport_policy)
        return a.AccessClient(plan,transport),wire
    def agent(self,values=None,max_calls=4,store=None):
        client,wire=self.client(values)
        if store is None:
            store=Store(self.root/('synthetic-'+str(len(list(self.root.iterdir())))));self.addCleanup(store.close)
        return Conversation(client,store,max_calls=max_calls,**client.plan.policy.kwargs()),wire
    def assert_official(self,wire):
        for req,body,timeout in wire.requests:
            self.assertEqual((req.method,req.full_url),('POST','https://api.deepseek.com/chat/completions'))
            self.assertEqual(body['model'],'deepseek-flash');self.assertEqual(body['thinking'],{'type':'disabled'})
            for name in ('store','reasoning_effort','temperature','top_p','stream'):self.assertNotIn(name,body)
            self.assertNotIn('json_schema',json.dumps(body.get('response_format')))
            self.assertEqual(req.get_header('Authorization'),'Bearer SYNTHETIC_ONLY')
    def test_offline_inspect_no_credentials_client_or_database_creation(self):
        self.assertEqual(OFFICIAL.read_bytes(),(ROOT/'steady_companion/data/deepseek-official.json').read_bytes())
        out=io.StringIO()
        with patch.object(a.os.environ,'get',side_effect=lambda key,default=None: (_ for _ in ()).throw(AssertionError('credential read')) if 'KEY' in key else default),patch.object(a,'connect',side_effect=AssertionError('client')),redirect_stdout(out):
            rc=cli.main(['--data-dir',str(self.root/'absent'),'inspect','--profile','daily-deepseek','--connection',str(self.path)])
        self.assertEqual(rc,0);self.assertFalse((self.root/'absent').exists());s=json.loads(out.getvalue())
        self.assertFalse(s['model_access']['credential_loaded']);self.assertEqual(s['parameters']['thinking'],{'type':'disabled'})
        self.assertNotIn('store',s['parameters']);self.assertEqual(s['parameters']['store_field'],'omitted')
        self.assertEqual(s['structured_output']['response_request_limit'],1)
        self.assertEqual(s['structured_output']['auxiliary_schema_mode'],'json_object')
    def test_plain_reply_wire_and_actual_continuous_history(self):
        agent,wire=self.agent([raw('受控前答。'),raw('受控后答。')]);agent.reply('合成：一个日常话题。');agent.reply('合成：接着刚才说。')
        self.assert_official(wire);self.assertEqual(len(wire.requests),2)
        for req,body,timeout in wire.requests:
            self.assertEqual(set(body),{'model','messages','max_tokens','thinking'});self.assertEqual((body['max_tokens'],timeout),(4096,90))
            self.assertNotIn('deepseek',str(body['messages']).lower())
        self.assertIn({'role':'assistant','content':'受控前答。'},wire.requests[1][1]['messages'])
        self.assertEqual(agent.usage.calls,2);self.assertEqual(agent.usage.summary_calls,0)
        self.assertEqual(agent.natural_memory.mode,'manual');self.assertEqual(len(agent.history),4)
        self.assertNotIn('store',cli.runtime_status(agent.client.config,agent)['parameters'])
    def test_authorized_memory_json_object_condition_single_commit_and_reload(self):
        agent,wire=self.agent([selector,raw()]);agent.natural_memory.set_mode('auto');agent.natural_memory.set_feature('memory_assist',True)
        with patch.object(agent.natural_memory,'save_current',wraps=agent.natural_memory.save_current) as commit:
            agent.reply('平时挺爱看历史电影的，不过太悲伤的我不喜欢。')
        self.assert_official(wire);self.assertEqual(len(wire.requests),2);self.assertEqual(commit.call_count,1)
        self.assertEqual(wire.requests[0][1]['response_format'],{'type':'json_object'})
        self.assertEqual((wire.requests[0][1]['max_tokens'],wire.requests[0][2]),(1200,20))
        self.assertNotIn('response_format',wire.requests[1][1]);self.assertEqual(agent.usage.memory_calls,1)
        self.assertEqual(agent.usage.calls,2);self.assertEqual(len(agent.natural_memory.list()),1)
        other,_=self.agent(store=agent.store);self.assertTrue(other.natural_memory.feature('memory_assist'))
        self.assertIn('不过太悲伤的我不喜欢',str(other.natural_memory.list()))
    def test_explicit_learn_json_object_lower_limits_no_auto_save(self):
        agent,wire=self.agent([raw(),raw({'proposals':[]})]);agent.reply('合成：以后说话别太正式。');agent.learn()
        self.assert_official(wire);self.assertEqual(agent.usage.calls,2)
        body=wire.requests[1][1];self.assertEqual(body['response_format'],{'type':'json_object'})
        self.assertEqual((body['max_tokens'],wire.requests[1][2]),(1200,20));self.assertIn('strict JSON',str(body['messages']))
        self.assertEqual(agent.store.list_notes(),[]);self.assertEqual(agent.natural_memory.mode,'manual')
    def test_invalid_unit_no_write_or_retry_reply_continues(self):
        agent,wire=self.agent([raw({'unit_ids':['UNKNOWN']}),raw()]);agent.natural_memory.set_mode('auto');agent.natural_memory.set_feature('memory_assist',True)
        agent.reply('平时挺爱看历史电影的，不过太悲伤的我不喜欢。')
        self.assertEqual(len(wire.requests),2);self.assertEqual(agent.natural_memory.list(),[])
        self.assertEqual(len(agent.history),2);self.assertEqual(agent.usage.memory_calls,1);self.assert_official(wire)
    def test_config_conflicts_fail_before_credentials_store_request(self):
        original=dict(self.data)
        for change in ({'response_format':'json_schema'},{'response_format':'off'},{'thinking':{'type':'enabled'}},
                       {'thinking':{'type':'disabled','extra':1}},{'reasoning_effort':'none'},{'store':False},
                       {'transport_policy':'guess'},{'provider':'another'},{'base_url':'https://api.deepseek.com/beta'},
                       {'model':'deepseek-v4-pro'},{'temperature':1},{'top_p':1},{'max_tokens':8192},{'timeout':91},
                       {'api_key_env':'NEBIUS_API_KEY'}):
            with self.subTest(change=change):
                self.data={**original,**change};self.write();out=io.StringIO()
                with patch.object(a,'connect',side_effect=AssertionError('credential')),patch.object(cli,'Store',side_effect=AssertionError('store')),redirect_stdout(out),redirect_stderr(out):
                    rc=cli.main(['--data-dir',str(self.root/'absent'),'chat','--profile','daily-deepseek','--connection',str(self.path)])
                self.assertEqual(rc,1)
                if change.get('response_format')=='json_schema':self.assertIn('json_schema',out.getvalue())
    def test_transport_overrides_and_policy_mismatch_rejected_locally(self):
        client,wire=self.client();messages=[{'role':'user','content':'合成'}]
        for kwargs in ({'response_format':{'type':'json_schema'}},{'temperature':.5},{'top_p':.9}):
            with self.assertRaises(ProviderError):client.complete(messages,**kwargs)
        with self.assertRaises(TypeError):client.complete(messages,reasoning_effort='none')
        self.assertFalse(wire.requests)
        with self.assertRaises(ProviderError):ChatCompletionsClient(replace(client.config,response_format='json_schema'),wire,policy=DEEPSEEK_POLICY)
        with self.assertRaises(ProviderError):a.AccessClient(client.plan,ChatCompletionsClient(client.config,wire))
    def test_ten_field_connection_not_reinterpreted_even_with_official_route(self):
        self.data.pop('thinking');self.data.pop('transport_policy');self.write();plan=self.plan()
        self.assertEqual(plan.transport_policy,GENERIC_POLICY);client,wire=self.client(plan=plan);client.complete([{'role':'user','content':'合成'}])
        self.assertEqual(set(wire.requests[0][1]),{'model','messages','max_tokens','store'});self.assertFalse(wire.requests[0][1]['store'])
    def test_nebius_legacy_request_body_unchanged(self):
        with patch.dict(os.environ,{},clear=True):plan=a.resolve('competition-nebius')
        client,wire=self.client(plan=plan);legacy=Wire();messages=[{'role':'user','content':'合成'}]
        from steady_companion.response_formats import for_stage
        fmt=for_stage('json_schema','review');client.complete(messages,response_format=fmt)
        Client(Config(api_key='SYNTHETIC_ONLY'),legacy).complete(messages,response_format=fmt)
        self.assertEqual(wire.requests[0][1],legacy.requests[0][1]);self.assertFalse(wire.requests[0][1]['store'])
        self.assertNotIn('thinking',wire.requests[0][1]);self.assertEqual(plan.credential_env,'NEBIUS_API_KEY')
    def test_hidden_credential_no_fallback_no_startup_probe(self):
        plan=self.plan()
        with patch.dict(os.environ,{'NEBIUS_API_KEY':'SYNTHETIC_NEBIUS'},clear=True),patch.object(a.sys.stdin,'isatty',return_value=False),patch.object(a,'ChatCompletionsClient') as factory:
            with self.assertRaises(ProviderError):a.connect(plan)
            factory.assert_not_called()
        wire=Wire()
        def factory(config,**kwargs):return ChatCompletionsClient(config,wire,**kwargs)
        with patch.dict(os.environ,{'NEBIUS_API_KEY':'SYNTHETIC_NEBIUS'},clear=True),patch.object(a.sys.stdin,'isatty',return_value=True),patch.object(a.getpass,'getpass',return_value='SYNTHETIC_ONLY') as hidden,patch.object(a,'ChatCompletionsClient',side_effect=factory):
            client=a.connect(plan);hidden.assert_called_once();self.assertFalse(wire.requests)
            self.assertNotIn('STEADY_DEEPSEEK_API_KEY',os.environ)
        client.complete([{'role':'user','content':'合成'}]);self.assert_official(wire)
        self.assertNotIn('SYNTHETIC_ONLY',repr(client.config));self.assertNotIn('SYNTHETIC_ONLY',str(plan.status()))
    def test_failures_no_retry_no_failed_commit_shared_budget(self):
        for value in (raw(''),raw('<analysis>PRIVATE_SYNTHETIC</analysis>'),TimeoutError('PRIVATE_SYNTHETIC'),KeyboardInterrupt('PRIVATE_SYNTHETIC'),urllib.error.URLError('PRIVATE_SYNTHETIC'),urllib.error.HTTPError('https://synthetic.invalid',400,'PRIVATE_SYNTHETIC',{},io.BytesIO(b'PRIVATE_SYNTHETIC'))):
            with self.subTest(value=type(value).__name__):
                agent,wire=self.agent([selector,value,raw()],max_calls=3);agent.natural_memory.set_mode('auto');agent.natural_memory.set_feature('memory_assist',True)
                with self.assertRaises((ProviderError,KeyboardInterrupt)):agent.reply('平时挺爱看历史电影的，不过太悲伤的我不喜欢。')
                self.assertEqual(len(wire.requests),2);self.assertEqual(agent.usage.calls,2)
                self.assertEqual(agent.history,[]);self.assertEqual(agent.natural_memory.list(),[])
                agent.reply('合成：用户主动换个话题。');self.assertEqual(len(wire.requests),3)
                self.assertNotIn('PRIVATE_SYNTHETIC',str(wire.requests[-1][1]['messages']))
                with self.assertRaises(ProviderError):agent.reply('合成：额度外消息')
                self.assertEqual(len(wire.requests),3);self.assert_official(wire)
    def test_auxiliary_session_quota_and_total_budget(self):
        agent,wire=self.agent([selector,raw(),raw()],max_calls=3);agent.natural_memory.set_mode('auto');agent.natural_memory.set_feature('memory_assist',True)
        agent.reply('平时挺爱看历史电影的，不过太悲伤的我不喜欢。');agent.reply('合成：今天只打招呼。')
        self.assertEqual(agent.usage.calls,3);self.assertEqual(agent.usage.memory_calls,1)
        with self.assertRaises(ProviderError):agent.learn()
        self.assertEqual(len(wire.requests),3)
        other,wire2=self.agent([raw({'unit_ids':[]})]*4,max_calls=8)
        for _ in range(4):other._aux_call([{'role':'user','content':'合成 JSON'}],'memory')
        with self.assertRaises(ProviderError):other._aux_call([{'role':'user','content':'合成 JSON'}],'memory')
        self.assertEqual(other.usage.memory_calls,4);self.assertEqual(len(wire2.requests),4);self.assert_official(wire2)
if __name__=='__main__':unittest.main()
