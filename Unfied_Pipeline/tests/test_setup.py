import stat

import setup as su

FRESH = {"python": "3.11", "venv": False, "venv_deps": False, "dev_deps": False, "docker": True, "searxng": False,
         "env_file": False, "env_secret": False, "env_tracked": False, "run_mode": None, "keys": {k: None for k in su.API_KEYS},
         "brew": True, "apt": False, "tesseract": None, "npm": True, "cline": False, "lmstudio": True, "model": None}


def test_first_option_is_the_recommended_one_everywhere():
    for s in su.screens(FRESH):
        assert "(recommended)" in s.options[0][1], s.title
        assert not any("(recommended)" in label for _, label in s.options[1:]), s.title
    assert su.defaults(FRESH) == {s.key: s.options[0][0] for s in su.screens(FRESH)}


def test_fresh_machine_plan_with_defaults():
    acts = su.plan(su.defaults(FRESH), FRESH)
    todo = [a.what for v in acts.values() for a in v if not a.note]
    assert todo == ["create .venv", "pip install -r requirements.txt", "pip install -r requirements-dev.txt",
                    "write a random SEARXNG_SECRET into .env", "docker compose up -d", "brew install tesseract",
                    "npm install", f"set CLINE_MODEL={su.MODELS[0]} in .env",
                    "set run_limits.mode = capped in config/query_plan.json (backup first)", "scripts/check.sh"]


def test_set_up_machine_needs_nothing_but_the_check():
    done = dict(FRESH, venv=True, venv_deps=True, dev_deps=True, searxng=True, env_file=True, env_secret=True,
                tesseract="tesseract 5.5.4", cline=True, model=su.MODELS[0], run_mode="capped")
    todo = [a.what for v in su.plan(su.defaults(done), done).values() for a in v if not a.note]
    assert todo == ["scripts/check.sh"]


def test_merge_env_fills_blanks_keeps_values_and_backs_up(tmp_path):
    env = tmp_path / ".env"
    env.write_text("# c\nSEARXNG_SECRET=keepme\nSERPER_API_KEY=\n")
    changed = su.merge_env(env, {"SEARXNG_SECRET": "new", "SERPER_API_KEY": "s1", "CLINE_MODEL": "m"})
    assert changed == ["SERPER_API_KEY", "CLINE_MODEL"]
    text = env.read_text()
    assert "SEARXNG_SECRET=keepme" in text and "SERPER_API_KEY=s1" in text and "CLINE_MODEL=m" in text
    assert (tmp_path / ".env.bak").read_text().startswith("# c")
    assert stat.S_IMODE(env.stat().st_mode) == 0o600
    su.merge_env(env, {"CLINE_MODEL": "m2"}, overwrite={"CLINE_MODEL"})
    assert "CLINE_MODEL=m2" in env.read_text() and (tmp_path / ".env.bak1").exists()


def test_new_env_starts_from_the_template(tmp_path):
    tpl = tmp_path / "example"
    tpl.write_text("# template\nSEARXNG_SECRET=\nSEARXNG_PORT=8082\n")
    su.merge_env(tmp_path / ".env", {"SEARXNG_SECRET": "abc"}, template=tpl)
    assert (tmp_path / ".env").read_text() == "# template\nSEARXNG_SECRET=abc\nSEARXNG_PORT=8082\n"


def test_keys_are_hidden_from_the_summary_and_refused_for_a_tracked_env():
    choice = dict(su.defaults(FRESH), keys="dotenv")
    keys = {"SERPER_API_KEY": "sk-very-secret"}
    acts = su.plan(choice, FRESH, keys)
    assert any(a.fn for a in acts["keys"])
    assert "sk-very-secret" not in "\n".join(su.summary(acts))
    tracked = su.plan(choice, dict(FRESH, env_tracked=True), keys)
    assert not any(a.fn for a in tracked["keys"]) and "refusing" in tracked["keys"][-1].what


def test_key_file_is_private(tmp_path, monkeypatch):
    monkeypatch.setattr(su, "KEYS_FILE", tmp_path / "cfg" / "keys.env")
    acts = su.plan(su.defaults(FRESH), FRESH, {"BRAVE_API_KEY": "b1"})
    next(a for a in acts["keys"] if a.fn).fn()
    assert (tmp_path / "cfg" / "keys.env").read_text() == "BRAVE_API_KEY=b1\n"
    assert stat.S_IMODE((tmp_path / "cfg" / "keys.env").stat().st_mode) == 0o600


def test_only_missing_keys_are_asked_for():
    asked = []
    st = dict(FRESH, keys={"SERPER_API_KEY": "environment", "BRAVE_API_KEY": None})
    got = su.ask_keys(su.defaults(st), st, getpass_fn=lambda p: asked.append(p) or "")
    assert got == {} and len(asked) == 1 and asked[0].startswith("BRAVE_API_KEY")


def test_numbered_prompts_blank_means_recommended():
    choice = su.defaults(FRESH)
    answers = iter(["", "2", "", "", "", "", "", "", ""])
    assert su.run_prompts(choice, FRESH, ask=lambda p: next(answers))
    assert choice["dev"] == "skip" and choice["python"] == "create" and choice["model"] == su.MODELS[0]


def test_run_limits_screen_writes_the_mode_with_a_backup(tmp_path, monkeypatch):
    import json
    plan = tmp_path / "query_plan.json"
    plan.write_text(json.dumps({"phases": [], "run_limits": {"mode": "capped", "max_files": 99}}))
    monkeypatch.setattr(su, "PLAN", plan)
    acts = su.plan(dict(su.defaults(FRESH), limits="unlimited"), dict(FRESH, run_mode="capped"))
    next(a for a in acts["limits"] if a.fn).fn()
    rl = json.loads(plan.read_text())["run_limits"]
    assert rl == {"mode": "unlimited", "max_files": 99, "max_companies": 25, "max_docs_per_company": 5}
    assert json.loads((tmp_path / "query_plan.json.bak").read_text())["run_limits"]["mode"] == "capped"
