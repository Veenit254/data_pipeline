"""Regression tests for the agent permission boundary.

Design under test: the tool guard accepts only an exact grammar (programs, flags, values), refuses anything
bash would expand or redirect, and rewrites every allowed call into `scripts/agent_exec.py <token>`, which
re-validates the pipeline and runs it without a shell (pipe filters are implemented in Python, stdin only)."""
import base64
import json
import subprocess
import sys

import pytest

import tool_guard as g

ROOT = str(g.ROOT)
PY = ".venv/bin/python"


def cmd(c):
    return g.check("run_commands", {"commands": [c]})


# ---------- S1: pipe filters may not touch files ---------------------------------------------------------
@pytest.mark.parametrize("c", [
    f"{PY} scripts/common.py log x | cut -c1-200 .env",
    f"{PY} scripts/common.py log x | cut -c1-200 /home/user/.config/nse-unified-pipeline/keys.env",
    f"{PY} scripts/common.py log x | grep -eKEY .env",
    f"{PY} scripts/common.py log x | grep -r API_KEY",
    f"{PY} scripts/common.py log x | grep -R KEY",
    f"{PY} scripts/common.py log x | grep -f .env",
    f"{PY} scripts/common.py log x | grep --file=.env",
    f"{PY} scripts/common.py log x | sort -oscripts/collect.py",
    f"{PY} scripts/common.py log x | sort -o scripts/collect.py",
    f"{PY} scripts/common.py log x | sort --output=x",
    f"{PY} scripts/common.py log x | sort --files0-from=.env",
    f"{PY} scripts/common.py log x | sort .env",
    f"{PY} scripts/common.py log x | head .env",
    f"{PY} scripts/common.py log x | tail -n 5 .env",
    f"{PY} scripts/common.py log x | wc -l .env",
    f"{PY} scripts/common.py log x | uniq - out.txt",
    f"{PY} scripts/common.py log x | grep KEY .env",
    f"{PY} scripts/common.py log x | grep -e KEY -e TOKEN .env",
])
def test_pipe_filters_cannot_read_or_write_files(c):
    assert cmd(c), c


# ---------- S2: nothing bash would expand -------------------------------------------------------------------
@pytest.mark.parametrize("c", [
    f"{PY} scripts/common.py log $HOME",
    f'{PY} scripts/common.py log "$HOME"',
    f"{PY} scripts/common.py log ~/.config/nse-unified-pipeline/keys.env",
    f"{PY} scripts/inspect_artifact.py data/raw/*",
    f"{PY} scripts/inspect_artifact.py data/raw/x?.pdf",
    f"{PY} scripts/inspect_artifact.py data/raw/{{a,b}}.pdf",
    f"{PY} scripts/inspect_artifact.py data/raw/[ab].pdf",
    f"{PY} scripts/common.py log a\\$b",
    f'{PY} scripts/common.py log "a`id`"',
    f"{PY} scripts/common.py log !!",
    f'{PY} scripts/common.py log "x\nrm -rf data"',
])
def test_shell_expansion_is_refused(c):
    assert cmd(c), c


# ---------- S3: agent scripts only with the agent's own arguments ------------------------------------------
@pytest.mark.parametrize("c", [
    f"{PY} scripts/docket.py --stats --unblock nseindia.com",
    f"{PY} scripts/docket.py --list dead --retry dead",
    f"{PY} scripts/docket.py --stats --promote --limit 100000",
    f"{PY} scripts/docket.py --list failed --limit 100000",
    f"{PY} scripts/report_issue.py --close=1",
    f"{PY} scripts/report_issue.py --clos 1",
    f"{PY} scripts/report_issue.py --list --close 1",
    f"{PY} scripts/job.py start --unlimited",
    f"{PY} scripts/job.py start --max-files all --max-docs-per-company all",
    f"{PY} scripts/job.py start --max-files 0",
    f"{PY} scripts/job.py start --max-companies 99999",
    f"{PY} scripts/job.py start --max-queries 8 --unl",
    f"{PY} scripts/job.py start --stage fetch --fetch-limit 999",
    f"{PY} scripts/job.py wait x",
    f"{PY} scripts/collect.py --dry-run --unlimited --stage all --max-files all extra",
    f"{PY} scripts/web_search.py --check --query x",
    f"{PY} scripts/status_report.py --watch 1",
    f"{PY} scripts/common.py log a b",
    f"{PY} scripts/common.py notify x",
    f"{PY} scripts/add_queries.py --year-tag 2008 --plan /tmp/x.json",
    f"{PY} scripts/inspect_artifact.py ../../etc/passwd",
    f"{PY} scripts/agent_exec.py e30",
    f"{PY} scripts/../scripts/report_issue.py --close 1",
])
def test_script_argument_grammar(c):
    assert cmd(c), c


