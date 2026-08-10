# Suresh's dotfiles

This repository keeps the personal machine layer separate from the shared
fleet tooling.

## Layout

- `shared/fleet-dotfiles/` is the upstream `b-vitamins/dotfiles` repository,
  tracked as a Git submodule. It contains the shared `fleetctl` tooling,
  templates, documentation, and Codex skills.
- `personal/home/` contains this machine's ordinary personal dotfiles. It is
  deliberately ignored so credentials, paths, history, and local overrides do
  not get published.

Clone the shared component with:

```bash
git clone --recurse-submodules https://github.com/suresh-meena/dotfiles.git
```

For an existing checkout:

```bash
git submodule update --init --recursive
```

The `remote-fleet-operator` skill (the fleetctl skill) is installed globally
for Codex under `~/.codex/skills/` and for OpenCode under
`~/.config/opencode/skills/`. Both locations share the same installed skill.
