# Steady Companion · 稳伴

[简体中文](README.zh-CN.md)

An open-source AI companion framework with user-controlled memory and experimental interaction boundaries. Runnable agent + reusable skill.

**Experimental preview — Agent 0.6.0.dev15 / Skill 0.8.0-dev15.** The aim is sustained companionship with a consistent stance and user-controlled continuity. Natural conversation, psychological support and practical benefit remain unvalidated. This is not a clinician, treatment service or emergency monitor.

## Quick start

Python **3.10+**; no third-party runtime dependencies. From this source directory:

```sh
python3 -m venv .venv
. .venv/bin/activate
python3 -m pip install .
python3 -m steady_companion inspect --profile daily-deepseek --connection ./configs/deepseek-official.json
python3 -m steady_companion demo
```

`inspect` makes no network requests or credential prompts; `demo` uses temporary synthetic data and no model. Test outputs are not actual model replies. Wheel installation is also supported with `python3 -m pip install /path/to/steady_companion_agent-0.6.0.dev15-py3-none-any.whl`.

Only when you choose to start **paid** chat:

```sh
python3 -m steady_companion chat --profile daily-deepseek --connection ./configs/deepseek-official.json --max-calls 4
```

Enter your key in the hidden terminal prompt (process-only), or supply `STEADY_DEEPSEEK_API_KEY` through your own environment. Never put it in source, config, logs or an issue. A message triggers the request, not startup. Four requests is the **whole session budget**, not four guaranteed turns. User-enabled auxiliary operations count too. No automatic retry, model switch or background probe.

The explicit official profile uses `https://api.deepseek.com`, `deepseek-flash`, thinking disabled, 4096 output-token and 90-second host limits. Main replies are text; structured auxiliaries use JSON object plus host validation. No `store`, schema, temperature or top-p is sent. The service alias does not pin model weights. Request omission is not a retention guarantee. This release's offline verification does not establish account connectivity or response quality.

`competition-nebius` and chat without a profile retain the legacy Nebius/NVIDIA checked path, using separate `NEBIUS_API_KEY`. `Start.command` explicitly remains Nebius. They are different runtime policies, not a controlled model comparison. Public release does not establish competition eligibility. See [run/configuration](RUN.md).

## Agent, Skill and experimental controls

The Agent implements storage, revision checks, budgets, context and delivery. The [Skill](steady_companion/skill/SKILL.md) is reusable guidance; installing it alone provides none of those execution guarantees. Unzip the Skill artifact into your host's supported skill directory.

**Interaction control and narrow checks are OFF by default.** To explicitly test them, add `--interaction-control session --interaction-check selective` to the paid chat command. A triggered check consumes at most one additional request. A required failed/unknown check withholds the draft. This bounded Chinese-language recognizer can miss intent; it is not a semantic safety guarantee. `all` checks every candidate at additional cost; it is not the default. Scope state is volatile, not a psychological profile.

## Memory and privacy

New users start in `manual`. `/remember preference ...` saves an explicit record; `/memory`, `/correct ID ...`, `/forget ID` inspect, correct and delete. `/memory-mode auto` permits supported ordinary memories; `/auto-memory`, `/auto-correct A1 ...`, `/auto-forget A1` manage them. `/memory-mode session` avoids new durable memory. `/wipe` requires confirmation. `/new` clears conversation but retains saved records. Existing mode and optional assistance settings survive profile changes.

Ordinary auto authorization excludes sensitive histories; extraction is conservative, incomplete and may skip. Deletions/corrections invalidate dependent context. Failed persistence may remain effective only within the session and is reported as unsaved. See [memory controls](docs/MEMORY.md). The selected service receives current context and relevant authorized records. Normal chat does not add full conversation or candidate logs. SQLite data is local and is **not encrypted by this application**; keep it out of Git.

## Development and evidence

```sh
python3 scripts/test_offline.py
```

This clears inherited credentials in the test subprocess and blocks socket connections. CI only runs this offline suite, without secrets, account probes or inference. Use newly written synthetic cases in contributions. [Architecture](docs/ARCHITECTURE.md), [fixes/limits](docs/FIXES.md), [verification](docs/OFFLINE_RESULTS.md), [public file scope](docs/PUBLICATION.md), [contributing](CONTRIBUTING.md), [changelog](CHANGELOG.md).

No real inference was run for this release. Passing tests validates the program paths covered, not naturalness, universal pause detection, stable long-term companionship or psychological outcomes.

MIT covers project code/material; linked external research remains under its own terms. [License](LICENSE).

## Optional continuous experience (dev15)

`eval-experience` previews six newly authored synthetic turns offline. The explicit live opt-in uses the existing official daily profile, session control and selective checks, a new temporary manual-memory store, and a fixed 12-request total cap. It asks whether the next scripted turn fits the actual previous reply; failure or inapplicability stops the run. Local visible reports, including withheld drafts, are never uploaded automatically. [Command, protocol and unknown cost items](docs/CONTINUOUS_EXPERIENCE.md). Program tests do not establish companionship quality.
