# Contributing

Read the current README, architecture, memory controls and known limitations first. Keep common core/R-A/safety and authorization explicit. Treat topics as topics, not permission to assess the user. Discuss changes to product scope separately from implementation fixes.

Run `python3 scripts/test_offline.py`. Tests must use invented scenarios and intercepted transport, no credentials, account probes or paid inference. Add observable storage/history/delivery checks, not required chat phrases or mock-based claims about model intelligence. Do not upload personal conversations, databases, hidden reasoning, approval handoffs or unreviewed artifacts/history. Report vulnerabilities without attaching secrets or user data.

Ordinary contributions use focused commits/PRs; respect branch protections and existing contributions. Review both tree and artifacts before release; .gitignore alone cannot remove committed secrets. Public source/wheel/Skill are built from the same reviewed source set with checksums. Keep prerelease labeling until justified by evidence.

After reviewing every manifest entry and running tests, build with `python3 scripts/build_release.py`. It verifies file hashes and stages only manifest-listed files; pip runs with --no-index/--no-deps/--no-build-isolation. Install setuptools and wheel in your build environment beforehand. Existing artifacts are not overwritten.
