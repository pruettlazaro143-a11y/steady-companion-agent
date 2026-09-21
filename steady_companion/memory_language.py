"""Closed, auditable grammar for ordinary auto memory, not a sensitivity classifier.

Every persisted evidence clause must be completely consumed by supported syntax
and ordinary semantic slots. Unknown content fails closed in ALL categories.
No model text or arbitrary recent transcript is accepted as evidence.
"""
from __future__ import annotations
import re
import unicodedata

MAX_INPUT = 500
MAX_EVENTS = 3
PARSER_VERSION = 'p11-1'
INTERESTS = {'篮球','足球','游泳','跑步','散步','羽毛球','乒乓球','网球','徒步','登山','骑行',
             '烘焙','做饭','烹饪','咖啡','茶','音乐','古典音乐','电影','科幻','科幻电影','阅读','读书',
             '摄影','画画','绘画','园艺','养花','游戏','电子游戏','桌游','围棋','象棋','吉他','钢琴',
             'basketball','football','swimming','running','music','movies','reading','gardening',
             'cooking','photography','chess','hiking','coffee','tea','guitar','piano','games'}
ALIASES = {'打篮球':'篮球','踢足球':'足球','打羽毛球':'羽毛球','打乒乓球':'乒乓球','打网球':'网球',
           '看电影':'电影','看书':'读书','弹吉他':'吉他','弹钢琴':'钢琴','下围棋':'围棋','下象棋':'象棋'}
TOPICS = INTERESTS | {'成绩','考试','学习','复习','薄荷','天气'}
# Positive slot grammar. This intentionally excludes arbitrary free-text labels.
LABEL = re.compile(r'(?:隐形|低调|安静|快乐|篮球|游泳)?(?:学霸|冠军|队长|选手|球迷)')
PLANT = re.compile(r'(?:一盆|这盆|我的)?(?:蔫)?(?:薄荷|绿萝|仙人掌|植物)')
NAMES = {'坚强','随缘','小绿','小草','薄荷','绿绿'}
PROVENANCE = re.compile(r'假设|假如|如果|虚构|故事里|引用|转述|台词|扮演|也许|可能|大概|好像|似乎|不确定|是不是|是否|朋友说|他说|她说|据说|听说|pretend|suppose|maybe|perhaps|quoted?|said',re.I)


def normalize(text):
    return re.sub(r'[\s，。！？,.!?“”「」"\']+', '', unicodedata.normalize('NFKC', text).lower())


def clean(text):
    return text.strip().rstrip('。.!！')


def event(kind, text, key, meaning, scope, operation='upsert', **extra):
    return dict(kind=kind,text=text.strip(),evidence=text.strip(),key=key,meaning=meaning,
                scope=scope,operation=operation,**extra)


def safe_definition(text):
    # Semantic slots are limited to study-performance and ordinary hobby descriptions.
    return bool(re.fullmatch(r'(?:先说(?:自己)?不会[、，,]?结果(?:考第一|考得很好)|考得很好但平时不声张|成绩很好但(?:平时)?不声张)(?:的)?人',text)
                or re.fullmatch(r'(?:喜欢|每天)(?:打篮球|游泳|踢足球|下棋)(?:的)?人',text))


def joke_parts(text):
    s=clean(text)
    m=re.fullmatch(r'(?:我们(?:圈子里)?|在我们这里)(?:把|管)(.+?)(?:叫作|叫|称为)[“「"](.+?)[”」"]',s)
    if m: return m.group(2),m.group(1)
    m=re.fullmatch(r'(?:我们(?:圈子里)?|在我们这里)[，,]?[“「"](.+?)[”」"](?:改指|是指|指)(.+)',s)
    return (m.group(1),m.group(2)) if m else None


def joke_event(text, label, definition):
    if not LABEL.fullmatch(label) or not safe_definition(definition): return None
    return event('joke',text,'joke:'+normalize(label),normalize(definition),'仅用户所在圈子的叫法')


