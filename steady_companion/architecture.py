"""Versioned part 4 and bounded lexical part 2. No network or write tools."""
from __future__ import annotations
import hashlib
import json
from pathlib import Path
import re
from .provider import ProviderError

ROOT = Path(__file__).parent / 'skill'


def tokens(text):
    words = set(re.findall(r'[a-z0-9_]{2,}', text.lower()))
    for run in re.findall(r'[\u3400-\u9fff]+', text):
        words.update(run[i:i+2] for i in range(len(run)-1))
    return words


def read_bounded(path, limit=20000):
    if path.is_symlink() or not path.is_file() or path.stat().st_size > limit * 4:
        raise ValueError('Missing, oversized or linked configuration')
    body = path.read_text(encoding='utf-8')
    if not body.strip() or len(body) > limit:
        raise ValueError('Empty or oversized configuration')
    return body


class Architecture:
    def __init__(self, root=None):
        self.root = Path(root) if root is not None else ROOT
        self.reload()

    def reload(self):
        try:
            config_text = read_bounded(self.root / 'runtime.json', 2000)
            self.config = json.loads(config_text)
            c = self.config
            if not isinstance(c, dict) or not re.fullmatch(r'[A-Za-z0-9_-]{1,40}', c['role_id']):
                raise ValueError('Role ID')
            for key, cap in [('topics_max_records', 5), ('topics_max_chars', 6000),
                             ('memory_max_records', 12), ('memory_max_chars', 6000)]:
                if type(c[key]) is not int or not 1 <= c[key] <= cap:
                    raise ValueError('Invalid budget')
            if not all(isinstance(c[k], str) and 1 <= len(c[k]) <= 80 for k in ('version', 'core_version')):
                raise ValueError('Version')
            from .pipeline import SELECTION_PROTOCOL
            if c.get('selection_protocol') != SELECTION_PROTOCOL:
                raise ValueError('Selection protocol mismatch')
            self.core = read_bounded(self.root / 'core.md', 6000)
            role_text = read_bounded(self.root / 'roles' / (c['role_id'] + '.json'), 6000)
            self.role = json.loads(role_text)
            if (self.role['role_id'] != c['role_id'] or self.role['core_ref'] != c['core_version']
                or not isinstance(self.role['version'], str) or not self.role['version']
                or not self.role.get('description') or not self.role.get('style_tendencies')
                or c['core_version'] not in self.core):
                raise ValueError('Core/role version mismatch')
            self.fingerprint = hashlib.sha256((config_text+self.core+role_text).encode()).hexdigest()[:16]
            self.instruction = '\nPART 4 COMMON CORE:\n' + self.core + '\nCURRENT ROLE:\n' + role_text
            self.index = []
            paths = sorted((self.root / 'topics').glob('*.json'))
            if len(paths) > 200:
                raise ValueError('Topic index exceeds 200 documents')
            seen = set()
            for path in paths:
                d = json.loads(read_bounded(path, 12000))
                for key in ('id', 'topic', 'body', 'source', 'updated_at', 'scope'):
                    if not isinstance(d[key], str) or not d[key].strip():
                        raise ValueError('Invalid topic record')
                if d['id'] in seen or not isinstance(d['aliases'], list) or len(d['aliases']) > 20:
                    raise ValueError('Duplicate topic or invalid aliases')
                if any(not isinstance(a, str) or not 1 <= len(a) <= 100 for a in d['aliases']):
                    raise ValueError('Invalid alias')
                seen.add(d['id'])
                self.index.append(d)
        except (OSError, ValueError, TypeError, KeyError) as exc:
            raise ProviderError('P1 核心、角色或资料配置缺失/无效；请检查 skill/core.md、runtime.json、roles 和 topics。未按正常角色发出请求。') from None

    def retrieve(self, query):
        if re.search(r'不想聊|不要聊|别聊|先不|暂停|stop|don.t (?:discuss|talk)', query, re.I):
            return [], 'current_refusal'
        ranked = []
        for d in self.index:
            names = [d['topic'], *d['aliases']]
            named = any(a.lower() in query.lower() for a in names)
            # Topic names/aliases gate retrieval; body overlap only ranks results.
            if named:
                ranked.append((len(tokens(query) & tokens(d['body'])), d['id'], d))
        selected, size = [], 2
        for _, _, d in sorted(ranked, key=lambda x: (-x[0], x[1])):
            record = dict(d)
            # Paragraph snippets only: do not silently cut a negation mid-sentence.
            paragraphs = d['body'].split('\n\n')
            ranked_p = sorted(enumerate(paragraphs), key=lambda p: (-len(tokens(query) & tokens(p[1])), p[0]))
            record['body'] = ''
            for _, paragraph in ranked_p:
                candidate = dict(record, body=(record['body']+'\n\n'+paragraph).strip())
                if len(json.dumps(candidate, ensure_ascii=False)) + size + 2 <= self.config['topics_max_chars']:
                    record = candidate
            if record['body']:
                size += len(json.dumps(record, ensure_ascii=False)) + 2
                selected.append(record)
            if len(selected) == self.config['topics_max_records']:
                break
        return selected, 'topic_or_alias_match' if selected else 'no_relevant_local_material'

    def status(self):
        return {'config_version': self.config['version'], 'config_sha256': self.fingerprint,
                'core_version': self.config['core_version'], 'selection_protocol': self.config['selection_protocol'], 'role_id': self.role['role_id'],
                'role_version': self.role['version'], 'topic_documents': len(self.index),
                'skill_path': str((self.root/'SKILL.md').resolve())}
