import json
import re
import subprocess
from pathlib import Path


def _resolve_repo_root() -> Path:
    """Find the checkout across CI mounts and ordinary checkout paths."""
    candidates = [Path("/repo")]
    for anchor in (Path(__file__).resolve(), Path.cwd()):
        candidates.extend((anchor, *anchor.parents))
    for candidate in candidates:
        if (candidate / "scripts" / "verification-toolchain.json").is_file():
            return candidate
    raise RuntimeError("repository root not found from CI mount or checkout path")


ROOT = _resolve_repo_root()
MANIFEST = ROOT / "scripts" / "verification-toolchain.json"
PROVISIONER = ROOT / "scripts" / "provision-verification-toolchain.sh"
PACKER = ROOT / "infra" / "packer" / "agent.pkr.hcl"
REQUIREMENTS = ROOT / "backend" / "requirements.txt"


def _normalize_package_name(name: str) -> str:
    return name.strip().lower().replace("_", "-")


def _requirements_pins() -> dict:
    pins = {}
    for line in REQUIREMENTS.read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        match = re.match(r"^([A-Za-z0-9_.-]+)==([A-Za-z0-9_.+!-]+)$", line)
        if match:
            pins[_normalize_package_name(match.group(1))] = match.group(2)
    return pins


def test_manifest_declares_pinned_receipt_toolchain_and_pg_trgm():
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["schema_version"] == 1
    assert manifest["python_packages"] == {
        "ruff": "0.12.9",
        "mypy": "1.20.2",
        "pytest": "7.4.4",
        "pgserver-search": "0.1.6",
        "alembic": "1.18.5",
        "sqlalchemy": "2.0.51",
    }
    assert "pg_trgm" in manifest["postgres"]["required_extensions"]
    assert all(re.match(r"^backend/alembic/versions/\d+_.+\.py$", path)
               for path in manifest["postgres"]["required_extensions"]["pg_trgm"])


def test_provisioner_and_packer_consume_the_same_manifest():
    provisioner = PROVISIONER.read_text()
    packer = PACKER.read_text()
    assert 'MANIFEST="${1:-$(dirname "$0")/verification-toolchain.json}"' in provisioner
    assert 'source      = "../../scripts/verification-toolchain.json"' in packer
    assert 'source      = "../../scripts/provision-verification-toolchain.sh"' in packer
    assert "/tmp/provision-verification-toolchain.sh /tmp/verification-toolchain.json /opt/blueprint-backend-venv" in packer
    assert "ruff==0.12.9" not in packer
    assert "pgserver-search==0.1.6" not in packer
    provision = packer.index(
        "/tmp/provision-verification-toolchain.sh /tmp/verification-toolchain.json"
    )
    root_restore = packer.index("sudo chown -R root:root /opt/blueprint-backend-venv")
    assert root_restore > provision


