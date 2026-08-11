import json
import re
import subprocess
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
MANIFEST = ROOT / "scripts" / "verification-toolchain.json"
PROVISIONER = ROOT / "scripts" / "provision-verification-toolchain.sh"
PACKER = ROOT / "infra" / "packer" / "agent.pkr.hcl"


def test_manifest_declares_pinned_receipt_toolchain_and_pg_trgm():
    manifest = json.loads(MANIFEST.read_text())
    assert manifest["schema_version"] == 1
    assert manifest["python_packages"] == {
        "ruff": "0.12.9",
        "mypy": "1.20.2",
        "pytest": "7.4.4",
        "pgserver-search": "0.1.6",
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
    assert "ruff, mypy, pytest, and pgserver-search must be pinned" in result.stderr
