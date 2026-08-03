#!/usr/bin/env python3
"""Plan and render a KVM boundary for Blueprint GitHub Actions runners.

The host-side lifecycle is implemented by ``install-github-actions-vm.sh``.
This module owns validation and cloud-init rendering so shell never has to
interpolate SSH keys, YAML, or embedded guest scripts.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import re
import stat
import tempfile
from dataclasses import asdict, dataclass
from pathlib import Path, PurePosixPath


_SCRIPT_DIR = Path(__file__).resolve().parent
_ASSET_DIR = _SCRIPT_DIR / "github-actions-vm"
_SSH_KEY_RE = re.compile(
    r"^(ssh-ed25519|ecdsa-sha2-nistp(?:256|384|521)|sk-ssh-ed25519@openssh\.com) "
    r"[A-Za-z0-9+/]+={0,3}(?: [A-Za-z0-9_.@-]{1,128})?$"
)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")

UBUNTU_CLOUD_IMAGE_URL = (
    "https://cloud-images.ubuntu.com/releases/24.04/release-20260725/"
    "ubuntu-24.04-server-cloudimg-amd64.img"
)
UBUNTU_CLOUD_IMAGE_SHA256 = (
    "d1940f7d69d343355e183dff1e08a59852d32e7309baa7a4bad8365b11b005ac"
)
HOST_CONTAINER_BASE = (
    "ubuntu@sha256:52df9b1ee71626e0088f7d400d5c6b5f7bb916f8f0c82b474289a4ece6cf3faf"
)


@dataclass(frozen=True)
class VmPlan:
    state_root: str
    host_image: str
    cloud_image_url: str
    cloud_image_sha256: str
    cpus: int
    memory_mib: int
    disk_gib: int
    runner_count: int
    ssh_host_port: int
    guest_user: str
    firewall_container: str
    qemu_container: str


def _absolute_posix_path(value: str, field: str) -> str:
    path = PurePosixPath(value)
    if (
        not path.is_absolute()
        or ".." in path.parts
        or any(ord(char) < 32 or ord(char) == 127 for char in value)
        or any(char.isspace() for char in value)
    ):
        raise ValueError(
            f"{field} must be an absolute, whitespace-free POSIX path without '..'"
        )
    return str(path)


def build_plan(
    *,
    state_root: str,
    cpus: int = 12,
    memory_mib: int = 32768,
    disk_gib: int = 80,
    runner_count: int = 4,
    ssh_host_port: int = 2222,
) -> VmPlan:
    """Return a validated, non-executing deployment plan."""

    state_root = _absolute_posix_path(state_root, "state_root")
    if not 2 <= cpus <= 64:
        raise ValueError("cpus must be between 2 and 64")
    if not 4096 <= memory_mib <= 131072:
        raise ValueError("memory_mib must be between 4096 and 131072")
    if not 24 <= disk_gib <= 2048:
        raise ValueError("disk_gib must be between 24 and 2048")
    if not 1 <= runner_count <= 16:
        raise ValueError("runner_count must be between 1 and 16")
    if not 1024 <= ssh_host_port <= 65535:
        raise ValueError("ssh_host_port must be an unprivileged TCP port")
    return VmPlan(
        state_root=state_root,
        host_image="blueprint/github-actions-kvm:24.04",
        cloud_image_url=UBUNTU_CLOUD_IMAGE_URL,
        cloud_image_sha256=UBUNTU_CLOUD_IMAGE_SHA256,
        cpus=cpus,
        memory_mib=memory_mib,
        disk_gib=disk_gib,
        runner_count=runner_count,
        ssh_host_port=ssh_host_port,
        guest_user="blueprint-ci",
        firewall_container="blueprint-actions-vm-net",
        qemu_container="blueprint-actions-vm",
    )


def validate_ssh_public_key(value: str) -> str:
    value = value.strip()
    if len(value) > 1024 or not _SSH_KEY_RE.fullmatch(value):
        raise ValueError(
            "SSH public key must be one supported single-line public key without "
            "shell or YAML metacharacters in its comment"
        )
    return value


def _asset_b64(name: str) -> str:
    path = _ASSET_DIR / name
    data = path.read_bytes()
    if not data or len(data) > 256_000:
        raise ValueError(f"guest asset {name!r} has an invalid size")
    return base64.b64encode(data).decode("ascii")


def render_cloud_init(plan: VmPlan, ssh_public_key: str) -> str:
    """Render NoCloud user-data with guest assets encoded losslessly."""

    key = validate_ssh_public_key(ssh_public_key)
    assets = (
        ("bootstrap-guest.sh", "0755"),
        ("install-runners.sh", "0755"),
        ("prepare-runner-network.sh", "0755"),
        ("blueprint-runner-network@.service", "0644"),
        ("blueprint-dockerd@.service", "0644"),
        ("blueprint-github-actions@.service", "0644"),
        ("prune-runner-volumes.sh", "0755"),
        ("thaw-owned-runners.sh", "0755"),
        ("blueprint-actions-volume-gc.service", "0644"),
        ("blueprint-actions-volume-gc.timer", "0644"),
        ("install-volume-maintenance.sh", "0755"),
    )
    write_files = []
    for name, permissions in assets:
        write_files.extend(
            (
                f"  - path: /usr/local/lib/blueprint/github-actions-vm/{name}",
                "    owner: root:root",
                f"    permissions: '{permissions}'",
                "    encoding: b64",
                f"    content: {_asset_b64(name)}",
            )
        )
    return (
        "#cloud-config\n"
        "ssh_pwauth: false\n"
        "disable_root: true\n"
        "users:\n"
        "  - default\n"
        f"  - name: {plan.guest_user}\n"
        "    gecos: Blueprint CI operator\n"
        "    groups: [adm, sudo]\n"
        "    sudo: ['ALL=(ALL) NOPASSWD:ALL']\n"
        "    shell: /bin/bash\n"
        f"    ssh_authorized_keys: [{json.dumps(key)}]\n"
        "package_update: true\n"
        "package_upgrade: true\n"
        "packages:\n"
        "  - ca-certificates\n"
        "  - curl\n"
        "  - docker.io\n"
        "  - git\n"
        "  - jq\n"
        "  - openssh-server\n"
        "  - rsync\n"
        "  - xz-utils\n"
        "write_files:\n"
        + "\n".join(write_files)
        + "\n"
        "runcmd:\n"
        "  - [/usr/local/lib/blueprint/github-actions-vm/bootstrap-guest.sh]\n"
        "final_message: 'Blueprint Actions VM bootstrap complete'\n"
    )


def cloud_init_sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def atomic_write_private(path: Path, text: str) -> None:
    """Write rendered cloud-init without a world-readable intermediate."""

    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError(f"refusing symlink output path: {path}")
    fd, temporary = tempfile.mkstemp(prefix=f".{path.name}.", dir=path.parent)
    try:
        os.fchmod(fd, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as handle:
            handle.write(text)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
        if os.name != "nt":
            mode = stat.S_IMODE(path.stat().st_mode)
            if mode != 0o600:
                raise OSError(f"rendered cloud-init mode is {mode:o}, expected 600")
    except Exception:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--state-root",
        default="/home/blueprint/.blueprint/github-actions-vm",
    )
    parser.add_argument("--cpus", type=int, default=12)
    parser.add_argument("--memory-mib", type=int, default=32768)
    parser.add_argument("--disk-gib", type=int, default=80)
    parser.add_argument("--runner-count", type=int, default=4)
    parser.add_argument("--ssh-host-port", type=int, default=2222)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("plan")
    render = sub.add_parser("render-cloud-init")
    render.add_argument("--ssh-public-key-file", type=Path, required=True)
    render.add_argument("--output", type=Path, required=True)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        plan = build_plan(
            state_root=args.state_root,
            cpus=args.cpus,
            memory_mib=args.memory_mib,
            disk_gib=args.disk_gib,
            runner_count=args.runner_count,
            ssh_host_port=args.ssh_host_port,
        )
        if args.command == "plan":
            print(json.dumps(asdict(plan), indent=2, sort_keys=True))
            return 0
        key = args.ssh_public_key_file.read_text(encoding="utf-8")
        rendered = render_cloud_init(plan, key)
        atomic_write_private(args.output, rendered)
        print(
            json.dumps(
                {
                    "output": str(args.output),
                    "sha256": cloud_init_sha256(rendered),
                    "runner_count": plan.runner_count,
                },
                sort_keys=True,
            )
        )
        return 0
    except (OSError, ValueError) as exc:
        _parser().error(str(exc))
    return 2


if __name__ == "__main__":
    raise SystemExit(main())
