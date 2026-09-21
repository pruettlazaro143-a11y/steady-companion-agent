# Output contract for implementers

Treat conversational appropriateness as a property of an exchange, not a list of
nice phrases. Keep the core Skill and safety instructions present in each model
request. Do not hide safety or refusal rules in optional references.

## Choose and check a reply

1. Read the present message and relevant visible context. Separate stated facts,
   explicit wishes, approved memory and uncertain interpretations. Do not infer
   diagnoses or stable personality from this step.
2. Prepare a small number of distinct candidate replies from the companion's
   stable manner and shared context, without requiring action labels, length
   categories or question quotas. Associate each with an exact visible user
   excerpt; never treat a matching excerpt as proof that a deduction is correct.
3. Validate structure and host restrictions in program code. Check empty output,
   unsupported actions, emoji controls and provenance. Mark possible repetitive
   invitations, disproportionate length and unnecessary questions for review.
4. Review the candidates against the original conversation. Prefer the candidate
   that addresses the actual contribution while respecting current wishes.
   Reject unnecessary questions, mechanical repetition, made-up context,
   mind-reading, missed current risk and unsupported promises.
5. If no candidate is suitable, revise within an explicit request budget and
   review again. Do not silently show an unreviewed draft when the budget ends.
   Report an incomplete request plainly rather than inventing a reassuring reply.
6. Retain only the displayed answer in recent conversation. Do not automatically
   persist alternative drafts, review hints, sensitive hypotheses or reasoning.

A second call to the same model is a separate review, not independent clinical
supervision. Both calls can make correlated errors. Style heuristics and exact
quotes do not establish psychological safety or effectiveness.

## Contextual contrasts

- After "有点失落" followed by "今天汇报没做好", take the new event into account;
  routinely asking "你当时是什么感受" duplicates what was already offered.
- After "还行吧，今天", avoid treating the exchange as complete or manufacturing
  cheerfulness. There may simply be nothing more to say yet.
- After "能具体帮我分析一下哪里可以改吗", provide useful detail; a one-line
  sympathy response can be inadequate even if it sounds natural.
- After "别分析了，聊点别的", stop analysis. Do not carry it into the new topic
  through hidden probing or a prediction about why the user refused.

These are decision contrasts, not fixed scripts. Do not turn any sample sentence
into the default reply for all users or situations.

## Memory and budget

Keep stable approved facts and communication preferences available with source,
scope and correction/deletion controls. Keep tentative interpretations revisable
and separate from facts. A prior preference does not override today's explicit
request. Reopen assumptions when corrected; do not require a complete psychological
profile before ordinary respectful conversation.

Do not assume this Markdown creates storage or review infrastructure. The bundled
terminal agent v0.3.0 implements checked generation/review with 2–4 requests per
turn, bounded volatile context, manual memories and approved shared notes.
An explicit /learn call can propose up to three notes from recent conversation;
only /keep saves a selected note. This is context learning, not a weight update.
It does not implement background follow-ups or a validated adaptive review
schedule. Report actual provider usage and failures.

Compare a shared-safety baseline, core-skill generation and checked generation
on the same synthetic exchanges. Include paired cases needing different lengths,
refusal/correction, direct practical help and urgent safety. Report extra requests,
tokens and delay along with user judgments. Evaluate naturalness and support
through real use with consent; offline contracts alone cannot establish either.
