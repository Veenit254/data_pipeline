"""The OS sandbox profile (macOS sandbox-exec). Probes run real processes inside the profile against a
throwaway HOME; nothing in the real home directory is read or written."""
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

import agent_sandbox as asb

pytestmark = pytest.mark.skipif(sys.platform != "darwin" or not shutil.which("sandbox-exec"),
                                reason="macOS sandbox-exec only")


@pytest.fixture
def world(tmp_path, monkeypatch):
    home, root = tmp_path / "home", tmp_path / "home" / "Projects" / "repo"
    (root / "logs").mkdir(parents=True)
    (root / "scripts").mkdir()
    (root / ".env").write_text("SERPER_API_KEY=CANARY_ENV")
    (root / ".env.example").write_text("SERPER_API_KEY=")
    (root / "data.csv").write_text("ok")
    keys = home / ".config" / "nse-unified-pipeline"
    keys.mkdir(parents=True)
    (keys / "keys.env").write_text("BRAVE_API_KEY=CANARY_KEYS")
    (home / ".ssh").mkdir()
    (home / ".ssh" / "id_ed25519").write_text("CANARY_SSH")
    (home / "notes.txt").write_text("CANARY_HOME")
    (home / ".cline").mkdir()
    monkeypatch.setattr(asb, "HOME", home.resolve())
    monkeypatch.setattr(asb, "ROOT", root.resolve())
    monkeypatch.setattr(asb, "runtime_dirs", lambda: [])
    return home.resolve(), root.resolve(), tmp_path


def run_in(profile: str, tmp: Path, *argv) -> subprocess.CompletedProcess:
    sb = tmp / "p.sb"
    sb.write_text(profile)
    return subprocess.run(["sandbox-exec", "-f", str(sb), *argv], capture_output=True, text=True)


def readable(profile, tmp, path) -> bool:
    return run_in(profile, tmp, "/bin/cat", str(path)).returncode == 0


def test_project_readable_and_writable(world):
    home, root, tmp = world
    prof = asb.profile()
    assert readable(prof, tmp, root / "data.csv")
    assert run_in(prof, tmp, "/usr/bin/touch", str(root / "logs" / "x")).returncode == 0
    assert readable(prof, tmp, root / ".env.example")


@pytest.mark.parametrize("rel", ["notes.txt", ".ssh/id_ed25519", ".config/nse-unified-pipeline/keys.env",
                                 "Projects/repo/.env"])
def test_secrets_and_home_are_unreadable(world, rel):
    home, root, tmp = world
    assert not readable(asb.profile(), tmp, home / rel)


def test_no_writes_in_home_outside_the_project(world):
    home, root, tmp = world
    assert run_in(asb.profile(), tmp, "/usr/bin/touch", str(home / "dropped")).returncode != 0
    assert not (home / "dropped").exists()


def test_cline_state_is_allowed(world):
    home, root, tmp = world
    assert run_in(asb.profile(), tmp, "/usr/bin/touch", str(home / ".cline" / "state")).returncode == 0


def test_allow_keys_opens_only_the_keys_file(world):
    home, root, tmp = world
    prof = asb.profile(allow_keys=True)
    # the keys dir is no longer explicitly denied, but home stays denied: still unreadable unless allowed
    assert not readable(prof, tmp, home / ".ssh" / "id_ed25519")
    assert not readable(prof, tmp, root / ".env")


def test_keychain_is_denied(world):
    home, root, tmp = world
    assert run_in(asb.profile(), tmp, "/usr/bin/security", "list-keychains").returncode != 0


def test_local_network_only_blocks_outbound(world):
    home, root, tmp = world
    # 192.0.2.1 is TEST-NET-1 (never routed): the sandbox refuses before any packet leaves
    r = run_in(asb.profile(local_network_only=True), tmp, "/usr/bin/curl", "-s", "-m", "3", "--noproxy", "*",
               "http://192.0.2.1/")
    assert r.returncode == 7


def test_shipped_check_passes():
    r = subprocess.run([sys.executable, str(Path(asb.__file__)), "--check"], capture_output=True, text=True)
    assert r.returncode == 0, r.stdout
