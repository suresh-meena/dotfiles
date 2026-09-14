#!/usr/bin/env sh
set -eu

repo_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
skill_dir="$repo_dir/skills/slurm-helper"

codex_home=${1:-${CODEX_HOME:-"$HOME/.codex"}}
opencode_home=${2:-${XDG_CONFIG_HOME:-"$HOME/.config"}/opencode}

mkdir -p "$codex_home/skills" "$opencode_home/skills"
ln -sfn "$skill_dir" "$codex_home/skills/slurm-helper"
ln -sfn "$skill_dir" "$opencode_home/skills/slurm-helper"

printf '%s\n' \
  "Installed slurm-helper for Codex: $codex_home/skills/slurm-helper" \
  "Installed slurm-helper for OpenCode: $opencode_home/skills/slurm-helper"

# ZCode keeps its own skills tree when present.
zcode_skills="$HOME/.zcode/skills"
if [ -d "$HOME/.zcode" ]; then
    mkdir -p "$zcode_skills"
    ln -sfn "$skill_dir" "$zcode_skills/slurm-helper"
    printf '%s\n' "Installed slurm-helper for ZCode: $zcode_skills/slurm-helper"
fi
