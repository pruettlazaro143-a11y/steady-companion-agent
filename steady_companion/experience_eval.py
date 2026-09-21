"""One bounded, opt-in synthetic conversation. No production runtime changes.

Fixtures test execution only. Applicability is a human decision; checker verdicts
are observations, never labels for companionship quality or ground truth.
"""
from dataclasses import asdict
from datetime import datetime
import hashlib
import json
import os
from pathlib import Path
from tempfile import TemporaryDirectory
import time
from uuid import uuid4

from . import __version__
from . import model_access
from .chat_transport import DEEPSEEK_POLICY, response_metadata
from .diagnostics import StageError, error_info, safe_log, visible_fragment
from .engine import Usage
from .interaction_runtime import InteractionConversation
from .store import Store

PROTOCOL = 'continuous-experience-v1'
MAX_TURNS = 6
MAX_CALLS = 12
# Newly authored synthetic material, not private chat or fabricated assistant history.
# The purpose labels stay in the host report and are never sent to the model.
SCENARIO = (
    ('interest', '我最近迷上了画虚构城市的散步地图，给小巷起名字比画路还好玩。昨天画了一条只在下雨时营业的街。'),
    ('co_creation', '顺着刚才聊的地图，我们给这条雨天才营业的街编一间小店吧。你觉得店里会卖什么？'),
    ('setback_without_help_request', '说到给人介绍地图，我今天在社团展示自己的作品，练了好几遍，上台却漏讲了最想分享的那条小路，有点失落。我现在只是想说说这件事。'),
    ('specific_help', '下次还要介绍这张地图。帮我写一个三十秒左右的开场吧，让大家先注意到那条雨天营业的街。'),
    ('narrow_scope', '我们只讨论这次地图介绍的开头，别追问我过去的经历了。把开头改成两句能直接说出口的话就好。'),
    ('pause', '我有点累了，先不聊这个了，也先不分析了。'),
)


def material():
    return [{'turn': i, 'purpose': goal, 'user_input': text}
            for i, (goal, text) in enumerate(SCENARIO, 1)]


def reported_usage(value):
    if not isinstance(value, dict):
        return None
    result = {k: value[k] for k in ('prompt_tokens', 'completion_tokens', 'total_tokens')
              if type(value.get(k)) is int and value[k] >= 0}
    return result or None


