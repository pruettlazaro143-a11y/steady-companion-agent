"""Transactional, bounded memory updates with pure previews and exact provenance."""
from __future__ import annotations
from copy import deepcopy
import json
import uuid
import hashlib
from datetime import date, timedelta
from .store import _timestamp, StaleRevisionError
from .architecture import tokens
from .memory_language import (parse_events, supported_record, pending_label, linked_event,
                              normalize, MAX_EVENTS, PARSER_VERSION)

MODES = ('session','manual','auto')
MAX_RECORDS = 200


def extract(text):
    """Compatibility helper. New code uses the bounded multi-event planner."""
    events=parse_events(text)
    return events[0] if len(events)==1 else None


def record_from_event(e, identifier=-1):
    now=_timestamp()
    return dict(id=identifier,kind=e['kind'],text=e['text'],scope=e['scope'],
                source=e.get('source','user_current_turn'),evidence=e['evidence'],
                created_at=now,updated_at=now,status='active',persistence='session_only_preview',semantic_key=e['key'],
                meaning=e['meaning'],merge_policy='auto',parser_version=PARSER_VERSION)


def usable(row):
    if row.get('status')!='active' or row.get('merge_policy')=='excluded_legacy': return False
    if row.get('source')=='user_command': return True
    return supported_record(dict(row,key=row['semantic_key']))


def plan_update(text, rows, pending=None, base_rows=None, *, candidates=None):
    """No I/O: classify and preview current USER input before model generation.

    Only explicit keys are merged. A deictic retraction requires one unique
    preference. Missing joke definitions may link to one immediately following
    user turn; no assistant transcript is ever scanned for facts.
    """
    events=parse_events(text) if candidates is None else candidates
    linked=linked_event(pending,text) if not events else None
    if linked: events=[linked]
    next_pending=pending_label(text)
    changes=[]; outcomes=[]; effective={r['semantic_key']:deepcopy(r) for r in rows if usable(r)}
    overrides={}; impacted=[]; seen_events=set();temporary_events={}
    base={r['semantic_key']:r for r in (rows if base_rows is None else base_rows) if usable(r)}
    for e in events[:MAX_EVENTS]:
        op=e['operation']; key=e['key']
        if not key and op in ('retract','temporary'):
            targets=[r for r in effective.values() if r['kind']=='preference' and r['meaning']=='deny' and r.get('merge_policy')=='auto']
            if len(targets)!=1:
                outcomes.append('no_save_ambiguous_target'); continue
            key=targets[0]['semantic_key']
        signature=(key,e['meaning'],e['scope'],op)
        if signature in seen_events:
            outcomes.append('duplicate'); continue
        seen_events.add(signature)
        old=effective.get(key) or base.get(key)
        if op in ('retract','temporary'):
            if not old or old.get('merge_policy')!='auto':
                outcomes.append('no_save_missing_target'); continue
            effective.pop(key,None);overrides[key]=None;impacted.append(old)
            if op=='temporary': temporary_events[key]=e
            if op=='retract': changes.append(dict(action='retract',key=key,old=old,event=e))
            outcomes.append('retract' if op=='retract' else 'temporary')
            continue
        if op=='scope_change':
            # A temporary exception changes applicability, not durable ownership.
            targets=[r for r in (base | effective).values() if r['semantic_key'].startswith(e['family']) and r.get('merge_policy')=='auto']
            for target in targets:
                if target['semantic_key']!=key:
                    effective.pop(target['semantic_key'],None); overrides[target['semantic_key']]=None;impacted.append(target)
                    changes.append(dict(action='retract',key=target['semantic_key'],old=target,event=e))
        if old and old.get('merge_policy')!='auto':
            outcomes.append('no_save_manual_conflict'); continue
        committed=base.get(key)
        if (old and committed and old['id']>0 and committed['id']==old['id']
            and old['meaning']==committed['meaning']==e['meaning']
            and old['scope']==committed['scope']==e['scope']):
            outcomes.append('duplicate'); continue
        if not supported_record(e):
            outcomes.append('no_save_unsupported_scope'); continue
        action='scope_change' if op=='scope_change' else ('correct' if old and old['id']>0 else 'add')
        row=record_from_event(e,old['id'] if old else -int(hashlib.sha256(key.encode()).hexdigest()[:12],16))
        if old: row['created_at']=old['created_at']; impacted.append(old)
        effective[key]=row; overrides[key]=row
        changes.append(dict(action=action,key=key,old=old,event=e,record=row)); outcomes.append(action)
    return dict(changes=changes,outcomes=outcomes or ['no_save'],effective=list(effective.values()),
                overrides=overrides,impacted=impacted,pending=next_pending,temporary_events=temporary_events)


