#!/usr/bin/env sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
skill_dir="$repo_dir/skills/how-to-ml-paper"

codex_home=${1:-${CODEX_HOME:-"$HOME/.codex"}}
opencode_home=${2:-${XDG_CONFIG_HOME:-"$HOME/.config"}/opencode}

mkdir -p "$codex_home/skills" "$opencode_home/skills"
ln -sfn "$skill_dir" "$codex_home/skills/how-to-ml-paper"
ln -sfn "$skill_dir" "$opencode_home/skills/how-to-ml-paper"

printf '%s\n' \
  "Installed how-to-ml-paper for Codex: $codex_home/skills/how-to-ml-paper" \
  "Installed how-to-ml-paper for OpenCode: $opencode_home/skills/how-to-ml-paper"
