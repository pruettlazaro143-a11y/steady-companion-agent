# P1.2.3 candidate evidence isolation

Host debugging only; retain the [format recovery and privacy contract](p122-formats.md).
The host first validates the original 2–3 candidate group and its unique IDs.
An evidence_reference or evidence_quote defect rejects only the identified
candidate. Exact user-source and substring checks remain unchanged. Other
structure and visible-text errors keep their whole-group policy.

A filtered set may contain one candidate; it still requires normal review.
Review sees only retained candidates, and approvals/issues must refer to those
IDs. The original selected ID may be unavailable after filtering; selection
then uses only retained, approved candidates. A never-present selected ID or
ambiguous duplicate ID is a whole-group protocol fault. No evidence is rewritten,
no text is copied to manufacture another candidate, and no request repairs a quote.

All candidates rejected means no_eligible_candidates and no review or semantic
retry. Partial rejection does not reset the shared one-recovery/four-request
response budget or repeat memory extraction. Memory commits only after approval.

Candidate validation metadata records input count, eligible IDs, rejection codes,
IDs actually sent to review, and final selection. completed_with_rejections is
not a terminal failure. Previous recovery history and subsequent storage failure
remain separately visible. Evidence validity is only an anchor check, never a
substitute for content review, factual verification or naturalness evaluation.