class NaturalMemory:
    def __init__(self, store):
        self.store=store
        with store._connection() as c:
            c.executescript('''CREATE TABLE IF NOT EXISTS p1_settings(key TEXT PRIMARY KEY,value TEXT NOT NULL);
            INSERT OR IGNORE INTO p1_settings VALUES('memory_mode','manual');
            CREATE TABLE IF NOT EXISTS p1_memories(
              id INTEGER PRIMARY KEY AUTOINCREMENT, kind TEXT NOT NULL, text TEXT NOT NULL,
              scope TEXT NOT NULL, source TEXT NOT NULL, evidence TEXT NOT NULL,
              created_at TEXT NOT NULL, updated_at TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'active',
              semantic_key TEXT UNIQUE NOT NULL, meaning TEXT NOT NULL);
            ''')
        self._migrate()

    def _migrate(self):
        """Preserve every row; exclude unverified legacy auto data from retrieval.

        Explicit manual corrections are re-keyed if safe, or detached from
        automatic merging. Collisions never erase a different record.
        """
        with self.store._transaction() as c:
            fields={r[1] for r in c.execute('PRAGMA table_info(p1_memories)')}
            for name,default in [('merge_policy','legacy'),('parser_version','p1')]:
                if name not in fields:
                    c.execute("ALTER TABLE p1_memories ADD COLUMN "+name+" TEXT NOT NULL DEFAULT '"+default+"'")
            done=c.execute("SELECT value FROM p1_settings WHERE key='memory_schema'").fetchone()
            if done and done[0]=='2': return
            rows=[dict(r) for r in c.execute('SELECT * FROM p1_memories ORDER BY id')]
            for row in rows:
                e=extract(row['text'])
                safe=bool(e and e['operation']=='upsert' and supported_record(e) and row['evidence']==row['text'])
                if row['source']=='user_command':
                    safe=bool(e and e['operation']=='upsert' and supported_record(e))
                    policy='auto' if safe else 'manual'
                else:
                    policy='auto' if safe else 'excluded_legacy'
                key=e['key'] if safe else 'manual:'+str(row['id'])
                conflict=c.execute('SELECT id FROM p1_memories WHERE semantic_key=? AND id<>?',(key,row['id'])).fetchone()
                if conflict:
                    key='manual:'+str(row['id']); policy='manual' if row['source']=='user_command' else 'excluded_legacy'
                c.execute('UPDATE p1_memories SET semantic_key=?,kind=?,scope=?,meaning=?,merge_policy=?,parser_version=? WHERE id=?',
                          (key,e['kind'] if safe else row['kind'],e['scope'] if safe else row['scope'],
                           e['meaning'] if safe else row['meaning'],policy,PARSER_VERSION,row['id']))
            c.execute("INSERT OR REPLACE INTO p1_settings VALUES('memory_schema','2')")
            if rows: self.store._bump(c)

    @property
    def mode(self):
        with self.store._connection() as c:
            value=c.execute("SELECT value FROM p1_settings WHERE key='memory_mode'").fetchone()[0]
        if value not in MODES: raise ValueError('记忆设置无效；请使用 /memory-mode manual 修复。')
        return value

    def set_mode(self,mode):
        if mode not in MODES: raise ValueError('用法：/memory-mode session|manual|auto')
        with self.store._transaction() as c:
            c.execute("UPDATE p1_settings SET value=? WHERE key='memory_mode'",(mode,));self.store._bump(c)

    def feature(self,name):
        if name not in ('memory_assist','context_summary'): raise ValueError('unknown feature')
        with self.store._connection() as c:
            row=c.execute('SELECT value FROM p1_settings WHERE key=?',(name,)).fetchone()
        return bool(row and row[0]=='on')

    def set_feature(self,name,enabled):
        if name not in ('memory_assist','context_summary') or type(enabled)!=bool: raise ValueError('unknown feature')
        with self.store._transaction() as c:
            c.execute('INSERT OR REPLACE INTO p1_settings VALUES(?,?)',(name,'on' if enabled else 'off'))
            self.store._bump(c)

    def eviction_candidates(self):
        """Reserved interface: ordinary long-term automatic eviction is not implemented."""
        return []

    def list(self):
        with self.store._connection() as c:
            return [dict(r) for r in c.execute("SELECT * FROM p1_memories WHERE status='active' ORDER BY id")]

    @staticmethod
    def select_rows(rows,query,max_records=8,max_chars=4000,recent=''):
        from .semantic_memory import metadata
        from .continuity import PAUSE, REFER
        if PAUSE.search(query): return []
        # Carry context only for an explicit reference; a fresh topic doesn't drag old facts.
        context=query+' '+(recent[-1000:] if REFER.search(query) else '')
        ranked=[]
        for item in rows:
            if not usable(item): continue
            meta=metadata(item)
            if item['kind']=='thread' and item['semantic_key'].endswith(':明天'):
                try:
                    if date.today()>date.fromisoformat(item['created_at'][:10])+timedelta(days=1): continue
                except ValueError: continue
            temporal=meta.get('time','')
            if temporal in ('今天','这次','这周') and item['created_at'][:10]!=_timestamp()[:10]: continue
            # Past observations require a past question, not advice about today.
            past=('以前' in item['scope'] or '年前' in temporal or temporal=='过去')
            if past and not any(w in query for w in ('以前','之前','过去','年前','还记得')): continue
            overlap=len(tokens(context)&tokens(item['text']))
            aliases=meta.get('aliases',[])
            exact=any(a and a in context for a in aliases)
            # Transparent category aliases, not inferred traits or trained weights.
            if meta.get('attribute')=='liking' and any(w in context for w in ('故事','小说','哪类')) and any(w in meta.get('object','') for w in ('小说','故事')): exact=True
            key=item['semantic_key']
            boundary=item['kind']=='preference'
            if key.startswith('boundary:joke:') and key!='boundary:joke:*':
                if key.removeprefix('boundary:joke:') not in context: continue
            if key.startswith('boundary:discuss:') and key.removeprefix('boundary:discuss:') not in context: continue
            if meta.get('attribute')=='communication' and not (exact or meta.get('object','') in context): continue
            if overlap or exact or boundary:
                ranked.append((int(boundary),int(exact),overlap,item['updated_at'],item))
        result=[];size=2
        for *_,item in sorted(ranked,key=lambda r:r[:4],reverse=True):
            record={k:v for k,v in item.items() if k not in ('semantic_key','meaning','merge_policy','parser_version')}
            record.setdefault('persistence','committed')
            length=len(json.dumps(record,ensure_ascii=False))+2
            if size+length<=max_chars: result.append(record);size+=length
            if len(result)==max_records: break
        return result

    def select(self,query,max_records=8,max_chars=4000):
        if self.mode=='session': return []
        return self.select_rows(self.list(),query,max_records,max_chars)

    def save_current(self,text,expected_revision,*,plan=None):
        """Commit one current-input batch once, atomically; no retry queue.

        Preview is supplied by the conversation for immediate user-only joke
        linking and volatile overrides. Storage rechecks permission, revision,
        provenance scope and collision ownership while holding the write lock.
        """
        with self.store._transaction() as c:
            revision=c.execute("SELECT value FROM metadata WHERE key='revision'").fetchone()[0]
            if revision!=expected_revision: raise StaleRevisionError('记忆状态已变化；本轮没有自动写入。')
            if c.execute("SELECT value FROM p1_settings WHERE key='memory_mode'").fetchone()[0]!='auto':
                return {'status':'disabled','ids':[],'outcomes':['no_save']}
            if plan is None:
                rows=[dict(r) for r in c.execute("SELECT * FROM p1_memories WHERE status='active'")]
                plan=plan_update(text,rows)
            changes=plan['changes']
            if not changes:
                return {'status':'duplicate' if 'duplicate' in plan['outcomes'] else 'no_eligible_new_fact','ids':[], 'outcomes':plan['outcomes']}
            # Limits apply to the complete transaction, including existing legacy rows.
            count=c.execute('SELECT COUNT(*) FROM p1_memories').fetchone()[0]
            adds=sum(x['action']!='retract' and (not x['old'] or x['old']['id']<0) for x in changes)
            deletes=sum(x['action']=='retract' and x['old']['id']>0 for x in changes)
            if count+adds-deletes>MAX_RECORDS: return {'status':'storage_limit','ids':[],'outcomes':plan['outcomes']}
            ids=[];saved_rows=[];changed=False
            for change in changes:
                old=change['old'];identifier=old['id'] if old and old['id']>0 else None
                current=c.execute('SELECT * FROM p1_memories WHERE semantic_key=?',(change['key'],)).fetchone()
                if current and (identifier is None or current['id']!=identifier):
                    raise StaleRevisionError('匹配标识冲突；本轮没有自动写入。')
                e=change['event']
                from .semantic_memory import units
                permission=c.execute("SELECT value FROM p1_settings WHERE key='memory_assist'").fetchone()
                current_events=parse_events(text)
                if permission and permission[0]=='on' and not current_events: current_events=units(text)
                linked=(e.get('source')=='user_linked_turns' and e['evidence'].partition('\n')[2]==text.strip() and supported_record(e))
                if e not in current_events and not linked:
                    raise ValueError('候选不属于当前获准的用户输入。')
                # Retracts are grammar-validated too, even though no evidence is retained.
                candidates=parse_events(e['evidence'])
                if change['action']=='retract':
                    if not any(x['operation'] in ('retract','scope_change') and x==e for x in candidates):
                        raise ValueError('撤回依据未通过范围检查。')
                    key=change['key']
                    authorized=(e['operation']=='scope_change' and key.startswith(e['family'])) or (e['operation']=='retract' and e['key']==key)
                    if e['operation']=='retract' and not e['key']:
                        targets=c.execute("SELECT semantic_key FROM p1_memories WHERE kind='preference' AND meaning='deny' AND merge_policy='auto'").fetchall()
                        authorized=len(targets)==1 and targets[0][0]==key
                    if not authorized or (current and (current['kind']!='preference' or current['merge_policy']!='auto')):
                        raise ValueError('撤回目标不在当前授权范围内。')
                    if identifier:
                        c.execute('DELETE FROM p1_memories WHERE id=?',(identifier,));changed=True
                    continue
                if e.get('source')=='user_semantic_verified':
                    from .semantic_memory import units
                    permission=c.execute("SELECT value FROM p1_settings WHERE key='memory_assist'").fetchone()
                    if not permission or permission[0]!='on' or e not in units(text):
                        raise ValueError('语义候选未通过当前输入与授权检查。')
                if not supported_record(e): raise ValueError('自动保存范围未通过检查。')
                row=change['record'];now=_timestamp()
                if (not supported_record(dict(row,key=row['semantic_key']))
                    or any(row[k]!=e[k] for k in ('text','evidence','kind','scope','meaning'))
                    or row['semantic_key']!=change['key'] or e['key']!=change['key']):
                    raise ValueError('写入字段与已验证的用户依据不一致。')
                fields=(row['kind'],row['text'],row['scope'],row['source'],row['evidence'],now,row['semantic_key'],row['meaning'],PARSER_VERSION)
                if identifier:
                    cur=c.execute("UPDATE p1_memories SET kind=?,text=?,scope=?,source=?,evidence=?,updated_at=?,semantic_key=?,meaning=?,merge_policy='auto',parser_version=? WHERE id=?",fields+(identifier,))
                    if not cur.rowcount: raise StaleRevisionError('更正目标已删除；未重建旧记录。')
                else:
                    cur=c.execute("INSERT INTO p1_memories(kind,text,scope,source,evidence,updated_at,semantic_key,meaning,parser_version,created_at,merge_policy,status) VALUES(?,?,?,?,?,?,?,?,?,?,'auto','active')",fields+(now,))
                    identifier=cur.lastrowid
                ids.append(identifier);saved_rows.append(dict(c.execute('SELECT * FROM p1_memories WHERE id=?',(identifier,)).fetchone()));changed=True
            if changed: self.store._bump(c)
            status=('retracted' if not saved_rows else 'scope_changed' if 'scope_change' in plan['outcomes'] else 'corrected' if 'correct' in plan['outcomes'] else 'saved')
            return dict(status=status,ids=ids,rows=saved_rows,revision_delta=int(changed),outcomes=plan['outcomes'])

    def correct(self,identifier,text):
        if not isinstance(text,str) or not text.strip() or len(text)>500: raise ValueError('更正内容须为 1–500 字符。')
        e=extract(text);auto=bool(e and e['operation']=='upsert' and supported_record(e))
        with self.store._transaction() as c:
            old=c.execute('SELECT * FROM p1_memories WHERE id=?',(identifier,)).fetchone()
            if old is None: raise ValueError('找不到这条 A 编号记忆。')
            key=e['key'] if auto else 'manual:'+uuid.uuid4().hex
            conflict=c.execute('SELECT id FROM p1_memories WHERE semantic_key=? AND id<>?',(key,identifier)).fetchone()
            if conflict: raise ValueError('匹配标识与 A'+str(conflict['id'])+' 冲突；两条记录均未修改。请明确选择要保留或删除的记录。')
            c.execute("UPDATE p1_memories SET text=?,evidence=?,source='user_command',semantic_key=?,kind=?,scope=?,meaning=?,merge_policy=?,parser_version=?,updated_at=? WHERE id=?",
                      (text.strip(),text.strip(),key,e['kind'] if auto else 'manual',e['scope'] if auto else '仅用户明确更正的原文范围；不参与自动合并',
                       e['meaning'] if auto else normalize(text),'auto' if auto else 'manual',PARSER_VERSION,_timestamp(),identifier))
            self.store._bump(c)
        return dict(old)

    def forget(self,identifier=None):
        with self.store._transaction() as c:
            where=' WHERE id=?' if identifier is not None else '';params=(identifier,) if identifier is not None else ()
            old=[dict(r) for r in c.execute('SELECT * FROM p1_memories'+where,params)]
            c.execute('DELETE FROM p1_memories'+where,params)
            if old: self.store._bump(c)
        return old
