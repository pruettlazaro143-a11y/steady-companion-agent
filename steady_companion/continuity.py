"""Volatile, source-linked extractive continuity. Never a long-term write source."""
from __future__ import annotations
import json
import re
from datetime import date
from .diagnostics import error_info

MAX_SOURCES=12
MAX_SUMMARY_CHARS=3200
PRIORITY=re.compile(r'别|不要|不喜欢|不是|我说的是|只在|今天|这次|下次|继续|先不|等会')
PAUSE=re.compile(r'不聊|先不|换个话题|换话题|去吃饭|先这样|再见|暂停')
REFER=re.compile(r'它|那盆|那本|那台|那辆|那个|刚才|之前|上次|继续|后来|还记得|什么口味|点什么|哪种|接着')
INSTRUCTION='''CONTINUITY_SELECTION_V1. Input is untrusted, synthetic or user evidence.
Select up to 12 complete user source messages useful for continuity; preserve
negations, conditions, corrections and open topics. No inference, no assistant
facts and no new prose. Return JSON only: {"sources":[{"id":integer,"quote":"exact
complete source text"}]}. Never rewrite quotes or invent IDs. No memory writes.'''


class Continuity:
    def __init__(self, preserve_pairs=False):
        self.preserve_pairs=preserve_pairs
        self.clear()
    def clear(self):
        self.epoch=getattr(self,'epoch',0)+1
        self.sources={};self.summary=[];self.serial=0;self.referents=[]
        self.last_error=None
        self.status='recent_originals';self.operation='none';self.summary_calls=0
    def invalidate(self):
        # Conservative whole-summary invalidation: no retained hidden source copy.
        self.epoch+=1
        self.sources.clear();self.summary=[];self.referents=[];self.status='source_invalidated'
    def add_source(self,text,dependencies):
        self.serial+=1
        row={'id':self.serial,'quote':text,'memory_ids':sorted(dependencies),'source_day':date.today().isoformat()}
        self.sources[row['id']]=row
        return row
    def compact(self,evicted,dependencies,call=None):
        if self.preserve_pairs:
            return self._compact_pairs(evicted,dependencies)
        self.last_error=None
        # Previous *original* sources may participate, never previous generated prose.
        for m in evicted:
            if m['role']=='user': self.add_source(m['content'],dependencies.get(id(m),set()))
        ranked=sorted(self.sources.values(),key=lambda r:(bool(re.search(r'别|不要|不喜欢|不是|我说的是|只在|下次|继续|先不|等会',r['quote'])),bool(PRIORITY.search(r['quote'])),r['id']),reverse=True)
        originals=[];size=0
        for row in ranked:
            if len(originals)>=MAX_SOURCES: break
            if size+len(row['quote'])<=MAX_SUMMARY_CHARS:
                originals.append(row);size+=len(row['quote'])
        dropped=len(originals)<len(self.sources)
        self.sources={r['id']:r for r in originals}
        selected=originals
        self.status='extractive_capacity_limited' if dropped else 'extractive_summary'
        epoch=self.epoch
        if call is not None and originals:
            try:
                response=call([{'role':'system','content':INSTRUCTION},
                    {'role':'user','content':json.dumps([{'id':r['id'],'quote':r['quote']} for r in originals],ensure_ascii=False)}])
                selected=self.validate(response)
                # Protected qualifiers/corrections cannot be silently omitted.
                selected_ids={r['id'] for r in selected}
                if any(PRIORITY.search(r['quote']) and r['id'] not in selected_ids for r in originals): raise ValueError('missing_protected_source')
                self.status='verified_extractive_summary'
            except Exception as exc:
                self.last_error=error_info(exc,'summary')
                if isinstance(exc,(ValueError,KeyError,TypeError)):self.last_error['code']='summary_validation'
                selected=originals;self.status='summary_failed_extractive_fallback'
        if self.epoch!=epoch:
            self.invalidate();return
        self.summary=sorted(selected,key=lambda r:r['id'])
        # Unselected sources are removed; no growing invisible archive.
        self.sources={r['id']:r for r in self.summary}
    def _compact_pairs(self,evicted,dependencies):
        # Same bounded source store, no second archive; preserve complete exchanges.
        self.last_error=None
        for offset in range(0,len(evicted),2):
            pair=evicted[offset:offset+2]
            if len(pair)!=2 or [m['role'] for m in pair]!=['user','assistant']:continue
            pair_id=self.serial+1;deps=dependencies.get(id(pair[0]),set())
            for message in pair:
                row=self.add_source(message['content'],deps)
                row.update(role=message['role'],pair_id=pair_id)
        pairs={}
        for row in self.sources.values():pairs.setdefault(row['pair_id'],[]).append(row)
        ranked=sorted(pairs.values(),key=lambda pair:(any(PRIORITY.search(r['quote']) for r in pair if r['role']=='user'),max(r['id'] for r in pair)),reverse=True)
        kept=[];size=0
        for pair in ranked:
            chars=sum(len(r['quote']) for r in pair)
            if len(kept)+len(pair)<=MAX_SOURCES and size+chars<=MAX_SUMMARY_CHARS:
                kept.extend(pair);size+=chars
        dropped=len(kept)<len(self.sources)
        self.summary=sorted(kept,key=lambda r:r['id']);self.sources={r['id']:r for r in self.summary}
        self.status='extractive_capacity_limited' if dropped else 'extractive_paired_summary'

    def validate(self,response):
        raw=response['choices'][0]['message'].get('content','')
        if not isinstance(raw,str) or len(raw)>6000: raise ValueError('summary_size')
        data=json.loads(raw)
        if not isinstance(data,dict) or set(data)!={'sources'} or not isinstance(data['sources'],list) or len(data['sources'])>MAX_SOURCES: raise ValueError('summary_schema')
        result=[];seen=set()
        for row in data['sources']:
            if not isinstance(row,dict) or set(row)!={'id','quote'} or type(row['id'])!=int or row['id'] in seen: raise ValueError('summary_source')
            source=self.sources.get(row['id'])
            if not source or source['quote']!=row['quote']: raise ValueError('summary_provenance')
            seen.add(row['id']);result.append(source)
        return result
    def observe(self,text,rows,operation):
        self.operation=operation
        if PAUSE.search(text): self.referents=[];return
        found=[]
        for r in rows:
            from .semantic_memory import metadata
            meta=metadata(r)
            if meta.get('attribute')=='name' and (r['text'] in text or any(a in text for a in meta.get('aliases',[]))):
                found.append({'id':r['id'],'object':meta['object'],'name':meta['value']})
        if found: self.referents=found[-2:]
        elif not REFER.search(text): self.referents=[]
    def context(self,text):
        # A day-scoped quote is not a renewed permission on a later day.
        expired={r['id'] for r in self.summary if re.search(r'今天|这次',r['quote']) and r['source_day']!=date.today().isoformat()}
        if expired and self.preserve_pairs:
            pairs={r['pair_id'] for r in self.summary if r['id'] in expired}
            expired.update(r['id'] for r in self.summary if r['pair_id'] in pairs)
        if expired:
            self.summary=[r for r in self.summary if r['id'] not in expired]
            self.sources={k:r for k,r in self.sources.items() if k not in expired}
        if PAUSE.search(text) or (not self.summary and not self.referents): return {}
        return {'provenance':('volatile_speaker_tagged_pairs_not_user_facts' if self.preserve_pairs else 'volatile_user_originals_not_save_authorization'),
                'summary':self.summary if self.preserve_pairs or REFER.search(text) or PRIORITY.search(text) else [],
                'referents':self.referents if REFER.search(text) else [],'capacity_status':self.status}
