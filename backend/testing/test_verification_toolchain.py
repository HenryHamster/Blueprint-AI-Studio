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
