"""Explicitly authorized continuous synthetic evaluation, isolated temporary data."""
from contextlib import redirect_stdout
from dataclasses import asdict
import io
import json
import os
import re
import time
from pathlib import Path
from tempfile import TemporaryDirectory
from .engine import Conversation
from .store import Store
from .provider import ProviderError
from .diagnostics import error_info,safe_log


def validate_story(path):
    if path.stat().st_size > 100000:
        raise ValueError('Scenario exceeds 100 KB')
    story=json.loads(path.read_text(encoding='utf-8'))
    if not isinstance(story,dict) or not isinstance(story.get('steps'),list) or not 1<=len(story['steps'])<=100:
        raise ValueError('Scenario needs 1–100 steps')
    if not isinstance(story.get('provenance'),str) or 'synthetic' not in story['provenance'].lower():
        raise ValueError('Only explicitly labelled synthetic scenarios are accepted')
    features=story.get('features',{})
    if not isinstance(features,dict) or any(k not in ('memory_assist','context_summary') or type(v)!=bool for k,v in features.items()):
        raise ValueError('Invalid optional cost-bearing features')
    for step in story['steps']:
        if not isinstance(step,dict) or len(set(step)&{'user','command','new_session','inject'})!=1:
            raise ValueError('Each step requires exactly one supported action')
        if 'user' in step and (not isinstance(step['user'],str) or not 1<=len(step['user'])<=6000):
            raise ValueError('Bounded user text required')
        if 'command' in step:
            command=step['command']
            if not isinstance(command,str) or len(command)>550 or any(ord(c)<32 for c in command):
                raise ValueError('Invalid bounded scenario command')
            simple=command in ('/memory-mode auto','/memory-mode manual','/memory-mode session','/auto-forget all')
            correction=re.fullmatch(r'/auto-correct A[1-9][0-9]{0,5} .{1,500}',command)
            deletion=re.fullmatch(r'/auto-forget A[1-9][0-9]{0,5}',command)
            if not (simple or correction or deletion):
                raise ValueError('Scenario command is not allowlisted')
        if 'inject' in step:
            m=step['inject']
            if (not isinstance(m,dict) or set(m)!={'role','content'} or m['role']!='assistant'
                or not isinstance(m['content'],str) or not 1<=len(m['content'])<=3000
                or 'synthetic_mismatch' not in step.get('provenance','')):
                raise ValueError('Injected mismatch must be bounded, labelled synthetic assistant text')
    return story


def _write_record(output,record,secrets=()):
    output.write(json.dumps(safe_log(record,secrets),ensure_ascii=False,allow_nan=False)+'\n')
    output.flush()


def _terminal_failure(index,error,used,limit,path):
    from .engine import visible_text
    print('合成步骤 '+str(index)+' 未完成：阶段='+error['stage']+'；错误码='+error['code']+
          '；请求='+str(used)+'/'+str(limit)+'；日志='+visible_text(str(path)))
    if error.get('configuration_hint')=='check_response_format_capability':
        print('请核对模型/端点的格式能力与 NEBIUS_RESPONSE_FORMAT；未切换模式或自动重发。')


def _step_status(record):
    checks=record['program_checks']
    record['response_status']='completed' if 'result' in record else ('failed' if 'error' in record else 'not_applicable')
    record['memory_status']=record['diagnostics'].get('memory_status','not_triggered')
    record['evaluation_status']=('not_evaluated' if checks.get('status')=='not_evaluated_due_to_failure' else
        'not_configured' if not checks['total'] else 'passed' if checks['passed']==checks['total'] else 'failed')


def _terminal_step(index,record):
    if record['response_status']!='completed':
        print('完成合成步骤 '+str(index));return
    parts=['步骤 '+str(index)+' 回复完成']
    labels={'extraction_failed':'记忆提取失败','commit_failed':'记忆提交失败','read_failed':'记忆读取失败'}
    status=record['memory_status']
    if status in labels:
        error=record['diagnostics'].get('failure') or {}
        parts.append(labels[status]+('（'+error['code']+'）' if error else ''))
    if record['evaluation_status']=='failed':
        kinds=[c['kind'] for c in record['program_checks']['checks'] if not c['passed']]
        parts.append(('保存' if 'saved_exact' in kinds else '检索' if 'retrieved_exact' in kinds else '宿主')+'断言未通过')
    elif record['evaluation_status']=='passed':parts.append('宿主断言通过')
    if record['evaluation_status']=='failed' or status in labels:parts.append('继续测试')
    print('；'.join(parts)+'。')


