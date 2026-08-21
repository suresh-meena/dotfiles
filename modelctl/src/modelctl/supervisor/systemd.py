from __future__ import annotations



def unit_name(target_id: str, digest: str) -> str:
    from ..domain import sanitize_unit_name

    return sanitize_unit_name(target_id, digest)


def render_unit(*, deployment_id: str, exec_start: str, env_file: str | None = None, working_dir: str | None = None) -> str:
    lines = ["[Unit]", f"Description=modelctl deployment {deployment_id}", "After=network.target", "", "[Service]", "Type=simple"]
    if working_dir:
        lines.append(f"WorkingDirectory={working_dir}")
    if env_file:
        lines.append(f"EnvironmentFile={env_file}")
    lines.append(f"ExecStart={exec_start}")
    lines.append("Restart=no")
    lines.append("KillMode=control-group")
    lines.append("TimeoutStopSec=30s")
    # private state per spec §13
    lines.append("NoNewPrivileges=yes")
    lines.append("")
    lines.append("[Install]")
    lines.append("WantedBy=default.target")
    return "\n".join(lines) + "\n"


def exec_start_for_vllm(*, activate: str | None, config_yaml_path: str, extra_env: dict[str, str] | None = None) -> str:
    # Build ExecStart without shell interpolation; systemd ExecStart is argv vector, but we render as single line with proper escaping.
    # Prefer invoking via bash -lc only when activate script is needed, with single-quoted path to avoid injection.
    # Use explicit python invocation after activation.
    if activate:
        # Use: /bin/bash -lc 'source <activate> && exec vllm serve --config <path>'
        # Single-quote the paths to avoid injection (activate and config path are validated to be absolute and not contain single-quote)
        safe_activate = activate.replace("'", "'\\''")
        safe_cfg = config_yaml_path.replace("'", "'\\''")
        return f"/bin/bash -lc 'source \"{safe_activate}\" && exec vllm serve --config \"{safe_cfg}\"'"
    else:
        safe_cfg = config_yaml_path.replace("'", "'\\''")
        return f"vllm serve --config '{safe_cfg}'"


def remote_paths(machine_state_root: str = "~/.local/state/modelctl") -> dict[str, str]:
    return {
        "state_root": machine_state_root,
        "deployments": f"{machine_state_root}/deployments",
        "generated": f"{machine_state_root}/generated",
        "locks": f"{machine_state_root}/locks",
    }
