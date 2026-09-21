"""Single visible-text generation with at most one bounded protocol recovery."""
from copy import deepcopy
import re,unicodedata
from .diagnostics import StageError,Trace
from .provider import ProviderError
from .output_checks import inspect_reply,strip_emoji,_unquoted_text
from .pipeline import MAX_REPLY_CHARS,PipelineResult

VERSION='companion-text-v1'
RECOVERABLE=frozenset(('visible_text','response_size','response_truncated','response_shape','capability_claim'))
HIDDEN=re.compile(r'<\s*/?\s*(?:think|analysis|reasoning)\b|\[/?(?:analysis|reasoning|think)\]|<\|[^>]*(?:analysis|reasoning)[^>]*\|>',re.I)
# Narrow performed-action claims, not a semantic or clinical classifier.
CAPABILITY=re.compile(r'(?:^|[。！!\n])\s*(?:我(?:已经|已)?(?:记住了|记下了|永久保存了)|我(?:已经|已)将你的[^。\n]{0,60}(?:存入|保存到)长期记忆|我(?:已经|已)(?:替你|为你)(?:报警|联系了你的家人)|I have (?:permanently saved your|called (?:the police|your family)))',re.I)

def visible_response(response,emoji_mode,secrets=()):
    try:
        choice=response['choices'][0];msg=choice['message']
        if choice.get('finish_reason')=='length':raise StageError('generation','response_truncated')
        if choice.get('finish_reason') not in (None,'stop') or not isinstance(msg,dict) or msg.get('tool_calls'):raise StageError('generation','response_shape')
        text=msg.get('content')
        if not isinstance(text,str) or not text.strip():raise StageError('generation','visible_text')
        if len(text)>MAX_REPLY_CHARS:raise StageError('generation','response_size')
        if HIDDEN.search(text) or any(secret and secret in text for secret in secrets):raise StageError('generation','visible_text')
        if any((unicodedata.category(c)=='Cc' and c not in '\n\t') or c in '\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069' for c in text):raise StageError('generation','visible_text')
        if CAPABILITY.search(_unquoted_text(text)):raise StageError('generation','capability_claim')
        # Known JSON protocol envelopes are not natural-language output. Do not parse drafts out of them.
        if text.lstrip().startswith(('```json','{"candidates"','{"reply"','{"approved_ids"')):raise StageError('generation','response_shape')
        if emoji_mode=='off':text=strip_emoji(text)
        if not text.strip():raise StageError('generation','visible_text')
        return text.strip()
    except (KeyError,IndexError,TypeError,AttributeError):raise StageError('generation','response_shape') from None

def run_companion(call,messages,*,recent_assistant,emoji_mode='off',max_calls=2,allow_recovery=True,trace=None):
    trace=trace or Trace();budget=min(max_calls,2 if allow_recovery else 1)
    if budget<1:raise ProviderError('Companion reply requires one remaining generation request; no reply completed or automatic budget increase.',code='budget')
    original=deepcopy(messages);error=None
    for attempt in range(budget):
        stage='generation' if attempt==0 else 'repair_generation';trace.start(stage)
        request=deepcopy(original)
        if error:request.append({'role':'system','content':'HOST OUTPUT ERROR: '+error+'. Give one visible natural-language reply from the original conversation; do not repeat protocol data or failed output.'})
        try:
            response=call(request)
            text=visible_response(response,emoji_mode,trace.secrets)
        except (Exception,KeyboardInterrupt) as exc:
            trace.fail(exc)
            # No transport/auth/timeout/cancellation/context retry; no failed text re-prompt.
            code=getattr(exc,'code','unknown')
            if isinstance(exc,ProviderError) and code in RECOVERABLE and attempt+1<budget:
                trace.row()['recovery_kind']='output_protocol';trace.row()['recovery_pending']=True
                trace.row()['recovery_outcome']='terminal_failure';error=code;continue
            raise
        trace.done();trace.recovered()
        soft=[{'code':i.code,'severity':i.severity} for i in inspect_reply(text,emoji_mode=emoji_mode,recent_assistant=recent_assistant) if i.severity=='soft']
        return PipelineResult(text,{'mode':'companion','protocol':VERSION,'semantic_review':False,
            'host_output_checks':'limited_protocol_only','style_observations':soft,'calls':attempt+1,'recovery_requests':attempt})
