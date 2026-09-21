# P1.2 host contract

The four-part architecture, common core, R-A and response candidate review remain
unchanged. Memory stages and state names are internal, never a visible per-turn
checklist. Retrieval does not require mentioning a stored fact; honor pauses.

`/memory-assist on-confirm-cost` enables optional ordinary-memory selection;
`/context-summary on-confirm-cost` enables optional extractive continuity selection.
Each setting defaults off, persists per user and requires a literal cost-bearing
opt-in. Both use the configured Nebius adapter. The first needs auto mode,
unsupported deterministic syntax and a memory cue. Max 500 current-user chars,
3 indivisible host evidence units, 4 calls/session. The current selector returns
only request-scoped IDs; see [unit selection](p124-unit-selection.md). No
assistant draft, inferred persona or full private database enters this request.
The host reconstructs object/property/value/time/condition from complete units,
validates all fields at commit, and matches update targets by exact host identity.
The model supplies no evidence text, targets or permissions.

Evidence-supported normalization is marked `user_evidence_normalized` in scope;
source `user_semantic_verified` denotes host validation, not guaranteed semantic
truth. Open ordinary-domain slots allow unseen names and media/food descriptions.
Mixed sensitive/unknown clauses, reported speech and uncertainty skip storage.
This bounded language and domain validator can miss valid ordinary expressions;
indirect sensitive terms can evade guards. Do not claim universal sensitivity
recognition or tested model accuracy. No model confidence score grants permission.

Effective preview, committed records, temporary exceptions, superseded/retracted
records and uncommitted failures are distinct host states. Time and conditions
participate in identity. Current action is not a rewritten underlying preference.
Scope replacement checks durable records even under temporary masks. A duplicate
must match committed storage too; a new explicit correction can retry once, but
there is no queued automatic retry. Failures are session-only until a real commit.

At 16 messages or 18,000 history characters, the host may retain at most 12 whole
user-source extracts / 3,200 characters outside the recent window. An optional
model may only select those exact quotes (max 1 call/session). No rewritten prose,
assistant guesses or recursive generated summaries become evidence. Protected
negations, corrections and conditions cannot be dropped by the selector. Source
version changes discard the result; deletion/correction clears derived state.
Failed selection uses bounded originals and a capacity notice. Summary is volatile
and cannot authorize a long-term save. Short chats need no summary request.

Auxiliary calls share the response budget, with 20-second transport timeouts,
late-response rejection and at most 1,200 output tokens. Failed/cancelled requests
count. No automatic retry or background work. Deterministic truncation remains
available. Ordinary long-term eviction is an unimplemented interface, not a policy
that removes important memories based on access count.