def test_packer_system_pip_pins_derive_from_owning_source_files(tmp_path):
    """The baked system pip3 install (pytest stack + ruff) must be pinned via
    shell variables derived from backend/requirements.txt and
    scripts/verification-toolchain.json, never hand-copied literals. Replays
    the packer's own extraction commands against real copies of those source
    files and asserts the resolved versions match — no version is restated
    here as a literal."""
    packer = PACKER.read_text()
    requirements_path = ROOT / "backend" / "requirements.txt"
    requirements_text = requirements_path.read_text()
    manifest = json.loads(MANIFEST.read_text())

    idx = packer.index('"sudo pip3 install ')
    line_end = packer.index("\n", idx)
    line = packer[idx:line_end].rstrip()
    assert line.endswith('",'), f"unexpected system pip3 install line: {line!r}"
    install_cmd = line[1:-2]

    tokens = re.findall(r"(\S+)==\$(\w+)", install_cmd)
    assert {name for name, _ in tokens} == {
        "ruff",
        "pytest",
        "pytest-asyncio",
        "pytest-mock",
        "pytest-timeout",
    }
    assert not re.search(r"==[0-9]", install_cmd), (
        "packer's baked system pip3 install must not hand-copy a literal "
        "version — single-source each package from a shell variable instead"
    )
    var_of = dict(tokens)

    def extract_var_command(var):
        prefix = f'"{var}=$('
        start = packer.index(prefix)
        end = packer.index("\n", start)
        raw_line = packer[start:end].rstrip()
        assert raw_line.endswith('",'), (
            f"unexpected derivation line for {var}: {raw_line!r}"
        )
        content = raw_line[1:-2].replace('\\"', '"')
        body_prefix = f"{var}=$("
        assert content.startswith(body_prefix) and content.endswith(")")
        return content[len(body_prefix) : -1]

    req_copy = tmp_path / "requirements.txt"
    req_copy.write_text(requirements_text)
    for pkg in ("pytest", "pytest-asyncio", "pytest-mock", "pytest-timeout"):
        cmd = extract_var_command(var_of[pkg]).replace(
            "/tmp/backend-deps-ctx/requirements.txt", str(req_copy)
        )
        assert str(req_copy) in cmd, (
            f"{pkg}'s pin is not derived from backend/requirements.txt"
        )
        result = subprocess.run(
            ["bash", "-c", cmd], capture_output=True, text=True, check=True
        )
        resolved = result.stdout.strip()
        expected = next(
            entry.split("==", 1)[1]
            for entry in requirements_text.splitlines()
            if entry.startswith(f"{pkg}==")
        )
        assert resolved == expected, (
            f"packer's baked system {pkg} pin ({resolved!r}) != "
            f"backend/requirements.txt ({expected!r})"
        )

    manifest_copy = tmp_path / "verification-toolchain.json"
    manifest_copy.write_text(MANIFEST.read_text())
    ruff_cmd = extract_var_command(var_of["ruff"]).replace(
        "/tmp/verification-toolchain.json", str(manifest_copy)
    )
    assert str(manifest_copy) in ruff_cmd, (
        "ruff's pin is not derived from scripts/verification-toolchain.json"
    )
    result = subprocess.run(
        ["bash", "-c", ruff_cmd], capture_output=True, text=True, check=True
    )
    assert result.stdout.strip() == manifest["python_packages"]["ruff"]


def test_provisioner_rejects_an_incomplete_manifest_loudly(tmp_path):
    invalid = tmp_path / "verification-toolchain.json"
    invalid.write_text(
        '{"schema_version": 1, "python_packages": {}, '
        '"postgres": {"required_extensions": {"pg_trgm": ["migration.py"]}}}'
    )
    result = subprocess.run(
        ["bash", str(PROVISIONER), str(invalid), str(tmp_path / "venv")],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 2
    assert "VERIFICATION_TOOLCHAIN_INVALID" in result.stderr
    assert "ruff, mypy, pytest, pgserver-search, and alembic must be pinned" in result.stderr


def test_manifest_and_requirements_agree_on_overlapping_package_pins():
    """The provisioner installs backend/requirements.txt BEFORE the manifest's
    pinned packages (provision-verification-toolchain.sh: "Installed first so
    the manifest pins below win any version overlap"), so any package pinned
    in both silently takes the MANIFEST's version in every provisioned venv —
    bare-runner and packer-baked alike. Drift here is a silent downgrade of
    a requirements.txt bump, never surfaced by pip (which exits 0 on an
    already-installed version). This test derives the overlap directly from
    both files, so drift is only fixable by making the two agree — never by
    editing a literal in this test.

    Packages that are manifest-only (pgserver-search, ruff, mypy) have no
    counterpart in backend/requirements.txt and are expected to be absent
    from `overlap` below; that is fine and not asserted against.
    """
    manifest = json.loads(MANIFEST.read_text())
    manifest_pins = {
        _normalize_package_name(name): version
        for name, version in manifest["python_packages"].items()
    }
    requirements_pins = _requirements_pins()

    overlap = sorted(set(manifest_pins) & set(requirements_pins))
    assert overlap, "expected at least one package pinned in both files"

    mismatches = [
        f"{name}: manifest={manifest_pins[name]!r} requirements.txt={requirements_pins[name]!r}"
        for name in overlap
        if manifest_pins[name] != requirements_pins[name]
    ]
    assert not mismatches, (
        "scripts/verification-toolchain.json and backend/requirements.txt "
        "disagree on an overlapping package pin (the manifest pin silently "
        "wins during provisioning): " + "; ".join(mismatches)
    )
