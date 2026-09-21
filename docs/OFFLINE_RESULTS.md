# Actual offline verification — dev14

- Internal full regression: **673 passed**, 0 failures/errors, 11.996 s. Original **31 test files / 648 tests byte-identical**, plus 25 new F1–F5 tests. No old test expectations weakened.
- Reviewed public distribution: **346 passed**, 0 failures/errors, 3.231 s. This is a subset, not another 346 unique tests. It includes the same 25 new tests and 12 existing official transport tests with local standalone helpers. One private historical-plan hash test remains internal; its business expectation is unchanged there.
- Tests run in a fresh credential-free environment with socket connect/connect_ex/create_connection blocked. All provider responses and tokens are fixtures, not real usage.
- Private baseline replay confirmed F1 and F3 actually delivered conflicting fixtures (2 and 1 fake generations, no check). Fixed replay used 3 and 2 total fake requests respectively, withheld both conflicting drafts, and preserved only previously completed history. Check verdicts were supplied fixtures, not evidence of model detection accuracy.
- F2 withdrawal boundaries, F4 imperative/onset/causal and cross-turn signals, F5 actual local retrieval in compiled sharing context have public synthetic integration coverage. Requested task clarification remains deliverable without extra review; failures and exhausted budget do not commit drafts.
- Engine, storage, memory parsing/selection, checked/companion contracts and transport modules match dev13 bytes. Core, R-A and safety unchanged. CLI adds availability gating for non-public historical diagnostic material; installation references are cleaned for public packaging.
- Initial public packaging verification exposed an omitted synthetic scenario and a helper import; both were corrected, then the public suite above passed. No application test was removed to hide those failures.

**Real inference calls: 0.** No user private chat or user database read. GitHub publication authentication is separate from model credentials. Account connectivity, current alias availability and naturalness remain unverified in this release. Local CI-equivalent success is not a claim that GitHub Actions has run.

See [machine-readable results](offline-results.json) and [public test output](offline-tests.txt). Private audit wording and raw historical model results are deliberately absent. Timing measures local tests only.
