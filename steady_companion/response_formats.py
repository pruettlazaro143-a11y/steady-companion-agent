"""Host-owned machine schemas, independently scoped to memory, generation and review."""
import re
from copy import deepcopy
from .provider import ProviderError


def _object(properties):
    return {'type':'object', 'properties':properties, 'required':list(properties), 'additionalProperties':False}


_ID={'type':'string', 'pattern':r'^[A-Za-z][A-Za-z0-9_-]{0,31}$'}
_GENERATION=_object({
    'candidates':{'type':'array','minItems':2,'maxItems':3,'items':_object({
        'id':_ID, 'reply':{'type':'string'},
        'evidence':{'type':'array','minItems':1,'maxItems':8,'items':_object({
            'ref':{'type':'string','enum':['U'+str(i) for i in range(8)]},
            'quote':{'type':'string','minLength':1,'maxLength':100}})}})},
    'selected_id':_ID})
_REVIEW=_object({
    'approved_ids':{'type':'array','maxItems':3,'items':_ID},
    'issues':{'type':'array','maxItems':30,'items':_object({
        'candidate_id':_ID, 'code':{'type':'string','enum':[
            'irrelevant','unnecessary_question','repeated_template','premature_closing',
            'unsupported_claim','boundary_violation','too_long','too_short','missed_risk','emoji']}})},
    'repair_hint':{'type':'string','maxLength':500}})


def for_stage(mode, stage):
    if mode not in ('off','json_object','json_schema'):
        raise ProviderError('Invalid response format configuration.',code='configuration')
    if mode=='off' or stage not in ('generation','review','repair_generation','repair_review'):
        return None
    if mode=='json_object':return {'type':'json_object'}
    name='review' if stage.endswith('review') else 'generation'
    schema=_REVIEW if name=='review' else _GENERATION
    return {'type':'json_schema','json_schema':{'name':'steady_'+name,'strict':True,'schema':deepcopy(schema)}}


def for_memory(mode, unit_ids):
    if mode not in ('off','json_object','json_schema'):
        raise ProviderError('Invalid memory format configuration.',code='configuration')
    if not isinstance(unit_ids,(list,tuple)) or not 1 <= len(unit_ids) <= 3 or any(
            not isinstance(i,str) or not re.fullmatch(r'E[0-9a-f]{16}_[0-2]',i) for i in unit_ids) or len(set(unit_ids)) != len(unit_ids):
        raise ProviderError('Invalid memory unit schema.',code='configuration')
    if mode == 'off': return None
    if mode == 'json_object': return {'type':'json_object'}
    schema = _object({'unit_ids':{'type':'array','maxItems':3,
                                 'items':{'type':'string','enum':list(unit_ids)}}})
    return {'type':'json_schema','json_schema':{'name':'steady_memory_selection','strict':True,'schema':schema}}


def for_grounding():
    """One fixed diagnostic schema, never a caller-supplied schema or ID set."""
    schema=_object({
        'status':{'type':'string','enum':['supported','contradicted','not_established']},
        'evidence':{'type':'array','minItems':0,'maxItems':2,'items':_object({
            'turn_id':{'type':'string','enum':['T0','T1','T2']},
            'quote':{'type':'string','minLength':1,'maxLength':160}})}})
    return {'type':'json_schema','json_schema':{
        'name':'steady_claim_grounding_v1','strict':True,'schema':schema}}


def for_assumptions():
    """Fixed, diagnostic-only reply extraction plus self-report grounding schema."""
    fields={
        'reply_quote':{'type':'string','minLength':1,'maxLength':160},
        'claim':{'type':'string','minLength':1,'maxLength':160},
        **for_grounding()['json_schema']['schema']['properties']}
    schema=_object({'claims':{'type':'array','minItems':0,'maxItems':3,'items':_object(fields)}})
    return {'type':'json_schema','json_schema':{
        'name':'steady_reply_assumptions_v1','strict':True,'schema':schema}}


def validate_format(value):
    # Only our versioned schemas: never serialize arbitrary caller fields/secrets.
    try:
        ids = value['json_schema']['schema']['properties']['unit_ids']['items']['enum']
        if value == for_memory('json_schema',ids): return
    except (KeyError,TypeError,ProviderError): pass
    if value not in (for_stage('json_object','generation'),for_stage('json_schema','generation'),for_stage('json_schema','review'),for_grounding(),for_assumptions()):
        raise ProviderError('Unsupported response_format; use the checked stage configuration.',code='configuration')
