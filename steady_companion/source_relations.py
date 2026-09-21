"""Request-local role/adoption references, derived only from actual dialogue.

No facts are extracted and no database or second history is created. An exact
explicit adoption links a fragment for the current task, not a biographical fact.
All other references remain unresolved; assistant contributions are never user
evidence. A link is not an endorsement of the truth of the contributed content.
"""
import re

ADOPT = re.compile(r'^(?:现在|那)?(?:我们)?(?:就用|采用|保留|选定)[“「"]([^”」"\n]{1,120})[”」"]')
RETRACT = re.compile(r'算了|不用|不采用|撤回|不要|别用|先不')
UNCERTAIN = re.compile(r'[？?]|(?:吗|好不好|行不行)[。！]?\s*$|如果|假如|也许|可能|还没决定|没想好')


def relations(history, current):
    messages = [*history, {'role': 'user', 'content': current}]
    start = max(0, len(messages) - 12)
    rows = []
    links = []
    for index in range(start, len(messages)):
        message = messages[index]
        role = message['role']
        rows.append({'ref': 'H'+str(index), 'role': role,
                     'status': 'assistant_contribution_not_user_evidence' if role == 'assistant' else 'user_utterance_not_automatically_fact'})
        text = message['content']
        match = ADOPT.match(text) if role == 'user' and not RETRACT.search(text) and not UNCERTAIN.search(text) else None
        if match:
            # Ambiguous duplicate sources are not guessed; only the retained raw
            # history can supply a source. No links survive removal of that source.
            matches = [i for i in range(start, index) if messages[i]['role'] == 'assistant' and match[1] in messages[i]['content']]
            if len(matches) == 1:
                links.append({'user_ref': 'H'+str(index), 'assistant_ref': 'H'+str(matches[0]),
                              'fragment': match[1], 'user_span': list(match.span(1)),
                              'status': 'explicit_fragment_adoption_for_current_task_only',
                              'scope': 'only the quoted fragment; no expansion to other objects or real user history'})
    return {'protocol': 'dialogue-source-relations-v1', 'refs': rows, 'adoptions': links,
            'default': 'unresolved; role or silence alone does not establish adoption or user facts',
            'memory_write_source': False}
