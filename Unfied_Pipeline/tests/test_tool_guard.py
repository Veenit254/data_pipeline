import json

import tool_guard as g

ROOT = str(g.ROOT)


def run(cmd):
    return g.check("run_commands", {"commands": [cmd]})


def test_agent_commands_are_allowed():
    assert run(f"cd {ROOT} && .venv/bin/python scripts/collect.py --dry-run") is None
    assert run(".venv/bin/python scripts/job.py wait 3") is None
    assert run(".venv/bin/python scripts/audit_dataset.py 2>&1 | tail -40") is None
    assert run('.venv/bin/python scripts/common.py log "cycle 2: done; records 590 (+3)"') is None
    assert run(".venv/bin/python scripts/status_report.py | grep -i engines") is None
    assert run("git status --short") is None


def test_everything_else_needs_approval():
    for cmd in ["curl http://x", "rm -rf data", '.venv/bin/python -c "print(1)"', "ls -la data",
                ".venv/bin/python scripts/collect.py --max-queries 5",
                ".venv/bin/python scripts/status_report.py\nrm -rf data",
                ".venv/bin/python scripts/status_report.py > out.txt",
                ".venv/bin/python scripts/status_report.py $(rm x)",
                ".venv/bin/python scripts/status_report.py | grep x /etc/passwd",
                ".venv/bin/python scripts/parse_nse_records.py --force",
                ".venv/bin/python scripts/report_issue.py --close 3",
                ".venv/bin/python scripts/job.py start --max-queries 8 &", "cd /tmp && git status"]:
        assert run(cmd), cmd


def test_reads_and_edits():
    assert g.check("read_files", {"files": [{"path": f"{ROOT}/config/query_plan.json"}]}) is None
    for p in ["scripts/collect.py", "/etc/passwd", "data/raw/master_lists/x.pdf", "~/.cline/data/x"]:
        assert g.check("read_files", {"files": [{"path": p}]}), p
    assert g.check("editor", {"path": f"{ROOT}/config/query_plan.json", "new_text": "x"})  # no direct edits at all
    assert g.check("editor", {"path": "config/../scripts/x.py", "new_text": "x"})
    assert g.check("apply_patch", {"input": "*** Begin Patch\n*** Update File: soul.md\n*** End Patch"})
    assert g.check("fetch_web_content", {"url": "https://x"})
    assert g.check("start_background_command", {"command": "sleep 1"})


def test_blocked_call_is_rewritten_not_cancelled(monkeypatch, capsys):
    monkeypatch.setattr(g, "record", lambda *a: None)
    monkeypatch.setattr(g, "audit", lambda *a: None)
    payload = {"tool_call": {"name": "run_commands", "input": {"commands": ["curl http://evil"]}}}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    g.main()
    out = json.loads(capsys.readouterr().out)
    assert "cancel" not in out  # cancel would end the agent's whole run
    assert out["overrideInput"]["commands"][0].startswith("printf ")
    assert "curl" in out["context"] or "not allowed" in out["context"]


def test_secrets_and_search_settings_are_off_limits():
    for p in [".env", f"{ROOT}/.env.local", "~/.config/nse-unified-pipeline/keys.env"]:
        assert g.check("read_files", {"files": [{"path": p}]}), p
    assert g.check("read_files", {"files": [{"path": ".env.example"}]}) is None
    plan = f"{ROOT}/config/query_plan.json"
    assert g.check("editor", {"path": plan, "old_text": '"daily_limit": 50', "new_text": '"daily_limit": 5000'})
    assert g.check("editor", {"path": plan, "new_text": '"providers": ["serper"]'})
    assert g.check("apply_patch", {"input": '*** Begin Patch\n*** Update File: config/query_plan.json\n'
                                            '+ "monthly_limit": null\n*** End Patch'})
    # policy: queries are added only with scripts/add_queries.py, never by editing the plan
    assert g.check("editor", {"path": plan, "old_text": '"queries": [', "new_text": '"queries": ["nse delisting 2019",'})
    assert g.check("run_commands", {"commands": [".venv/bin/python scripts/web_search.py --check"]}) is None
    assert g.check("run_commands", {"commands": [".venv/bin/python scripts/web_search.py --query x"]})


def test_docket_is_read_only_for_the_agent():
    assert g.check("run_commands", {"commands": [".venv/bin/python scripts/docket.py --stats"]}) is None
    assert g.check("run_commands", {"commands": [".venv/bin/python scripts/docket.py --list failed --limit 5"]}) is None
    for cmd in ["--retry dead", "--unblock nseindia.com", "--promote"]:
        assert g.check("run_commands", {"commands": [f".venv/bin/python scripts/docket.py {cmd}"]}), cmd
    assert g.check("run_commands", {"commands": [".venv/bin/python scripts/fetch_worker.py"]})


def test_blocked_config_script_points_to_add_queries():
    inp = {"commands": [".venv/bin/python - <<'PY'\nimport json; open('config/query_plan.json')\nPY"]}
    reason = g.check("run_commands", inp)
    assert reason and "scripts/add_queries.py" in g.blocked_input("run_commands", inp, reason)["commands"][0]