def _terminal_summary(summary,path):
    from .engine import visible_text
    print('测试汇总：完成用户轮 '+str(summary['completed_user_turns'])+
          '；断言 '+str(summary['assertions_passed'])+'/'+str(summary['assertions_total'])+
          '（已执行 '+str(summary['assertions_evaluated'])+'）；失败步骤 '+str(summary['failed_steps'])+
          '；请求 '+str(summary['usage']['calls'])+'/'+str(summary['request_limit'])+
          '；已报告 token '+str(summary['usage']['total_tokens'])+
          '；日志='+visible_text(str(path)))


def evaluate_story(args):
    from .cli import configured_client, execute_command, runtime_status
    story=validate_story(args.scenario)
    if getattr(args,'command','') in ('eval-pilot','eval-diagnose'):
        cap=8 if getattr(args,'context',None)=='name' else 32
        turns=2 if getattr(args,'context',None)=='name' else 8
        if args.max_calls>cap or sum('user' in s for s in story['steps'])>turns:
            raise ValueError('Diagnostic/pilot scene exceeds its fixed turn or request cap.')
    if not 1<=args.max_calls<=1000:raise ValueError('max-calls must be 1–1000')
    index=0;agent=None;error=None;failed_checks=0;output_created=False
    summary={'completed_user_turns':0,'assertions_passed':0,'assertions_evaluated':0,
             'assertions_total':sum(len(step.get('assertions',[])) for step in story['steps']),
             'failed_steps':[],'warning_steps':[],'request_limit':args.max_calls}
    try:
        args.output.parent.mkdir(parents=True,exist_ok=True)
        with args.output.open('x',encoding='utf-8') as output:
            output_created=True
            os.chmod(args.output,0o600)
            if not args.confirm_live:
                _write_record(output,{'status':'not_generated_no_real_API_test','scenario':story,
                    'runtime':runtime_status(),'actual_calls':0,'actual_tokens':0,'latency':None,'cost':None})
                print('已保存合成场景输入；未生成模型回复，未进行真实 API 测试。');return 0
            client=configured_client()
            secrets=(getattr(client.config,'api_key',''),)
            with TemporaryDirectory(prefix='steady-synthetic-diagnostic-') as temp, Store(Path(temp)) as store:
                agent=Conversation(client,store,max_calls=args.max_calls,capture_stages=args.capture_stages)
                for feature,enabled in story.get('features',{}).items():agent.natural_memory.set_feature(feature,enabled)
                agent.clear()
                for index,step in enumerate(story['steps'],1):
                    step_started=time.monotonic();before=asdict(agent.usage);agent.last_diagnostic={}
                    record={'scenario_id':story.get('id'),'step':index,'input':step,'runtime':runtime_status(client.config,agent)}
                    try:
                        if step.get('new_session'):
                            usage=agent.usage
                            agent=Conversation(client,store,max_calls=args.max_calls,capture_stages=args.capture_stages)
                            agent.usage=usage
                            record['event']='new_session_same_user'
                        elif 'command' in step:
                            agent._memory_progress={}
                            with redirect_stdout(io.StringIO()):execute_command(step['command'],agent,store)
                            record['event']='synthetic_host_command'
                        elif 'inject' in step:
                            if not agent.history or agent.history[-1]['role']!='assistant':raise ValueError('Mismatch requires a previous completed reply')
                            agent.history[-1]=dict(step['inject'])
                            record['event']='synthetic_mismatch_injection_NOT_model_output'
                        else:record['result']=asdict(agent.reply(step['user']))
                        record['program_checks']=check_step(step,agent,record.get('result',{}))
                        failed_checks+=record['program_checks']['total']-record['program_checks']['passed']
                        record['failure_class']=failure_class(record.get('result',{}))
                    except (Exception,KeyboardInterrupt) as exc:
                        diagnostics=agent.last_diagnostic or getattr(exc,'pipeline_metadata',{})
                        error=(diagnostics.get('failure') if diagnostics.get('response_status')=='failed' else None) or error_info(exc,'context' if agent else 'setup')
                        record['error']=error['code'];record['failure_class']='unknown'
                        record['failed_stage']=error['stage'];record['error_code']=error['code']
                        record['program_checks']={'status':'not_evaluated_due_to_failure','passed':0,'total':len(step.get('assertions',[]))}
                    record['usage']=asdict(agent.usage)
                    record['step_usage']={k:record['usage'][k]-before[k] for k in before}
                    record['elapsed_seconds']=round(time.monotonic()-step_started,3)
                    record['diagnostics']=agent.last_diagnostic
                    # Read actual remaining synthetic state before TemporaryDirectory is removed.
                    record['synthetic_state']=agent.synthetic_state()
                    record['stored_memory_ids']=[r['id'] for r in record['synthetic_state']['durable'].get('records') or []]
                    record['experience_review']={'status':'human_review_required','annotations':[]}
                    record['cost']=None
                    if not error and record['program_checks']['passed']<record['program_checks']['total']:
                        kind=next(c['kind'] for c in record['program_checks']['checks'] if not c['passed'])
                        record['assertion_failure']={'stage':'memory' if kind=='saved_exact' else 'context','code':'assertion_failed'}
                    _step_status(record)
                    if 'result' in record:summary['completed_user_turns']+=1
                    checks=record['program_checks']
                    if record['evaluation_status']!='not_evaluated':summary['assertions_evaluated']+=checks['total']
                    summary['assertions_passed']+=checks['passed']
                    if error or record['evaluation_status']=='failed':summary['failed_steps'].append(index)
                    if record['diagnostics'].get('outcome')=='completed_with_warnings':summary['warning_steps'].append(index)
                    summary['usage']=asdict(agent.usage)
                    if error or index==len(story['steps']):record['evaluation_summary']=dict(summary)
                    _write_record(output,record,secrets)
                    if error:
                        _terminal_failure(index,error,agent.usage.calls,args.max_calls,args.output);return 1
                    _terminal_step(index,record)
        return int(failed_checks>0)
    except (OSError,UnicodeError) as exc:
        # No exception text, attempted re-request, alternate private log or overwrite.
        primary=error or (agent.last_diagnostic.get('failure') if agent and agent.last_diagnostic.get('response_status')=='failed' else None)
        error={'stage':'logging','code':'logging_write'}
        if primary:_terminal_failure(index,primary,agent.usage.calls if agent else 0,args.max_calls,args.output)
        _terminal_failure(index,{'stage':'logging','code':'logging_write'},agent.usage.calls if agent else 0,args.max_calls,args.output)
        print('诊断日志无法完整写入；未重新调用模型，文件可能只包含部分记录。')
        return 1
    except (Exception,KeyboardInterrupt) as exc:
        stage='setup' if agent is None else 'context'
        error=error_info(exc,stage)
        if agent is None and isinstance(exc,ProviderError) and error['code']=='unknown':error['code']='configuration'
        used=agent.usage.calls if agent else 0
        if output_created:
            try:
                with args.output.open('a',encoding='utf-8') as output:
                    _write_record(output,{'step':index,'error':error['code'],'failed_stage':stage,'error_code':error['code'],
                        'usage':asdict(agent.usage) if agent else {'calls':0},
                        'synthetic_state':agent.synthetic_state() if agent else {'status':'not_created'}})
            except (OSError,UnicodeError):print('诊断日志无法完整写入：logging/logging_write。')
        _terminal_failure(index,error,used,args.max_calls,args.output)
        return 1
    finally:
        if args.confirm_live and agent is not None:
            summary['usage']=asdict(agent.usage)
            if error and index not in summary['failed_steps']:summary['failed_steps'].append(index)
            _terminal_summary(summary,args.output)


