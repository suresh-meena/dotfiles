# Suresh's dotfiles

This is the single Git repository for Suresh's dotfiles, projects, and skills.
Every project lives in its own top-level directory; the repository itself uses
one branch, `main`.

## Layout

- `codex/` contains Codex skills maintained in this repository.
- `fleetmon/` contains the fleet monitoring project.
- `how-to-ml-paper/` contains the machine-learning paper workflow skill.
- `modelctl/` contains the model control-plane project and its skill.
- `shared/fleet-dotfiles/` is the shared fleet tooling, tracked as a Git
  submodule.
- `personal/home/` contains this machine's ordinary personal dotfiles. It is
  deliberately ignored so credentials, paths, history, and local overrides do
  not get published.

Do not initialize nested repositories under these project directories. Changes
to any project are committed to this root repository and pushed to its `origin`
remote.

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
