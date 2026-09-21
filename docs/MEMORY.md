# User-controlled memory

`manual` is the new-user default. `session` keeps new information volatile. `auto` permits only supported ordinary preferences, interests, names/shared meanings and unfinished topics after source/scope checks. Sensitive or uncertain material can be skipped, including evidence fields. The finite parser is not a universal sensitive-content detector.

Use `/memory` and `/auto-memory` to inspect stored records. `/correct ID TEXT` and `/auto-correct A1 TEXT` update content and matching metadata; unclassifiable explicit corrections leave automatic merging. `/forget ID`, `/auto-forget A1` (or `all`), `/unlearn N1`, and `/wipe` remove corresponding records and invalidate dependent context. `/wipe` needs explicit terminal confirmation. `/new` is not deletion of saved memory.

Optional `/memory-assist on-confirm-cost` selects request-bound host unit IDs (at most four calls per session, each up to 1200 tokens/20 seconds), never arbitrary evidence or target IDs. `/context-summary on-confirm-cost` and explicit `/learn` also cost requests under the shared cap. `/keep L1` accepts a proposed note. None is silently enabled by this release or by switching providers.

A current correction can apply in-session before durable save. A failed write is reported as unsaved and must not be advertised as cross-session memory. Restart can expose the previously committed state. Failed reads degrade by omitting the affected long-term records. There is no unlimited retry. Revision invalidation prevents stale drafts and pending proposals from committing.

Current dialogue and relevant authorized records go to the chosen inference provider. SQLite files remain local, not application-encrypted. No full private conversation logging is added by ordinary chat. An exported Skill alone cannot enforce these host behaviors. Ordinary memory, interaction recognition and long-duration quality all have untested cases.
