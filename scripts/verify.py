"""Pre-deploy verification — syntax and type checks.

Runs the same checks CI would catch, but locally before pushing.

Usage:
    python scripts/verify.py              — run all checks
    python scripts/verify.py backend      — Python syntax only
    python scripts/verify.py agent        — TypeScript only
    python scripts/verify.py --changed    — only check files with uncommitted changes
"""
import subprocess
import sys
import os

PROJECT_ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from utf8_io import force_utf8_streams  # noqa: E402

# Python files to check (backend)
BACKEND_FILES = [
    "app/main.py",
    "app/compute.py",
    "app/admin.py",
    "app/auth.py",
    "app/models.py",
    "app/database.py",
    "app/github.py",
    "app/security.py",
]


def _run(cmd, cwd=None, label=""):
    """Run a command and return (success, output)."""
    try:
        # errors="replace" because this echoes EXTERNAL tool output: tsc and
        # py_compile diagnostics carry curly quotes and box rules, and a
        # verification run must survive printing the failure it just found.
        result = subprocess.run(
            cmd, capture_output=True, text=True,
            encoding="utf-8", errors="replace", timeout=120,
            cwd=cwd or PROJECT_ROOT
        )
        if result.returncode != 0:
            print(f"  FAIL: {label}")
            if result.stderr:
                for line in result.stderr.strip().split("\n")[:20]:
                    print(f"    {line}")
            if result.stdout:
                for line in result.stdout.strip().split("\n")[:20]:
                    print(f"    {line}")
            return False
        return True
    except FileNotFoundError:
        print(f"  SKIP: {label} (command not found)")
        return True
    except subprocess.TimeoutExpired:
        print(f"  FAIL: {label} (timed out)")
        return False


def _get_changed_files():
    """Get list of changed files (staged + unstaged)."""
    # Paths come back as git wrote them; a non-ascii filename must not abort
    # the verification pass with a locale decode error.
    result = subprocess.run(
        ["git", "diff", "--name-only", "HEAD"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=PROJECT_ROOT
    )
    staged = subprocess.run(
        ["git", "diff", "--name-only", "--cached"],
        capture_output=True, text=True, encoding="utf-8", errors="replace",
        cwd=PROJECT_ROOT
    )
    files = set(result.stdout.strip().split("\n") + staged.stdout.strip().split("\n"))
    return [f for f in files if f]


def check_backend(only_changed=False):
    """Syntax-check Python files."""
    print("\n--- Backend (Python) ---")

    files = BACKEND_FILES
    if only_changed:
        changed = _get_changed_files()
        files = [f for f in BACKEND_FILES if f in changed or f"backend/{f}" in changed]
        if not files:
            print("  No changed backend files.")
            return True

    # Try Docker first, fall back to local python
    docker_available = subprocess.run(
        ["docker", "compose", "version"], capture_output=True, cwd=PROJECT_ROOT
    ).returncode == 0

    all_ok = True
    for f in files:
        if docker_available:
            ok = _run(
                ["docker", "compose", "run", "--rm", "--no-deps", "app", "python", "-m", "py_compile", f],
                cwd=PROJECT_ROOT, label=f
            )
        else:
            ok = _run(
                ["python", "-m", "py_compile", os.path.join("backend", f)],
                cwd=PROJECT_ROOT, label=f
            )
        if ok:
            print(f"  OK: {f}")
        else:
            all_ok = False
    return all_ok


def check_agent(only_changed=False):
    """TypeScript type-check the agent."""
    print("\n--- Agent (TypeScript) ---")

    if only_changed:
        changed = _get_changed_files()
        agent_changed = [f for f in changed if f.startswith("agent/")]
        if not agent_changed:
            print("  No changed agent files.")
            return True

    agent_dir = os.path.join(PROJECT_ROOT, "agent")
    if not os.path.isdir(agent_dir):
        print("  SKIP: agent/ directory not found")
        return True

    ok = _run(["npx", "tsc", "--noEmit"], cwd=agent_dir, label="tsc --noEmit")
    if ok:
        print("  OK: TypeScript")
    return ok


def main():
    force_utf8_streams()
    args = sys.argv[1:]
    only_changed = "--changed" in args
    targets = [a for a in args if not a.startswith("-")]

    run_backend = not targets or "backend" in targets
    run_agent = not targets or "agent" in targets

    results = []
    if run_backend:
        results.append(("Backend", check_backend(only_changed)))
    if run_agent:
        results.append(("Agent", check_agent(only_changed)))

    print("\n--- Summary ---")
    all_ok = True
    for name, ok in results:
        status = "PASS" if ok else "FAIL"
        print(f"  {name}: {status}")
        if not ok:
            all_ok = False

    sys.exit(0 if all_ok else 1)


if __name__ == "__main__":
    main()