def test_job_caps_may_only_be_tightened(monkeypatch):
    monkeypatch.setattr(g, "user_caps", lambda: {"mode": "capped", "max_files": 60, "max_companies": 25,
                                                  "max_docs_per_company": 5})
    assert cmd(f"{PY} scripts/job.py start --harvest-only --max-files 40") is None
    assert cmd(f"{PY} scripts/job.py start --harvest-only --max-files 60") is None
    assert cmd(f"{PY} scripts/job.py start --harvest-only --max-files 61")
    monkeypatch.setattr(g, "user_caps", lambda: {"mode": "unlimited", "max_files": 60})
    assert cmd(f"{PY} scripts/job.py start --harvest-only --max-files 500") is None  # user chose unlimited


# ---------- S4: no direct edits at all; the plan changes only through add_queries.py -----------------------
@pytest.mark.parametrize("old,new", [
    ('"min_free_gb": 20', '"min_free_gb": 0'), ('"delay_s": [10, 20]', '"delay_s": [0, 0]'),
    ('"max_per_run": 12', '"max_per_run": 5000'), ('"deny_domains": [', '"deny_domains_x": ['),
    ('"block_domain_after_failures": 3', '"block_domain_after_failures": 9999'), ('"open_web": false', '"open_web": true'),
    ('"queries": [', '"queries": ["nse delisting 2019",'),
])
def test_agent_cannot_edit_config(old, new):
    assert g.check("editor", {"path": f"{ROOT}/config/query_plan.json", "old_text": old, "new_text": new}), new


def test_agent_cannot_patch_or_delete_files():
    for verb in ("Delete File", "Update File", "Add File"):
        assert g.check("apply_patch", {"input": f"*** Begin Patch\n*** {verb}: config/query_plan.json\n*** End Patch"})


# ---------- what the agent legitimately does keeps working ------------------------------------------------
@pytest.mark.parametrize("c", [
    f"cd {ROOT} && {PY} scripts/collect.py --dry-run",
    f"{PY} scripts/collect.py --dry-run --stage discover --max-queries 999",
    f"{PY} scripts/job.py start --max-queries 8",
    f"{PY} scripts/job.py wait 3",
    f"{PY} scripts/job.py status",
    f"{PY} scripts/audit_dataset.py 2>&1 | tail -40",
    f'{PY} scripts/common.py log "cycle 2: done; records 590 (+3)"',
    f"{PY} scripts/status_report.py --json",
    f"{PY} scripts/status_report.py | grep -i engines",
    f'{PY} scripts/status_report.py | grep -E "zero.*rows|audit" | head -5',
    f"{PY} scripts/report_issue.py --list",
    f'{PY} scripts/report_issue.py --kind zero_rows --summary "3 orders gave 0 rows" --evidence data/raw/a/x.pdf '
    f'--evidence data/raw/a/y.pdf',
    f"{PY} scripts/inspect_artifact.py --zero-rows",
    f"{PY} scripts/inspect_artifact.py data/raw/master_lists/18fea6d29ab81379.pdf",
    f"{PY} scripts/docket.py --stats",
    f"{PY} scripts/docket.py --list failed --limit 5",
    f"{PY} scripts/web_search.py --check",
    f'{PY} scripts/add_queries.py --year-tag 2008 --query "SEBI order delisting 2008" --query \'"Acme Ltd" delisted\'',
    f"{PY} scripts/add_queries.py --add-domain capitalmarket.com",
    f"{PY} scripts/build_metadata.py && {PY} scripts/status_report.py | sort | uniq -c | sort -rn | cut -c1-80",
    "git status --short",
])
def test_agent_commands_still_allowed(c, monkeypatch):
    monkeypatch.setattr(g, "user_caps", lambda: {"mode": "capped", "max_files": 60, "max_companies": 25,
                                                  "max_docs_per_company": 5})
    assert cmd(c) is None, (c, cmd(c))


# ---------- allowed calls are rewritten to the shell-free runner ------------------------------------------
def test_allowed_call_is_rewritten_to_agent_exec(monkeypatch, capsys):
    monkeypatch.setattr(g, "audit", lambda *a: None)
    c = f'{PY} scripts/status_report.py | grep -i "engines" | head -3'
    payload = {"tool_call": {"name": "run_commands", "input": {"commands": [c]}}}
    monkeypatch.setattr("sys.stdin", __import__("io").StringIO(json.dumps(payload)))
    g.main()
    out = json.loads(capsys.readouterr().out)
    [new] = out["overrideInput"]["commands"]
    prog, script, token = new.split(" ")
    assert (prog, script) == (".venv/bin/python", "scripts/agent_exec.py")
    assert set(token) <= set("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_=")
    plan = json.loads(base64.urlsafe_b64decode(token))
    assert plan == [{"op": None, "pipeline": [["scripts/status_report.py"], ["grep", "-i", "engines"], ["head", "-3"]]}]