def evaluate_diagnostic(args):
    args.scenario=Path(__file__).parent/'data'/('p121_naming.json' if args.context=='name' else 'p12_pilot.json')
    if args.max_calls is None:args.max_calls=8 if args.context=='name' else 32
    return evaluate_story(args)


def failure_class(result):
    update=result.get('memory_update',{})
    if update.get('status') in ('not_saved','storage_limit'): return 'commit_failure'
    extraction=result.get('architecture',{}).get('extraction_status','')
    if extraction.startswith('skipped'): return 'unknown'
    return None  # A returned reply is not an automatic expression-quality pass.


def check_step(step,agent,result):
    """Assertions are host metadata, never a user message or a response template."""
    rows=agent.natural_memory.list()
    reports=[]
    for check in step.get('assertions',[]):
        kind=check['kind'];passed=False
        if kind=='stored_contains': passed=any(check['value'] in r['text'] for r in rows)
        elif kind=='stored_absent': passed=not any(check['value'] in str(r) for r in rows)
        elif kind=='row_count': passed=len(rows)==check['value']
        elif kind=='retrieved': passed=bool(result.get('natural_memory_ids'))
        elif kind in ('saved_exact','retrieved_exact'):
            matches=[r for r in rows if r['evidence'].rstrip('。.!')==check['value'].rstrip('。.!') and r['status']=='active']
            actual=(result.get('memory_update',{}).get('ids',[]) if kind=='saved_exact' else result.get('natural_memory_ids',[]))
            passed=len(matches)==1 and matches[0]['id'] in actual
        elif kind=='derived_empty': passed=not (agent.continuity.summary or agent.continuity.sources or agent.continuity.referents or agent._memory_overrides or agent.pending_learning)
        reports.append({'kind':kind,'passed':passed,'failure_class':None if passed else check.get('failure_class','unknown')})
    return {'passed':sum(r['passed'] for r in reports),'total':len(reports),'checks':reports}
