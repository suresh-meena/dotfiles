from __future__ import annotations

import json
from pathlib import Path

from research_kb.clients.cli import main as cli_main
from research_kb.transports.mcp_server import McpServer


def run_cli(capsys, *args):
    code = cli_main(["--json", "--actor", "admin", *args])
    captured = capsys.readouterr()
    payload = json.loads(captured.out) if captured.out.strip() else None
    return code, payload


def test_cli_init_and_capabilities(tmp_path: Path, capsys):
    root = tmp_path / "cli-project"
    root.mkdir()
    code, payload = run_cli(capsys, "--root", str(root), "init", "CLI Project")
    assert code == 0
    assert payload["status"] == "initialized"
    code, payload = run_cli(capsys, "--root", str(root), "capabilities")
    assert code == 0
    assert payload["project_id"]
    assert payload["api_version"]


def test_cli_propose_and_search(project_root: Path, capsys):
    operations = {
        "operations": [
            {
                "op": "capture",
                "payload": {
                    "kind": "knowledge",
                    "subkind": "idea",
                    "title": "CLI idea",
                    "state_json": {"subkind": "idea", "proposal": "Test the CLI end to end."},
                },
            }
        ]
    }
    ops_file = project_root / "ops.json"
    ops_file.write_text(json.dumps(operations), encoding="utf-8")
    code, payload = run_cli(
        capsys,
        "--root",
        str(project_root),
        "propose",
        "--file",
        str(ops_file),
        "--auto-apply",
    )
    assert code == 0
    assert payload["status"] == "applied"
    code, payload = run_cli(capsys, "--root", str(project_root), "search", "CLI")
    assert code == 0
    assert payload["result"]["candidates"]


def test_cli_error_exit_codes(project_root: Path, capsys):
    code, payload = run_cli(capsys, "--root", str(project_root), "get", "does-not-exist")
    assert code == 3
    assert payload["error"]["code"] in ("NOT_FOUND", "REFERENCE_UNRESOLVED")


def test_cli_export_and_verify(project_root: Path, capsys, tmp_path: Path):
    output = tmp_path / "status.md"
    code, payload = run_cli(
        capsys,
        "--root",
        str(project_root),
        "export",
        "--format",
        "markdown",
        "--output",
        str(output),
    )
    assert code == 0
    assert output.exists()
    code, payload = run_cli(capsys, "--root", str(project_root), "verify", "--checks", "integrity,index")
    assert code == 0
    assert payload["result"]["summary"]["errors"] == 0


def test_mcp_initialize_list_and_call(project_root: Path):
    server = McpServer(root=str(project_root))
    init = server.handle({"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {}})
    assert init["result"]["protocolVersion"]
    listing = server.handle({"jsonrpc": "2.0", "id": 2, "method": "tools/list"})
    tool_names = {tool["name"] for tool in listing["result"]["tools"]}
    assert "rkb_context" in tool_names
    assert "rkb_propose" in tool_names
    called = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 3,
            "method": "tools/call",
            "params": {"name": "rkb_capabilities", "arguments": {}},
        }
    )
    assert called["result"]["isError"] is False
    assert called["result"]["structuredContent"]["project_id"] == server._open().ctx.project_id


def test_mcp_error_is_structured(project_root: Path):
    server = McpServer(root=str(project_root))
    called = server.handle(
        {
            "jsonrpc": "2.0",
            "id": 4,
            "method": "tools/call",
            "params": {"name": "rkb_get", "arguments": {"refs": ["missing-object"]}},
        }
    )
    assert called["result"]["isError"] is True
    assert called["result"]["structuredContent"]["error"]["code"] in (
        "NOT_FOUND",
        "REFERENCE_UNRESOLVED",
    )
