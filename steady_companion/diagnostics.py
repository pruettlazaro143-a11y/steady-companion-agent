"""Bounded diagnostics: stable host codes, no arbitrary exception/model dumps."""
from __future__ import annotations
from copy import deepcopy
import json,re,sqlite3,time,urllib.error
from .provider import ProviderError
from .store import StaleRevisionError

STAGES=frozenset(('interaction_check','memory','generation','review','repair_generation','repair_review','summary','commit','context','setup','logging','unknown'))
CODES=frozenset(('interaction_capacity','interaction_conflict','interaction_unknown','interaction_validation','capability_claim','unknown','json_parse','json_duplicate_fields','json_constant','response_shape','response_size','response_truncated',
 'candidate_count','candidate_fields','candidate_id','evidence_fields','evidence_reference','evidence_quote','visible_text',
 'no_eligible_candidates','review_fields','review_reference','review_conflict','review_rejected','memory_validation','summary_validation',
 'transport','http','timeout','cancelled','budget','context_invalidated','storage','logging_write','configuration','assertion_failed'))
FIELDS=frozenset(('unit_ids','candidates','selected_id','id','evidence','ref','quote','reply','operation','target_id','approved_ids','issues','candidate_id','code','repair_hint','sources','action','length','question_limit'))
BLOCKED=re.compile(r'<\s*/?\s*(?:think|analysis|reasoning)\b|(?:analysis|reasoning|分析|思考)\s*[:：]|\[/?(?:analysis|reasoning|think)\]|<\|.*?(?:analysis|reasoning).*?\|>|authorization|bearer\s|api[_ -]?key|NEBIUS_API_KEY|password|sk-[A-Za-z0-9]|[A-Za-z0-9_-]{32,}',re.I)

class ValidationFault(ValueError):
    def __init__(self,code):self.code=code if code in CODES else 'unknown';super().__init__(self.code)

class StageError(ProviderError):
    def __init__(self,stage,code):
        self.stage=stage if stage in STAGES else 'unknown'
        super().__init__('Stage '+self.stage+' failed ('+(code if code in CODES else 'unknown')+'); no reply was completed.'+(' Review requires at least two remaining requests.' if code=='budget' and stage in ('generation','review','repair_generation','repair_review') else ''),code=code)


def error_info(exc,stage='unknown'):
    code=getattr(exc,'code','unknown')
    if isinstance(exc,KeyboardInterrupt):code='cancelled'
    elif isinstance(exc,TimeoutError):code='timeout'
    elif isinstance(exc,urllib.error.HTTPError):code='http'
    elif isinstance(exc,(ConnectionError,urllib.error.URLError)):code='transport'
    elif isinstance(exc,StaleRevisionError):code='context_invalidated'
    elif isinstance(exc,sqlite3.Error):code='storage'
    elif isinstance(exc,OSError):code='transport' if stage not in ('commit','logging','setup') else ('logging_write' if stage=='logging' else 'storage')
    result={'stage':stage if stage in STAGES else 'unknown','code':code if code in CODES else 'unknown'}
    http_status=getattr(exc,'http_status',None)
    if type(http_status)==int and 300<=http_status<=599:result['http_status']=http_status
    if getattr(exc,"configuration_hint",None)=="check_response_format_capability":
        result["configuration_hint"]="check_response_format_capability"
    return result


def visible_fragment(value,secrets=(),limit=512):
    if not isinstance(value,str):return None
    # Refuse the whole value BEFORE clipping; don't leave partial analysis or keys.
    if BLOCKED.search(value) or any(secret and secret in value for secret in secrets):return None
    if any(ord(c)<32 and c not in '\n\t' for c in value):return None
    return value[:limit]


def capture_response(response,secrets=()):
    """Malformed JSON is metadata-only: guessing text boundaries could expose reasoning."""
    result={'capture':'structure_only'}
    try:
        choice=response['choices'][0];message=choice['message'];raw=message.get('content')
        finish=choice.get('finish_reason');result['finish_reason']=finish if finish in ('stop','length','tool_calls','content_filter') else 'unknown'
        result['content_chars']=len(raw) if isinstance(raw,str) else None
        if not isinstance(raw,str) or len(raw)>65536:return result
        text=raw.strip()
        if text.startswith('```'):
            fence=re.fullmatch(r'```(?:json)?\s*\n(.*?)\n```',text,re.S|re.I)
            if not fence:return result
            text=fence.group(1)
        value=json.loads(text)
        if not isinstance(value,dict):return result
        result['fields']=sorted(k for k in value if k in FIELDS)
        result['unknown_fields_count']=sum(k not in FIELDS for k in value)
        if isinstance(value.get('unit_ids'),list):
            result['unit_ids_count']=len(value['unit_ids'])
            result['unit_ids']=[visible_fragment(v,secrets,32) for v in value['unit_ids'][:3]]
        if 'selected_id' in value:
            result['selected_id']=visible_fragment(value['selected_id'],secrets,64)
        if isinstance(value.get('approved_ids'),list):
            result['approved_ids_count']=len(value['approved_ids'])
            result['approved_ids']=[visible_fragment(v,secrets,64) for v in value['approved_ids'][:3]]
        # No arbitrary keys or fields, provider reasoning, headers, or raw failure dump.
        for field in ('candidates','sources','issues'):
            seq=value.get(field)
            if not isinstance(seq,list):continue
            result[field+'_count']=len(seq);items=[]
            for row in seq[:3]:
                if not isinstance(row,dict):items.append({'type':'non_object'});continue
                item={'fields':sorted(k for k in row if k in FIELDS),'unknown_fields_count':sum(k not in FIELDS for k in row)}
                for key in ('id','reply','quote','operation','target_id','candidate_id','code'):
                    if key not in row:continue
                    v=row[key]
                    if v is None or type(v)==int:item[key]=v
                    elif isinstance(v,str):
                        fragment=visible_fragment(v,secrets,512 if key in ('reply','quote') else 64)
                        item[key+'_chars']=len(v)
                        if fragment is not None:item[key]=fragment
                        else:item[key+'_omitted']=True
                evidence=row.get('evidence')
                if isinstance(evidence,str):
                    item['evidence_chars']=len(evidence)
                    item['evidence_fragment']=visible_fragment(evidence,secrets,100)
                if isinstance(evidence,list):
                    item['evidence_count']=len(evidence);item['evidence']=[]
                    for anchor in evidence[:3]:
                        if not isinstance(anchor,dict):continue
                        clean={k:visible_fragment(anchor[k],secrets,100) for k in ('ref','quote') if k in anchor}
                        clean.update({k+'_chars':len(anchor[k]) for k in ('ref','quote') if isinstance(anchor.get(k),str)})
                        item['evidence'].append(clean)
                items.append(item)
            result[field]=items
        result['capture']='bounded_visible_fields'
    except json.JSONDecodeError as exc:
        result['json_error_position_basis']='normalized_content'
        result['json_error_position']={'line':exc.lineno,'column':exc.colno,'offset':exc.pos}
        result['json_parse_category']=parse_category(exc)
    except (KeyError,IndexError,TypeError,ValueError,RecursionError):pass
    return result


