# Running this preview

Use the single DeepSeek quick-start path in [README](README.md) / [中文](README.zh-CN.md). No batch evaluation is necessary to start.

`configs/deepseek-official.json` is non-secret. Provider/endpoint/model, thinking policy and auxiliary format are host configuration, not persona instructions. Unsupported official schema mode fails before credentials or requests. Independent credentials never fall back to one another. Generic ten-field connections remain supported through `steady_companion/data/deepseek-connection.example.json`; do not assume an arbitrary DeepSeek reseller is compatible.

Legacy Nebius (paid only after a user message):

```sh
python3 -m steady_companion chat --profile competition-nebius --max-calls 4
```

No-profile `chat` and `Start.command` retain Nebius checked mode. They normally need generation plus review, with the existing bounded shared repair opportunity. The current daily profile uses companion-v3 without output recovery. Do not describe their difference as only the model name.

The default data directory is `~/.steady-companion`; `--data-dir /path/to/local/data` is a **global option before chat/inspect**. Use separate directories for separate people. No migration is required by dev14. Existing SQLite schema 2 and user choices remain intact. Going back to dev13 restores old interaction behavior; session scope does not persist across process restart. Back up data locally before manual version changes; backups must not be published.

`/usage` shows calls and reported tokens; absent provider usage is unknown, never zero-cost evidence. Ctrl-C cancels without automatic retry. A failed required check withholds the reply and history commit. Diagnoses of provider errors should use safe error codes, not repeated paid runs. Do not post private chat by default.

Historical private approval/replay matrices and their entry points are not in this public distribution. Earlier internal artifacts are retained locally; no historical evaluation command is a prerequisite for use.

## dev16 optional continuity trial

For the single-launch six-turn / twelve-request synthetic experience, see [the current protocol and command](docs/CONTINUOUS_EXPERIENCE.md). It is an opt-in evaluation with local visible-text capture, not the normal chat default. No existing user database is used.

Dev16 replaces the default synthetic trial material with a new vegetable stop-motion scenario (continuous-experience-v2); dev15 and its original material remain in the dev15 tag. No real-run authorization is inferred from an earlier trial. The runtime instruction/examples change is separate from model access; endpoint/model/thinking/token/timeout settings are unchanged.
