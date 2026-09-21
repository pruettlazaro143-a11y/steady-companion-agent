# P1.2.1 diagnostics contract

The host preserves stage metadata on success and failure: memory, generation,
review, repair_generation, repair_review, summary and commit. Stable error codes
replace arbitrary exception text. A completed transport response is not a validated
generation; a reviewed answer is not proof of a durable memory commit.

Ordinary chat does not write full transcripts or candidate bodies. Only explicitly
authorized synthetic capture-stages may retain bounded visible fields. Never log
provider reasoning/reasoning_content, analysis-markup content, headers or keys.
Malformed JSON has no safely known visible-text boundary: record size, finish
reason, JSON position and code only. Unknown fields are counted, not copied.

Failed evaluation writes available stage/usage metadata and an actual metadata-only
snapshot of the temporary store and volatile candidates before cleanup, then prints
step, stage, code, budget usage and log path. Failed requests stay counted; do not
make diagnostic paid retries or silently enlarge the budget. No unchecked draft is
a visible final answer. Keep the original generation/review and repair budget.

The isolated two-turn name retest is capped at 8 total calls. It differs from the
original failure context. The ordered pilot diagnostic retains the original input
sequence and 32-call cap. Both require explicit cost confirmation. New host-only
assertions verify the exact saved/retrieved record without giving expected answers
to the model. Old logs with zero assertions remain historical evidence, not passes.
