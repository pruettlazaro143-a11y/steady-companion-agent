# P1.2.2 host format contract

For host debugging, retain the [diagnostic privacy contract](p121-diagnostics.md).
The checked host supplies separate generation/review response formats. Configuration
selects json_schema (default), json_object, or off; auxiliary memory/summary and
direct chat receive neither checked schema. Schema fields do not prescribe a
conversational style. Current model/endpoint compatibility remains unverified.

An initial candidate JSON syntax/shape failure may use one fresh generation plus
mandatory review. Format recovery shares the existing single content-repair
opportunity and four-response-request cap. Both remaining calls must fit the
session budget. Failed content is never reused in the repair prompt. Transport,
HTTP, timeout, cancellation, evidence mismatch and stale context are not format
retry triggers. Memory extraction is not repeated; commit follows approved output.

Diagnostics keep the initial failure in failure_history. Only a successful review
marks it recovered and clears it from the terminal failure field. A subsequent
commit failure remains a separate failure. Raw malformed JSON is not captured;
parser categories and positions describe normalized content, not the original wire
string. Real verification requires a new explicitly authorized bounded run; the
host never probes capabilities or changes format mode and resends automatically.
