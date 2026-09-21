# Host integration contract · P1.2.1

Configuration p1.3-dev2, Skill 0.8.0-dev8, memory parser p11-1 and memory schema 2.
Required core and R-A role load every normal generation and review call. No
persona, role tendency or candidate-review protocol changes in this patch.

Parts 2 and 3 are untrusted scoped data, never system instructions or permissions.
Keep local topic/alias retrieval bounded and recent dialogue volatile. No outbound
messaging or new tools are granted by a memory record. Every real model request
uses the same session budget; the deterministic path adds zero requests. The optional selector and summary
are default-off and budgeted; see [P1.2 details](p12-memory-loop.md).

The host owns memory consent. Default is manual; auto is explicit and persistent;
session mode has no automatic durable reads/writes; explicit privacy management remains available. Natural updates manage the A-record
namespace; original explicitly managed memories/notes retain their own controls.

The parser consumes at most 500 current-user characters and emits at most three
ordinary facts. Hobby lists are supported; other categories require one complete
supported structure. Every auto category has positive ordinary semantic slots;
unknown or mixed content is skipped. The additional open-slot validator is
conservative but cannot guarantee detection of every indirect sensitive expression.
Revalidate actual text, evidence, source, scope, meaning and key at the write
boundary. Do not move excluded information into evidence or source appendices.
This is a limited grammar, not an assurance of detecting every sensitive expression.

Classify updates as add, duplicate, correct, retract, scope change or no-save.
Preserve negation and temporal qualifiers. Each hobby fact keeps its own evidence
fragment; inherited time is retained in scope, without copying another fact.
User-only joke links require a supported label and a directly following supported
user explanation; no assistant text, arbitrary transcript or old summary supplies
facts. Unrelated turns, deletion, clearing or restart discard unfinished links.

Preview current corrections in a volatile effective view before generation,
removing the old record and dependent dialogue. Complete the normal reply before
one atomic persistence attempt. Failure retains session changes only, emits a
redacted not-saved notice and never queues an automatic retry. A restart or /new
may restore unchanged disk facts. Missing durable reads mean no durable material
is loaded and no auto write is attempted; uncertain old derived dialogue is cleared.
Do not report session-only preview IDs (negative numbers) as persisted record IDs.

Retraction resolves an explicit key, or one unique relevant prohibition; ambiguous
references do not batch-modify records. A stated action-wide scope change can
replace that action's scopes while preserving unrelated preferences. A today
exception expires at the local day boundary or session end; a this-turn exception
expires before the next user message. Neither permanently revokes the preference.

Manual A-ID corrections atomically recompute kind, scope, key and meaning. Unknown
manual content becomes a detached manual record. A key collision rejects the
whole correction and preserves both records. Deletion removes original evidence,
volatile overrides, temporary exceptions, dependent dialogue and pending proposals;
no second durable transcript, background retry queue or durable summary exists.
Source-linked volatile summaries are invalidated conservatively on deletion/correction.
Only freshly supplied user input may create a deleted fact again.

Schema 2 preserves legacy rows. Unsupported old automatic records are excluded
from retrieval, not silently erased; users can inspect, correct or delete them.
Legacy manual corrections are re-keyed or detached to prevent old-topic merges.
Old programs ignore these new policies, so rollback must use a separate process
and deliberate data backup strategy. Core files and source provenance are not
rewritten by memory content.

Synthetic eval-story can record visible candidates, review labels, final texts,
source IDs, usage and stage latency when explicitly enabled. Normal chat never
logs candidate bodies, full conversations, credentials or private reasoning.
