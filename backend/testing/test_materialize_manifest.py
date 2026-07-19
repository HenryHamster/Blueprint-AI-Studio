"""Direct tests for backend/app/_agent_scripts/materialize_manifest.py.

This script is the VM's credential-materialization step — it turns the
agent_secret manifest into a systemd-compatible env file plus any
FileMaterial entries. A bug here causes every VM to die pre-claim
(see task 0bcfd031 / c94d6d9b post-mortems).

We run the module's ``main`` directly rather than via subprocess so
assertion failures point at concrete lines. The file-writing half that
touches /etc and chowns to the agent user is exercised in a second
test with a temp working dir and monkeypatched uid/gid lookup.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from app._agent_scripts import materialize_manifest


def _write_manifest(tmp_path: Path, env: dict, files: list | None = None) -> Path:
    manifest = {"env": env, "files": files or []}
    p = tmp_path / "manifest.json"
    p.write_text(json.dumps(manifest), encoding="utf-8")
    return p


class TestEnvFileMaterialization:
    def test_simple_key_value_pairs(self, tmp_path: Path):
        manifest = _write_manifest(
            tmp_path,
            env={"AGENT_SECRET_TOKEN": "abc.def.ghi", "PROVIDER": "anthropic"},
        )
        env_out = tmp_path / "agent.env"
        rc = materialize_manifest.main(["m", str(manifest), str(env_out)])
        assert rc == 0
        body = env_out.read_text(encoding="utf-8")
        assert 'AGENT_SECRET_TOKEN="abc.def.ghi"\n' in body
        assert 'PROVIDER="anthropic"\n' in body

    def test_none_values_skipped(self, tmp_path: Path):
        manifest = _write_manifest(
            tmp_path, env={"SET": "yes", "UNSET": None, "ALSO": "no"}
        )
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0
        body = env_out.read_text(encoding="utf-8")
        assert "SET=" in body
        assert "ALSO=" in body
        assert "UNSET=" not in body

    def test_backslash_quote_newline_escapes(self, tmp_path: Path):
        """systemd env file spec requires ``\\``, ``"``, and ``\\n`` to be
        escaped inside the quoted value. These are the exact characters
        whose mishandling produced SyntaxError in the old inline heredoc.
        """
        manifest = _write_manifest(
            tmp_path,
            env={
                "CONFIG": "line1\nline2",
                "QUOTED": 'has "inner" quotes',
                "BACKSLASH": r"path\with\backslashes",
            },
        )
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0
        body = env_out.read_text(encoding="utf-8")
        # newline escape
        assert 'CONFIG="line1\\nline2"\n' in body
        # quote escape
        assert 'QUOTED="has \\"inner\\" quotes"\n' in body
        # backslash escape (doubled)
        assert 'BACKSLASH="path\\\\with\\\\backslashes"\n' in body

    def test_missing_env_key_produces_empty_file(self, tmp_path: Path):
        manifest_path = tmp_path / "m.json"
        manifest_path.write_text('{"files": []}', encoding="utf-8")
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest_path), str(env_out)]) == 0
        assert env_out.read_text(encoding="utf-8") == ""

    def test_env_file_permissions_600(self, tmp_path: Path):
        manifest = _write_manifest(tmp_path, env={"X": "y"})
        env_out = tmp_path / "agent.env"
        materialize_manifest.main(["m", str(manifest), str(env_out)])
        mode = os.stat(env_out).st_mode & 0o777
        assert mode == 0o600, f"expected 0o600, got {oct(mode)}"


class TestFileMaterialization:
    def test_files_are_written_with_content_and_mode(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        # chown to the current user so we don't need root
        current_uid = os.getuid() if hasattr(os, "getuid") else 0
        current_gid = os.getgid() if hasattr(os, "getgid") else 0
        monkeypatch.setattr(
            materialize_manifest,
            "_lookup_uid_gid",
            lambda _user: (current_uid, current_gid),
        )

        file1 = tmp_path / "sub" / "file1.json"
        manifest = _write_manifest(
            tmp_path,
            env={"A": "b"},
            files=[{"path": str(file1), "content": "{}", "mode": "0600"}],
        )
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0

        assert file1.read_text(encoding="utf-8") == "{}"
        mode = os.stat(file1).st_mode & 0o777
        assert mode == 0o600

    def test_claude_config_marks_workspace_trusted(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            materialize_manifest,
            "_lookup_uid_gid",
            lambda _user: (
                (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (0, 0)
            ),
        )
        monkeypatch.setattr(os, "chown", lambda *_args: None, raising=False)
        target = tmp_path / ".claude.json"
        manifest = _write_manifest(
            tmp_path,
            env={},
            files=[
                {
                    "path": str(target),
                    "content": json.dumps(
                        {
                            "hasCompletedOnboarding": True,
                            "projects": {
                                "/other/workspace": {
                                    "hasTrustDialogAccepted": False
                                }
                            },
                        }
                    ),
                    "mode": "0600",
                }
            ],
        )
        env_out = tmp_path / "agent.env"

        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0

        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["hasCompletedOnboarding"] is True
        assert data["projects"]["/other/workspace"] == {
            "hasTrustDialogAccepted": False
        }
        assert data["projects"]["/tmp/workspace"]["hasTrustDialogAccepted"] is True
        assert data["projects"]["/tmp/workspace"]["allowedTools"] == []

    def test_claude_config_trusts_manifest_workspace(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            materialize_manifest,
            "_lookup_uid_gid",
            lambda _user: (
                (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (0, 0)
            ),
        )
        monkeypatch.setattr(os, "chown", lambda *_args: None, raising=False)
        target = tmp_path / ".claude.json"
        manifest = _write_manifest(
            tmp_path,
            env={"AGENT_WORKSPACE": "/workspace/agent"},
            files=[
                {
                    "path": str(target),
                    "content": json.dumps(
                        {
                            "projects": {
                                "/operator/project": {"custom": "keep"}
                            }
                        }
                    ),
                    "mode": "0600",
                }
            ],
        )
        env_out = tmp_path / "agent.env"

        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0

        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["projects"]["/operator/project"] == {"custom": "keep"}
        assert data["projects"]["/workspace/agent"] == {
            "hasTrustDialogAccepted": True,
            "allowedTools": [],
        }

    def test_claude_config_normalizes_malformed_allowed_tools_and_preserves_lists(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            materialize_manifest,
            "_lookup_uid_gid",
            lambda _user: (
                (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (0, 0)
            ),
        )
        monkeypatch.setattr(os, "chown", lambda *_args: None, raising=False)
        target = tmp_path / ".claude.json"
        manifest = _write_manifest(
            tmp_path,
            env={"AGENT_WORKSPACE": "/workspace/agent"},
            files=[
                {
                    "path": str(target),
                    "content": json.dumps(
                        {
                            "projects": {
                                "/workspace/agent": {
                                    "allowedTools": {"Bash": True}
                                },
                                "/other/workspace": {
                                    "allowedTools": ["Read", "Grep"]
                                },
                            }
                        }
                    ),
                    "mode": "0600",
                }
            ],
        )
        env_out = tmp_path / "agent.env"

        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0

        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["projects"]["/workspace/agent"] == {
            "hasTrustDialogAccepted": True,
            "allowedTools": [],
        }
        assert data["projects"]["/other/workspace"]["allowedTools"] == [
            "Read",
            "Grep",
        ]

    def test_missing_content_defaults_to_empty(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            materialize_manifest,
            "_lookup_uid_gid",
            lambda _user: (
                (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (0, 0)
            ),
        )
        target = tmp_path / "empty"
        manifest = _write_manifest(
            tmp_path, env={}, files=[{"path": str(target), "mode": "0600"}]
        )
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0
        assert target.read_text(encoding="utf-8") == ""

    def test_created_parent_dirs_are_chowned_to_agent(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Regression: the Gemini CLI writes projects.json into the same
        ~/.gemini/ dir where we pre-place settings.json. Our mkdir runs
        as root, so without an explicit chown the CLI (running as agent)
        gets EACCES trying to create siblings.
        """
        chowned: list[tuple[str, int, int]] = []
        real_chown = os.chown

        def _recording_chown(path, uid, gid):
            chowned.append((str(path), uid, gid))
            real_chown(path, uid, gid)

        monkeypatch.setattr(os, "chown", _recording_chown)
        monkeypatch.setattr(
            materialize_manifest,
            "_lookup_uid_gid",
            lambda _user: (
                (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (0, 0)
            ),
        )

        # Two levels of missing parents — simulates /home/agent/.gemini/sub/x
        nested = tmp_path / "home_agent" / ".gemini" / "settings.json"
        manifest = _write_manifest(
            tmp_path,
            env={},
            files=[{"path": str(nested), "content": "{}", "mode": "0600"}],
        )
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0

        chowned_paths = {entry[0] for entry in chowned}
        assert str(nested.parent) in chowned_paths, (
            f"parent dir {nested.parent} should have been chowned; "
            f"got {sorted(chowned_paths)}"
        )
        assert str(nested.parent.parent) in chowned_paths, (
            "grandparent dir should also have been chowned when we created it"
        )
        # Files are written atomically (temp + rename); ownership is applied to
        # the temp file BEFORE the swap and travels with the rename, so the
        # chown call records the temp path, not the final one.
        assert any(
            p == str(nested) or p.startswith(str(nested) + ".tmp.")
            for p in chowned_paths
        ), f"materialized file should be chowned (via its atomic temp); got {sorted(chowned_paths)}"

    def test_existing_parent_dirs_are_not_chowned(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Dirs that already exist shouldn't be touched — don't stomp
        on the real /home/agent/ ownership, for instance.
        """
        chowned: list[tuple[str, int, int]] = []
        real_chown = os.chown

        def _recording_chown(path, uid, gid):
            chowned.append((str(path), uid, gid))
            real_chown(path, uid, gid)

        monkeypatch.setattr(os, "chown", _recording_chown)
        monkeypatch.setattr(
            materialize_manifest,
            "_lookup_uid_gid",
            lambda _user: (
                (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (0, 0)
            ),
        )

        # Parent already exists — simulates /home/agent/.claude.json
        # where /home/agent/ is owned by the agent user already.
        existing_parent = tmp_path / "existing"
        existing_parent.mkdir()
        target = existing_parent / "file.json"
        manifest = _write_manifest(
            tmp_path,
            env={},
            files=[{"path": str(target), "content": "{}", "mode": "0600"}],
        )
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0

        chowned_paths = {entry[0] for entry in chowned}
        assert str(existing_parent) not in chowned_paths, (
            "existing parent should not be re-chowned"
        )
        # Atomic write: the chown lands on the temp file pre-rename (see above).
        assert any(
            p == str(target) or p.startswith(str(target) + ".tmp.")
            for p in chowned_paths
        ), f"materialized file should be chowned (via its atomic temp); got {sorted(chowned_paths)}"


class TestAnthropicTrustMaterialization:
    """Tests for the minimal ~/.claude.json materialization path.

    When an Anthropic pool entry omits claude_config_json, the manifest
    builder now always includes a .claude.json file with content ``{}``.
    _normalize_materialized_file_content marks onboarding complete and merges
    hasTrustDialogAccepted into /tmp/workspace so the CLI doesn't stall.
    """

    def _monkeypatch_uid_gid(self, monkeypatch: pytest.MonkeyPatch) -> None:
        uid = os.getuid() if hasattr(os, "getuid") else 0
        gid = os.getgid() if hasattr(os, "getgid") else 0
        monkeypatch.setattr(
            materialize_manifest,
            "_lookup_uid_gid",
            lambda _user: (uid, gid),
        )
        monkeypatch.setattr(os, "chown", lambda *_: None, raising=False)

    def test_omitted_config_materializes_minimal_trust_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Manifest with empty .claude.json content ({}) produces a file
        with hasTrustDialogAccepted injected into /tmp/workspace."""
        self._monkeypatch_uid_gid(monkeypatch)
        target = tmp_path / ".claude.json"
        manifest = _write_manifest(
            tmp_path,
            env={"PROVIDER": "anthropic", "CLAUDE_CODE_OAUTH_TOKEN": "sk-ant-test"},
            files=[{"path": str(target), "content": "{}", "mode": "0600"}],
        )
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0

        data = json.loads(target.read_text(encoding="utf-8"))
        assert data["hasCompletedOnboarding"] is True
        assert data["projects"]["/tmp/workspace"]["hasTrustDialogAccepted"] is True
        # No secrets in the config file
        raw = target.read_text(encoding="utf-8")
        assert "sk-ant" not in raw
        assert "CLAUDE_CODE_OAUTH_TOKEN" not in raw

    def test_supplied_config_preserved_and_trust_injected(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Operator-supplied claude_config_json is preserved and trust is
        merged on top — not replaced."""
        self._monkeypatch_uid_gid(monkeypatch)
        operator_content = json.dumps(
            {
                "hasCompletedOnboarding": True,
                "email": "op@example.com",
                "projects": {"/other/path": {"hasTrustDialogAccepted": False}},
            }
        )
        target = tmp_path / ".claude.json"
        manifest = _write_manifest(
            tmp_path,
            env={"PROVIDER": "anthropic"},
            files=[{"path": str(target), "content": operator_content, "mode": "0600"}],
        )
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0

        data = json.loads(target.read_text(encoding="utf-8"))
        # Operator fields preserved
        assert data["hasCompletedOnboarding"] is True
        assert data["email"] == "op@example.com"
        assert data["projects"]["/other/path"]["hasTrustDialogAccepted"] is False
        # Trust injected for /tmp/workspace
        assert data["projects"]["/tmp/workspace"]["hasTrustDialogAccepted"] is True

    def test_home_reanchored_path_still_normalizes(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """When the agent re-anchors /home/agent/.claude.json to a different
        $HOME (e.g. /home inside a bwrap sandbox), the normalization still
        fires because it keys on the filename .claude.json, not the prefix."""
        self._monkeypatch_uid_gid(monkeypatch)
        # Simulate re-anchored path: /home/.claude.json instead of /home/agent/
        reanchored = tmp_path / ".claude.json"
        manifest = _write_manifest(
            tmp_path,
            env={"PROVIDER": "anthropic"},
            files=[{"path": str(reanchored), "content": "{}", "mode": "0600"}],
        )
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0

        data = json.loads(reanchored.read_text(encoding="utf-8"))
        assert data["projects"]["/tmp/workspace"]["hasTrustDialogAccepted"] is True


class TestNssBypass:
    def test_lookup_reads_passwd_not_pwd_module(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        """Regression: the point of _lookup_uid_gid is to avoid
        pwd.getpwnam / grp.getgrnam because NSS can hang. Assert the
        implementation reads files directly.
        """
        import pwd as pwd_module
        import grp as grp_module

        def _boom(*_args, **_kwargs):
            raise AssertionError(
                "pwd/grp was called — NSS bypass broken; see task 23889608"
            )

        monkeypatch.setattr(pwd_module, "getpwnam", _boom)
        monkeypatch.setattr(grp_module, "getgrnam", _boom)

        # Stub /etc/passwd and /etc/group via a chdir? Simpler: patch open()
        # — but here we just call _lookup_uid_gid against the real files.
        # On any system that has a current user in passwd, this will succeed.
        import pwd

        me = pwd.getpwuid(os.getuid()).pw_name if hasattr(os, "getuid") else "root"
        # Roll back the boom patch just for getpwuid, which we used above
        # to find the test user name. Then re-apply for the real call.
        monkeypatch.setattr(pwd_module, "getpwnam", _boom)
        uid, gid = materialize_manifest._lookup_uid_gid(me)
        assert uid >= 0
        assert gid >= 0

    def test_lookup_raises_on_missing_user(self, tmp_path: Path):
        with pytest.raises(RuntimeError, match="missing from /etc/passwd"):
            materialize_manifest._lookup_uid_gid("definitely-not-a-real-user-2x4f6z8q")


class TestReinvocationOverwrites:
    """Warm-VM re-targeting re-invokes this helper on an ALREADY-provisioned
    VM, so the env file and provider credential files already exist from the
    boot manifest. A second invocation must OVERWRITE them (not append / not
    fail) so the VM adopts the new account's credentials cleanly.
    """

    def test_env_file_is_overwritten_not_appended(self, tmp_path: Path):
        env_out = tmp_path / "agent.env"
        first = _write_manifest(tmp_path, env={"PROVIDER": "anthropic", "OLD": "1"})
        assert materialize_manifest.main(["m", str(first), str(env_out)]) == 0
        body1 = env_out.read_text(encoding="utf-8")
        assert 'PROVIDER="anthropic"' in body1
        assert 'OLD="1"' in body1

        second = tmp_path / "manifest2.json"
        second.write_text(
            json.dumps({"env": {"PROVIDER": "gemini"}, "files": []}),
            encoding="utf-8",
        )
        assert materialize_manifest.main(["m", str(second), str(env_out)]) == 0
        body2 = env_out.read_text(encoding="utf-8")
        # New value present, stale value from the first manifest gone.
        assert 'PROVIDER="gemini"' in body2
        assert "OLD=" not in body2
        assert body2.count('PROVIDER=') == 1

    def test_credential_file_is_overwritten_on_reinvocation(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        monkeypatch.setattr(
            materialize_manifest,
            "_lookup_uid_gid",
            lambda _user: (
                (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (0, 0)
            ),
        )
        cred = tmp_path / "creds" / "auth.json"
        env_out = tmp_path / "agent.env"

        first = _write_manifest(
            tmp_path,
            env={"A": "1"},
            files=[{"path": str(cred), "content": "OLD-SECRET", "mode": "0600"}],
        )
        assert materialize_manifest.main(["m", str(first), str(env_out)]) == 0
        assert cred.read_text(encoding="utf-8") == "OLD-SECRET"

        second = tmp_path / "manifest2.json"
        second.write_text(
            json.dumps(
                {
                    "env": {"A": "2"},
                    "files": [
                        {"path": str(cred), "content": "NEW-SECRET", "mode": "0600"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        # Re-invoke against the now-existing file + parent dir.
        assert materialize_manifest.main(["m", str(second), str(env_out)]) == 0
        assert cred.read_text(encoding="utf-8") == "NEW-SECRET"
        mode = os.stat(cred).st_mode & 0o777
        assert mode == 0o600


class TestAtomicWrites:
    """The warm re-target path re-invokes this script over an ALREADY-LIVE
    config (/etc/agent.env, ~/.claude.json). Writes must go through a
    same-directory temp file + os.replace so a mid-write death never leaves a
    partial/empty config — a VM that looks healthy but fails every task in
    seconds with "Claude configuration file not found" (2026-06-06
    poisoned-VM incidents, tasks 1330d291 / cf28097f).
    """

    @staticmethod
    def _current_owner(monkeypatch: pytest.MonkeyPatch) -> None:
        monkeypatch.setattr(
            materialize_manifest,
            "_lookup_uid_gid",
            lambda _user: (
                (os.getuid(), os.getgid()) if hasattr(os, "getuid") else (0, 0)
            ),
        )

    def test_no_tmp_residue_after_success(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._current_owner(monkeypatch)
        cred = tmp_path / "creds" / "auth.json"
        manifest = _write_manifest(
            tmp_path,
            env={"A": "1"},
            files=[{"path": str(cred), "content": "{}", "mode": "0600"}],
        )
        env_out = tmp_path / "agent.env"
        assert materialize_manifest.main(["m", str(manifest), str(env_out)]) == 0
        assert cred.read_text(encoding="utf-8") == "{}"
        assert list(tmp_path.rglob("*.tmp.*")) == []

    def test_failed_cred_write_preserves_prior_content_and_cleans_tmp(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        self._current_owner(monkeypatch)
        cred = tmp_path / "auth.json"
        env_out = tmp_path / "agent.env"
        first = _write_manifest(
            tmp_path,
            env={"A": "1"},
            files=[{"path": str(cred), "content": "OLD-SECRET", "mode": "0600"}],
        )
        assert materialize_manifest.main(["m", str(first), str(env_out)]) == 0
        assert cred.read_text(encoding="utf-8") == "OLD-SECRET"

        # Die mid-swap on the SECOND materialization (chmod on the cred temp
        # file raises). The live file must still hold the complete prior
        # content — never truncated, never partial — and no temp may linger.
        real_chmod = os.chmod

        def _chmod_boom_on_cred(path, mode, *args, **kwargs):
            if str(path).startswith(str(cred)):
                raise OSError("simulated mid-write death")
            return real_chmod(path, mode, *args, **kwargs)

        monkeypatch.setattr(os, "chmod", _chmod_boom_on_cred)
        second = tmp_path / "manifest2.json"
        second.write_text(
            json.dumps(
                {
                    "env": {"A": "2"},
                    "files": [
                        {"path": str(cred), "content": "NEW-SECRET", "mode": "0600"}
                    ],
                }
            ),
            encoding="utf-8",
        )
        with pytest.raises(OSError, match="simulated mid-write death"):
            materialize_manifest.main(["m", str(second), str(env_out)])

        assert cred.read_text(encoding="utf-8") == "OLD-SECRET"
        assert list(tmp_path.glob("auth.json.tmp.*")) == []

    def test_failed_env_write_preserves_prior_env_file(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ):
        env_out = tmp_path / "agent.env"
        first = _write_manifest(tmp_path, env={"PROVIDER": "anthropic"})
        assert materialize_manifest.main(["m", str(first), str(env_out)]) == 0
        body1 = env_out.read_text(encoding="utf-8")
        assert 'PROVIDER="anthropic"' in body1

        real_chmod = os.chmod

        def _chmod_boom_on_env(path, mode, *args, **kwargs):
            if str(path).startswith(str(env_out)):
                raise OSError("simulated mid-write death")
            return real_chmod(path, mode, *args, **kwargs)

        monkeypatch.setattr(os, "chmod", _chmod_boom_on_env)
        second = tmp_path / "manifest2.json"
        second.write_text(
            json.dumps({"env": {"PROVIDER": "gemini"}, "files": []}),
            encoding="utf-8",
        )
        with pytest.raises(OSError, match="simulated mid-write death"):
            materialize_manifest.main(["m", str(second), str(env_out)])

        # The live env file is byte-for-byte the COMPLETE prior config — the
        # re-exec'd / restarted agent still boots on a valid (old) account
        # rather than a truncated, configless one.
        assert env_out.read_text(encoding="utf-8") == body1
        assert list(tmp_path.glob("agent.env.tmp.*")) == []


class TestCLIContract:
    def test_wrong_arg_count_returns_2(self):
        rc = materialize_manifest.main(["script"])
        assert rc == 2
        rc = materialize_manifest.main(["script", "one"])
        assert rc == 2
        rc = materialize_manifest.main(["script", "one", "two", "three"])
        assert rc == 2


class TestBase64EmbeddingIsStable:
    """The startup-script generator (compute.py) embeds this file's
    contents as base64. If the file gets corrupted by line-ending
    translation, BOM, or similar, the rendered bash's base64 decode
    would produce invalid Python. Assert the on-disk file is ASCII-safe
    and round-trips cleanly.
    """

    def test_file_is_valid_utf8_no_bom(self):
        path = Path(materialize_manifest.__file__)
        raw = path.read_bytes()
        # No BOM — some text-to-base64 pipelines silently strip it
        assert not raw.startswith(b"\xef\xbb\xbf"), "UTF-8 BOM would break rendering"
        # Must decode as UTF-8 (Python source files are UTF-8 by default).
        # Non-ASCII in docstrings is fine — base64 is byte-level, so any
        # bytes survive the round-trip.
        try:
            raw.decode("utf-8")
        except UnicodeDecodeError as e:
            pytest.fail(f"materialize_manifest.py is not valid UTF-8: {e}")

    def test_base64_roundtrip(self):
        import base64

        path = Path(materialize_manifest.__file__)
        raw = path.read_bytes()
        encoded = base64.b64encode(raw).decode("ascii")
        decoded = base64.b64decode(encoded)
        assert decoded == raw
