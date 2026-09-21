#!/bin/sh
cd "$(dirname "$0")" || exit 1
printf '%s\n' 'Legacy Nebius entry (paid after a message). For DeepSeek see README.'
exec python3 -m steady_companion chat --profile competition-nebius --max-calls 4