def _boundary_action(action):
    m=re.fullmatch(r'拿(.+)开玩笑',action)
    if m and m.group(1) in TOPICS: return 'joke:'+m.group(1)
    if action in ('开玩笑',): return 'joke:*'
    if action in ('每句都问问题','每句话都问问题','每句都追问'): return 'questions:every_turn'
    m=re.fullmatch(r'(?:聊|讨论|提)(.+)',action)
    if m and m.group(1) in TOPICS: return 'discuss:'+m.group(1)
    return None


def _parse_preference(text):
    s=clean(text)
    # Explicit scope replacement only for the named action, never all preferences.
    m=re.fullmatch(r'以后只在(?:聊|谈)(.+)时(?:别|不要)开玩笑[，,](?:其他|别的)话题(?:可以|没关系)',s)
    if m and m.group(1) in TOPICS:
        topic=m.group(1)
        return event('preference',text,'boundary:joke:'+topic,'deny','仅拿'+topic+'开玩笑', 'scope_change', family='boundary:joke:')
    suffix=r'(?:[，,](?:别的|其他)话题(?:没关系|可以))?'
    m=re.fullmatch(r'(?:以后|今后)(?:还是)?(?:别|不要)(.+?)'+suffix,s)
    if m:
        action=_boundary_action(m.group(1))
        if action: return event('preference',text,'boundary:'+action,'deny','仅原话指定行为；'+action)
    m=re.fullmatch(r'(?:现在|以后|今后)(?:可以|允许)(.+?)(?:了)?',s)
    if m:
        action=_boundary_action(m.group(1))
        if action: return event('preference',text,'boundary:'+action,'allow','撤回对应长期禁令','retract')
    m=re.fullmatch(r'(?:撤回|取消)(?:之前|以前|刚才)?(?:的)?(?:不|别|不要)(.+?)(?:的要求|的限制|的偏好)',s)
    if m:
        action=_boundary_action(m.group(1))
        if action: return event('preference',text,'boundary:'+action,'allow','撤回对应长期禁令','retract')
    m=re.fullmatch(r'(?:今天|这次)(?:可以|允许)(.*?)(?:了)?(?:[，,]平时(?:还是)?(?:别说|别开玩笑|不要))?',s)
    if m:
        action=_boundary_action(m.group(1)) if m.group(1) else None
        if action or not m.group(1):
            return event('preference',text,'boundary:'+action if action else '', 'allow','仅当前会话的本次例外；不修改长期设置','temporary', expires='turn' if s.startswith('这次') else 'day')
    # A reference can resolve only against ONE currently active prohibition.
    if s in ('撤回刚才的要求','取消之前的限制','这个限制取消吧'):
        return event('preference',text,'','allow','唯一可确定的禁令','retract')
    return None