def parse_category(exc):
    # Map Python's fixed parser messages only, never serialize arbitrary exception text.
    return {"Extra data":"trailing_data", "Expecting ',' delimiter":"expected_delimiter",
            "Unterminated string starting at":"unterminated_string",
            "Invalid \\escape":"invalid_escape"}.get(getattr(exc,"msg",None),"unknown")


class Trace:
    def __init__(self,capture=False,secrets=()):
        self.capture=capture;self.secrets=tuple(s for s in secrets if s);self.entries=[];self.current='unknown'
        self.response_status='not_started';self.memory_status='not_triggered'
    def start(self,stage):
        self.current=stage if stage in STAGES else 'unknown'
        row={'stage':self.current,'status':'started','format_mode':'off','calls':0,'usage':None,'_started':time.monotonic()}
        self.entries.append(row);return row
    def row(self):
        return self.entries[-1] if self.entries else self.start(self.current)
    def response(self,response):
        row=self.row();usage=response.get('usage') if isinstance(response,dict) else None
        row['usage']={k:v for k,v in (usage or {}).items() if k in ('prompt_tokens','completion_tokens','total_tokens') and type(v)==int} if isinstance(usage,dict) else None
        if self.capture:row['response_capture']=capture_response(response,self.secrets)
        row['status']='returned'
    def done(self):
        row=self.row();row['status']='completed';row['elapsed_seconds']=round(time.monotonic()-row['_started'],3)
    def fail(self,exc):
        row=self.row();row['status']='failed';row['error']=error_info(exc,row['stage']);row['elapsed_seconds']=round(time.monotonic()-row['_started'],3)
    def recovered(self):
        for row in self.entries:
            if row.get('recovery_pending'):
                row['recovery_outcome']='recovered'
                row.pop('recovery_pending',None)
    def snapshot(self):
        entries=[{k:deepcopy(v) for k,v in r.items() if not k.startswith('_') and k!='recovery_pending'} for r in self.entries[-12:]]
        history=[r['error'] for r in entries if r['status']=='failed']
        failures=[r['error'] for r in entries if r['status']=='failed' and r.get('recovery_outcome')!='recovered']
        rejections=any(r.get('candidate_validation',{}).get('rejected_candidates') for r in entries)
        final_id=next((r['final_selected_id'] for r in reversed(entries) if r.get('final_selected_id')),None)
        terminal=self.response_status=='failed' or (bool(failures) and self.response_status!='completed')
        outcome=('terminal_failure' if terminal else 'completed_with_warnings' if failures or self.memory_status in ('extraction_failed','commit_failed','read_failed') else
                 'recovered' if history else 'completed_with_rejections' if rejections else 'completed')
        return {'response_status':self.response_status,'memory_status':self.memory_status,
                'warnings':failures if self.response_status=='completed' else [],
                'stages':entries,'candidate_rejections_present':rejections,'final_selected_id':final_id,'completed_stages':[r['stage'] for r in entries if r['status']=='completed'],
                'failure':failures[-1] if failures else None, 'failure_history':history,
                'outcome':outcome}


def safe_log(value,secrets=(),key='',depth=0):
    """Final log-boundary scrub, including synthetic inputs and final visible text."""
    if depth>16:return '[omitted depth]'
    if isinstance(value,dict):
        return {k:safe_log(v,secrets,k,depth+1) for k,v in list(value.items())[:100]
                if k.lower() not in ('reasoning','reasoning_content','analysis','headers','request_headers','authorization','api_key')}
    if isinstance(value,list):return [safe_log(v,secrets,key,depth+1) for v in value[:100]]
    if isinstance(value,str):
        if key=='configuration_hint' and value=='check_response_format_capability':return value
        if key=='event' and value in ('new_session_same_user','synthetic_host_command','synthetic_mismatch_injection_NOT_model_output'):return value
        if ('sha256' in key or 'fingerprint' in key) and re.fullmatch(r'[0-9a-f]{16,64}',value):return value
        fragment=visible_fragment(value,secrets,12000)
        return fragment if fragment is not None else '[omitted unsafe text; chars='+str(len(value))+']'
    if value is None or type(value) in (int,float,bool):return value
    return '[omitted unsupported value]'