class Report:
    """Exclusive local files, flushed before the next request. No raw exceptions."""
    def __init__(self, root):
        self.root = Path(root)
        self.root.mkdir(parents=True, exist_ok=False, mode=0o700)
        self.secrets = ()
        self.files = []
        try:
            for name in ('events.jsonl', 'report.md'):
                fd = os.open(self.root / name, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                self.files.append(os.fdopen(fd, 'w', encoding='utf-8'))
        except BaseException:
            self.close()
            raise

    def emit(self, event):
        clean = safe_log(event, self.secrets)
        try:
            self.files[0].write(json.dumps(clean, ensure_ascii=False, allow_nan=False) + '\n')
            # Human-readable, verbatim sanitized fields; no HTML interpretation.
            self.files[1].write('\n## ' + event['event'] + '\n\n    ' +
                                json.dumps(clean, ensure_ascii=False, indent=2).replace('\n', '\n    ') + '\n')
            for stream in self.files:
                stream.flush()
                os.fsync(stream.fileno())
        except (OSError, ValueError):
            raise StageError('logging', 'logging_write') from None

    def close(self):
        for stream in self.files:
            stream.close()


class RecordedClient:
    """Observe the existing client once, without owning or duplicating Usage."""
    def __init__(self, client, report):
        self.client, self.report, self.config = client, report, client.config
        self.agent = None
        self.records = []
        self.turn = 0

    @property
    def auxiliary_response_format(self):
        return self.client.auxiliary_response_format

    def complete(self, messages, **kwargs):
        stage = self.agent._diagnostics.current
        if stage not in ('generation', 'interaction_check') or len(self.records) >= MAX_CALLS:
            raise StageError(stage, 'budget')
        row = {'event': 'request', 'turn': self.turn, 'request_number': len(self.records) + 1,
               'stage': stage, 'status': 'started', 'service_usage': None, 'amount': None,
               'amount_status': 'unknown', 'max_output_tokens': kwargs.get('max_tokens', self.config.max_tokens)}
        self.records.append(row)
        self.report.emit({**row, 'event': 'request_started'})
        started = time.monotonic()
        failure = None
        try:
            response = self.client.complete(messages, **kwargs)
            row.update(status='returned', service_usage=reported_usage(response.get('usage')),
                       metadata=response_metadata(response, self.report.secrets))
            if stage == 'generation':
                raw = response['choices'][0]['message'].get('content')
                row['visible_candidate'] = visible_fragment(raw, self.report.secrets, 12000)
                row['candidate_capture'] = 'safe_visible_text' if row['visible_candidate'] is not None else 'omitted_unsafe_or_unavailable'
            return response
        except (Exception, KeyboardInterrupt) as exc:
            failure = exc
            row.update(status='failed', error=error_info(exc, stage),
                       service_usage=reported_usage(getattr(exc, 'reported_usage', None)))
            raise
        finally:
            row['elapsed_seconds'] = round(time.monotonic() - started, 6)
            try:
                self.report.emit(row)
            except StageError as exc:
                # The engine still accounts for a returned response if logging
                # failed after transport; do not charge/count it a second time.
                if row['service_usage'] is not None:
                    exc.reported_usage = row['service_usage']
                if isinstance(failure, KeyboardInterrupt):
                    raise failure
                raise exc from None


def review_fields(check_status):
    return {'generation_content_problem': 'not_assessed_by_human',
            'possible_checker_false_positive': 'needs_human_review' if check_status == 'conflict' else 'not_assessed_by_human',
            'naturalness_and_helpfulness': 'pending_human_reading',
            'note': 'A conflict verdict does not establish a content defect; not_triggered is not pass.'}


def turn_record(index, text, agent, requests, result, failure):
    check = agent.check_status
    by_stage = {}
    for stage in ('generation', 'interaction_check'):
        rows = [r for r in requests if r['stage'] == stage]
        by_stage[stage] = {'requests': len(rows), 'elapsed_seconds': round(sum(r.get('elapsed_seconds', 0) for r in rows), 6),
                           'reported_usage': [r['service_usage'] for r in rows],
                           'unreported_calls': sum(not r['service_usage'] or 'total_tokens' not in r['service_usage'] for r in rows)}
    candidates = [r for r in requests if r['stage'] == 'generation' and r.get('status') == 'returned']
    row = {'event': 'turn', 'turn': index, 'user_input': text,
           'response_status': 'completed' if result is not None else 'not_delivered',
           'delivered_reply': result.text if result is not None else None,
           'withheld_candidate': candidates[-1].get('visible_candidate') if candidates and result is None else None,
           'candidate_capture': candidates[-1].get('candidate_capture') if candidates else 'unavailable',
           'check_mode': 'selective', 'check_triggered': check['status'] not in ('not_triggered', 'disabled'),
           'check': check, 'stages': by_stage, 'amount': None, 'amount_status': 'unknown',
           'error': failure, 'diagnostics': agent.last_diagnostic,
           'human_review': review_fields(check['status'])}
    return row


def stop_reason(failure, check):
    if failure and failure['code'] == 'cancelled': return 'cancelled'
    if failure and failure['code'] == 'budget': return 'budget_exhausted'
    if check == 'conflict': return 'checker_blocked_pending_human_review'
    if check == 'unknown': return 'checker_unknown'
    return 'technical_failure'


def run_session(client, report, plan, *, ask=input, say=print):
    recorded = RecordedClient(client, report)
    report.secrets = (getattr(client.config, 'api_key', ''),)
    status = 'technical_failure'
    completed = attempted = 0
    failure = None
    usage = Usage()
    # No user-chosen database path, migration, or user memory access.
    with TemporaryDirectory(prefix='steady-experience-synthetic-') as folder, Store(Path(folder)) as store:
        agent = InteractionConversation(recorded, store, interaction_check='selective',
                                        max_calls=MAX_CALLS, **plan.policy.kwargs())
        recorded.agent = agent
        usage = agent.usage
        if agent.natural_memory.mode != 'manual' or any(agent.natural_memory.feature(n) for n in ('memory_assist', 'context_summary')):
            raise StageError('setup', 'configuration')
        report.emit({'event': 'session', 'storage': 'new_temporary_synthetic_directory',
                     'memory_mode': 'manual', 'memory_assist': False, 'context_summary': False,
                     'interaction_control': 'session', 'interaction_check': 'selective'})
        for index, (_, text) in enumerate(SCENARIO, 1):
            say('\n第 ' + str(index) + ' 轮合成输入：' + text)
            if index > 1:
                # Last actual delivered answer is on screen; operator input never
                # enters model history, is not saved, and cannot expand the script.
                say('请对照上轮实际回复判断下一句是否接得上；这不是自然度评分。')
                try:
                    applicable = ask('适用才输入 y；其他输入或无法确认均停止：').strip().lower() == 'y'
                except EOFError:
                    applicable = False
                except KeyboardInterrupt:
                    report.emit({'event': 'applicability', 'turn': index, 'applicable': None,
                                 'basis': 'operator_cancelled'})
                    status = 'cancelled'
                    failure = {'stage': 'context', 'code': 'cancelled'}
                    break
                report.emit({'event': 'applicability', 'turn': index, 'applicable': applicable,
                             'basis': 'operator_read_actual_previous_reply'})
                if not applicable:
                    status = 'not_applicable'
                    break
            attempted += 1
            recorded.turn = index
            before = len(recorded.records)
            result = None
            failure = None
            try:
                result = agent.reply(text)
            except (Exception, KeyboardInterrupt) as exc:
                failure = error_info(exc, getattr(exc, 'stage', agent._diagnostics.current))
            row = turn_record(index, text, agent, recorded.records[before:], result, failure)
            report.emit(row)
            if failure:
                status = stop_reason(failure, agent.check_status['status'])
                say('本轮未交付回复，停止：' + status + ' / ' + failure['stage'] + '/' + failure['code'])
                break
            completed += 1
            # Never display a withheld candidate as the companion's reply.
            say('稳伴 › ' + safe_log(result.text, report.secrets))
            say('检查：' + agent.check_status['status'] + '；累计请求 ' + str(usage.calls) + '/12')
        else:
            status = 'completed_pending_human_review'
        report.emit({'event': 'storage_final', 'memory_mode': agent.natural_memory.mode,
                     'durable_memories': len(store.list_memories()), 'shared_notes': len(store.list_notes()),
                     'automatic_memories': len(agent.natural_memory.list()),
                     'completed_history_messages': len(agent.history), 'temporary_directory_cleanup': 'on_context_exit'})
    summary = {'event': 'summary', 'status': status, 'completed_user_turns': completed,
               'attempted_user_turns': attempted, 'maximum_user_turns': MAX_TURNS,
               'not_completed_turns': list(range(completed + 1, MAX_TURNS + 1)),
               'usage': asdict(usage), 'request_limit': MAX_CALLS, 'retries': 0,
               'amount': None, 'amount_status': 'unknown', 'error': failure,
               'temporary_data_removed': True, 'human_review': review_fields('not_assessed')}
    report.emit(summary)
    say('体验结束：' + status + '；完成 ' + str(completed) + '/6 轮；请求 ' + str(usage.calls) + '/12；已报告 token ' +
        str(usage.total_tokens) + '，未报告用量请求 ' + str(usage.unreported_calls) + '；金额未知。')
    return 0 if status == 'completed_pending_human_review' else 1


def evaluate(args, *, ask=None, say=print):
    # Validation and exclusive outputs precede credentials and client creation.
    plan = model_access.resolve('daily-deepseek', args.connection)
    plan.require_ready()
    if plan.transport_policy != DEEPSEEK_POLICY:
        raise StageError('setup', 'configuration')
    if args.interaction_control != 'session' or args.interaction_check != 'selective':
        raise StageError('setup', 'configuration')
    rows = material()
    root = args.output or Path('eval-results') / ('continuous-dev15-' + datetime.now().strftime('%Y%m%d-%H%M%S') + '-' + uuid4().hex[:6])
    report = Report(root)
    say('本地报告目录：' + str(report.root.resolve()))
    try:
        report.emit({'event': 'plan', 'protocol': PROTOCOL, 'agent_version': __version__,
                     'execution': 'live_requested' if args.confirm_live else 'offline_preview',
                     'synthetic_only': True, 'material_sha256': hashlib.sha256(json.dumps(rows, ensure_ascii=False).encode()).hexdigest(),
                     'scenario': rows, 'profile': plan.status(), 'request_limit': MAX_CALLS, 'turn_limit': MAX_TURNS,
                     'cost': {'amount': None, 'status': 'unknown', 'generation_max_tokens': 4096,
                              'check_max_tokens': 1200, 'output_token_ceiling': 6 * (4096 + 1200),
                              'formula': 'reported input/cache tokens * applicable input rates + reported output tokens * output rate',
                              'unknowns': 'input tokenization/cache split, current account rates, failed-call charges; request cap is not a currency cap'},
                     'observations': 'No automatic naturalness score. Human review separates content defects, possible false blocks, technical failures, and completed conversation quality.'})
        if not args.confirm_live:
            say('离线预览已导出；未读取凭据、创建用户库或请求模型。--confirm-live 才授权最多 12 次请求及本地合成可见文本记录。')
            return 0
        client = model_access.connect(plan)
        return run_session(client, report, plan, ask=ask or input, say=say)
    except (Exception, KeyboardInterrupt) as exc:
        failure = error_info(exc, getattr(exc, 'stage', 'setup'))
        try:
            report.emit({'event': 'execution_failure', 'status': 'cancelled' if failure['code'] == 'cancelled' else 'technical_failure',
                         'error': failure, 'action': 'stopped; no automatic retry; retain earlier request events'})
        except (OSError, StageError):
            pass
        say('已停止：' + failure['stage'] + '/' + failure['code'] + '；日志可能不完整，请保留本地目录；不会自动重跑。')
        return 1
    finally:
        report.close()