def parse_events(text):
    """Parse the entire message, or save nothing. Return 0–3 user-grounded events."""
    if not isinstance(text,str) or not 1<=len(text)<=MAX_INPUT or PROVENANCE.search(text): return []
    if any(ord(c)<32 or ord(c)==127 for c in text): return []
    s=clean(text)
    pref=_parse_preference(text)
    if pref: return [pref]
    parts=joke_parts(text)
    if parts:
        e=joke_event(text,*parts)
        return [e] if e else []
    m=re.fullmatch(r'我(?:把|给)(.+?)(?:取名|起名|改名|改叫)(?:叫|为)?[“「"](.+?)[”」"]',s)
    if m and PLANT.fullmatch(m.group(1)) and m.group(2) in NAMES:
        return [event('shared',text,'name:'+normalize(m.group(1)),normalize(m.group(2)),'用户自述命名；不是 AI 亲历')]
    m=re.fullmatch(r'(下次|明天)(?:再|继续)(?:聊|讨论)(.+)',s)
    if m and m.group(2) in TOPICS:
        return [event('thread',text,'thread:'+normalize(m.group(2))+':'+m.group(1),normalize(s),'仅'+m.group(1)+'的未完话题；没有安排提醒')]
    english=re.fullmatch(r'I (like|love|enjoy|dislike|no longer like) ([a-zA-Z -]+)',s,re.I)
    if english and english.group(2).lower() in INTERESTS:
        subject=english.group(2).lower(); negative=english.group(1).lower() in ('dislike','no longer like')
        return [event('interest',text,'interest:'+subject+':',('negative' if negative else 'positive')+subject,'自述兴趣；未指定时间')]
    clauses=re.split(r'[，,；;。]',s)
    if not 1<=len(clauses)<=MAX_EVENTS or any(not c.strip() for c in clauses): return []
    result=[]; inherited_time=''
    for i,clause in enumerate(clauses):
        # First-person may be omitted only in an explicit habitual self-expression;
        # subsequent clauses can inherit the speaker and a temporal qualifier.
        m=re.fullmatch(r'(我)?(平时|最近|现在|这学期|以前)?(也)?(?:很|比较|挺)?(不再喜欢|不喜欢|喜欢|爱好|爱)(.+?)(?:的)?',clause.strip())
        if not m or (i==0 and not m.group(1) and not m.group(2)) or (i>0 and not m.group(1) and not m.group(2) and not m.group(3)): return []
        subject=ALIASES.get(m.group(5),m.group(5))
        if subject not in INTERESTS: return []
        temporal=m.group(2) or (inherited_time if i>0 else '')
        if i==0: inherited_time=temporal
        key_time='' if temporal in ('','现在','平时') else temporal
        # Habitual variants share scope; explicit past/recent ranges never collapse.
        meaning=('negative' if m.group(4).startswith('不') else 'positive')+normalize(subject)
        evidence=text.strip() if len(clauses)==1 else clause.strip()
        result.append(event('interest',evidence,'interest:'+normalize(subject)+':'+key_time,meaning,'自述兴趣；'+(key_time or '未指定时间')))
    keys={}
    for e in result:
        if e['key'] in keys and keys[e['key']]!=e['meaning']: return []
        keys[e['key']]=e['meaning']
    return result


def pending_label(text):
    if not isinstance(text,str) or len(text)>MAX_INPUT or PROVENANCE.search(text): return None
    parts=joke_parts(text)
    if parts and parts[1] in ('这种人','这样的人') and LABEL.fullmatch(parts[0]):
        return {'label':parts[0],'evidence':text.strip()}
    return None


def linked_event(pending,text):
    if not pending or not isinstance(text,str) or len(text)>MAX_INPUT or PROVENANCE.search(text): return None
    m=re.fullmatch(r'(?:就是说|意思是|指的就是|就是指)(.+)',clean(text))
    if not m: return None
    candidate=joke_event(pending['evidence']+'\n'+text.strip(),pending['label'],m.group(1))
    if candidate: candidate['source']='user_linked_turns'
    return candidate


def supported_record(record):
    """Validate all fields against re-parsed evidence immediately before auto write.

    No free-form appendix, source blob or excluded fact can bypass the grammar.
    Mixed/unknown messages are not sliced into a seemingly safe authorization.
    """
    if record.get('source')=='user_semantic_verified':
        from .semantic_memory import supported
        return supported(record)
    evidence=record.get('evidence','')
    if not isinstance(evidence,str): return False
    if '\n' in evidence:
        first,sep,second=evidence.partition('\n')
        candidates=[linked_event(pending_label(first),second)]
    else:
        candidates=parse_events(evidence)
        # A multi-fact continuation is a verbatim fragment with implicit self.
        if not candidates and evidence.startswith('也'):
            temporal=record.get('scope','').removeprefix('自述兴趣；')
            if temporal in ('未指定时间','最近','以前','这学期'):
                candidates=parse_events('我'+('' if temporal=='未指定时间' else temporal)+evidence[1:])
    return any(c and all(record.get(k)==c.get(k) for k in ('kind','key','meaning','scope'))
               and record.get('text')==evidence and record.get('source','user_current_turn') in ('user_current_turn','user_linked_turns')
               for c in candidates)
