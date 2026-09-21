# P1.2.4 host memory selection and completion states

Host debugging only. Retain the [candidate isolation contract](p123-candidates.md).
Memory assistance selects request-scoped unit IDs from complete host units,
including conditions and negations. IDs contain an ephemeral request marker;
they are not persistent memory IDs and cannot be reused across requests/users.
No legal units means no memory request. Empty selection means no nomination.
Unknown, duplicated or stale IDs and old free-text candidate responses are rejected.
The host restores records from its own table, rechecks grounding and permissions,
and matches update targets by existing exact semantic identity. It never repairs
model fragments or treats selection as permission to save.

Memory has its own structured schema; the current explicit format mode also
applies to it. Response generation/review keep their original schemas; summary
keeps its existing protocol. No probing, retries, additional review or budget
increase: memory remains at most four calls/session, 20 seconds and 1200 output
tokens per call. Commit remains after the approved reply.

A completed reply with failed extraction or commit is completed_with_warnings,
not terminal_failure. response_status, memory_status and evaluation_status are
separate. Failure codes and history remain visible; a failed assertion does not
become a pass because conversation continued. Final evaluation summarizes actual
completed user turns, assertions and usage. Persistent record format and scoped
identity remain unchanged. The new ZIP contains no historical rollback archives.