def test_agent_exec_revalidates_a_crafted_token():
    import agent_exec
    evil = base64.urlsafe_b64encode(json.dumps(
        [{"op": None, "pipeline": [["scripts/docket.py", "--unblock", "nseindia.com"]]}]).encode()).decode()
    with pytest.raises(agent_exec.Refused):
        agent_exec.decode(evil)
    evil2 = base64.urlsafe_b64encode(json.dumps([{"op": None, "pipeline": [["/bin/sh", "-c", "id"]]}]).encode()).decode()
    with pytest.raises(agent_exec.Refused):
        agent_exec.decode(evil2)


def test_python_filters_never_open_files():
    import agent_exec as ax
    lines = ["b 2", "a 1", "KEY=1", "a 1", "c 10"]
    assert ax.filter_lines(["grep", "-i", "key"], lines) == (["KEY=1"], 0)
    assert ax.filter_lines(["grep", "-v", "-E", "^a|KEY"], lines) == (["b 2", "c 10"], 0)
    assert ax.filter_lines(["grep", "-c", "a"], lines) == (["2"], 0)
    assert ax.filter_lines(["grep", "zzz"], lines) == ([], 1)
    assert ax.filter_lines(["head", "-2"], lines) == (["b 2", "a 1"], 0)
    assert ax.filter_lines(["tail", "-n", "1"], lines) == (["c 10"], 0)
    assert ax.filter_lines(["sort", "-u"], lines)[0] == ["KEY=1", "a 1", "b 2", "c 10"]
    assert ax.filter_lines(["sort", "-k", "2", "-n", "-r"], lines)[0][0] == "c 10"
    assert ax.filter_lines(["uniq", "-c"], ["a", "a", "b"])[0] == ["      2 a", "      1 b"]
    assert ax.filter_lines(["cut", "-d", " ", "-f", "2"], lines)[0] == ["2", "1", "KEY=1", "1", "10"]  # POSIX: no delimiter -> whole line
    assert ax.filter_lines(["cut", "-c1-3"], ["abcdef"])[0] == ["abc"]
    assert ax.filter_lines(["wc", "-l"], lines)[0] == ["5"]
    assert ax.filter_lines(["grep", "a.1"], lines)[0] == ["a 1", "a 1"]          # BRE '.' still a wildcard
    assert ax.filter_lines(["grep", "a|b"], ["a|b", "a"])[0] == ["a|b"]          # BRE '|' is literal


def test_agent_exec_runs_without_a_shell(tmp_path):
    """End to end: `common.py log` with a message full of shell syntax is passed as one literal argument."""
    token = g.encode_plan([{"op": None, "pipeline": [["git", "status", "--porcelain"], ["wc", "-l"]]}])
    r = subprocess.run([sys.executable, str(g.ROOT / "scripts" / "agent_exec.py"), token], capture_output=True,
                       text=True, cwd=g.ROOT)
    assert r.returncode == 0 and r.stdout.strip().isdigit(), r.stderr


# ---------- scripts reject abbreviated options themselves (defence in depth) ------------------------------
@pytest.mark.parametrize("argv", [["scripts/report_issue.py", "--clos", "1"], ["scripts/docket.py", "--unbl", "x"],
                                  ["scripts/collect.py", "--dry-run", "--unl"]])
def test_scripts_refuse_abbreviated_options(argv, tmp_path):
    """Run in a throwaway copy (scripts + config only): on a vulnerable version these commands would act."""
    import shutil
    for d in ("scripts", "config"):
        shutil.copytree(g.ROOT / d, tmp_path / d, ignore=shutil.ignore_patterns("__pycache__"))
    for d in ("logs/runs", "data/metadata", "data/processed/parsed", "data/raw"):
        (tmp_path / d).mkdir(parents=True, exist_ok=True)
    (tmp_path / "logs" / "issues.jsonl").write_text('{"id": 1, "kind": "other", "summary": "x", "status": "open", '
                                                     '"evidence": []}\n')
    r = subprocess.run([sys.executable, *argv], capture_output=True, text=True, cwd=tmp_path)
    assert r.returncode == 2 and "unrecognized arguments" in r.stderr, (r.returncode, r.stdout[-200:], r.stderr[-300:])
    assert '"status": "open"' in (tmp_path / "logs" / "issues.jsonl").read_text()


# ---------- RC: an empty argument is an argument, not an operator (real-Cline smoke test finding) -----------------
def test_empty_argument_gets_the_real_refusal_reason():
    import tool_guard as tg
    with pytest.raises(tg.Bad, match="command not allowed: sed"):
        tg.plan_command("sed -i '' 's/3/99/' config/query_plan.json")
    with pytest.raises(tg.Bad) as e:
        tg.plan_command("git status ''")
    assert "redirection" not in str(e.value)
    with pytest.raises(tg.Bad, match="redirection"):
        tg.plan_command("git status > x")
