#!/usr/bin/env python3
"""Regenerate backend/.test_durations with real timing data.
Runs uncovered test files in batches, merging results into .test_durations.
"""

import glob
import json
import os
import subprocess
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from durations_quality import MIN_ENTRIES, MIN_UNIQUE, MIN_CV, OUTLIER_CAP, quality_ok

BACKEND = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "backend")
os.chdir(BACKEND)

def _resolve_deps_image():
    """Compute the deps image tag from requirements.txt + Dockerfile hash,
    matching the pattern in batch-store-durations.sh so the tag stays
    current when dependencies change."""
    import hashlib
    req_path = os.path.join(BACKEND, "requirements.txt")
    dockerfile_path = os.path.join(BACKEND, "Dockerfile")
    h = hashlib.sha256()
    for path in (req_path, dockerfile_path):
        with open(path, "rb") as f:
            h.update(f.read())
    tag = h.hexdigest()[:16]
    return f"blueprint-backend-test-deps:{tag}"

DEPS_IMAGE = _resolve_deps_image()
NETWORK = "bn-regen"
PG_NAME = "bp-regen"

ENV_BASE = [
    "-e", f"DATABASE_URL=postgresql+asyncpg://test:test@{PG_NAME}:5432/test_blueprint",
    "-e", "USE_TEST_POSTGRES=true",
    "-e", "GCP_PROJECT_ID=test-project",
    "-e", "LOCAL_MODE=true",
    "-e", "GITHUB_PAT_ENCRYPTION_KEY=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA=",
    "-e", "AGENT_JWT_SECRET=ci-test-secret-min-32-bytes-xxxxx",
    "-e", "WS_TICKET_SECRET=ci-test-ws-secret-min-32-bytes-xx",
]

def docker_run(cmd, timeout=600):
    full_cmd = ["docker", "run", "--rm", "-v", f"{os.getcwd()}:/app",
                 "--network", NETWORK] + ENV_BASE + [DEPS_IMAGE] + cmd
    try:
        r = subprocess.run(full_cmd, capture_output=True, text=True, timeout=timeout)
        return r.returncode, r.stdout, r.stderr
    except subprocess.TimeoutExpired:
        return -1, "", "timeout"

def get_coverage():
    """Return (covered_file_count, total_entry_count, set_of_covered_file_paths)."""
    merged = {}
    for p in sorted(glob.glob(".durations_partial_*.json")):
        with open(p) as f:
            merged.update(json.load(f))

    files = set()
    for k in merged:
        parts = k.split("::")
        if parts:
            files.add(parts[0])

    # Also count what's already in .test_durations if it has real data
    td = ".test_durations"
    if os.path.exists(td):
        with open(td) as f:
            existing = json.load(f)
        # Only count entries with non-placeholder values (>3 unique values)
        unique_vals = len(set(existing.values()))
        if unique_vals > 10:  # Has real data
            for k in existing:
                parts = k.split("::")
                if parts:
                    files.add(parts[0])
            merged.update(existing)

    return len(files), len(merged), files


def write_merged():
    """Merge all partial files into .test_durations, dropping outliers > OUTLIER_CAP."""
    merged = {}
    outlier_count = 0
    for p in sorted(glob.glob(".durations_partial_*.json")):
        with open(p) as f:
            data = json.load(f)
        for k, v in data.items():
            if v > OUTLIER_CAP:
                outlier_count += 1
            else:
                merged[k] = v

    if not merged:
        print("  No partial data to merge")
        return 0

    if outlier_count:
        print(f"  Dropped {outlier_count} outlier(s) > {OUTLIER_CAP}s (fixture-setup misattribution)")

    td = ".test_durations"
    with open(td, "w") as f:
        json.dump(merged, f, indent=2, sort_keys=True)
        f.write("\n")

    files = set()
    for k in merged:
        parts = k.split("::")
        if parts:
            files.add(parts[0])

    print(f"  Merged {len(merged)} entries across {len(files)} files -> {td}")
    return len(merged)

def main():
    # Start Postgres + run migrations if needed
    subprocess.run(["docker", "network", "create", NETWORK], capture_output=True)
    subprocess.run(["docker", "rm", "-f", PG_NAME], capture_output=True)
    subprocess.run([
        "docker", "run", "-d", "--rm", "--name", PG_NAME, "--network", NETWORK,
        "-e", "POSTGRES_USER=test", "-e", "POSTGRES_PASSWORD=test",
        "-e", "POSTGRES_DB=test_blueprint", "postgres:15"
    ], capture_output=True)

    # Wait for Postgres
    for _ in range(30):
        r = subprocess.run(["docker", "exec", PG_NAME, "pg_isready", "-U", "test"],
                          capture_output=True)
        if r.returncode == 0:
            break
        time.sleep(0.5)

    # Run migrations
    print("Running migrations...")
    docker_run(["python3", "-m", "alembic", "upgrade", "head"], timeout=120)
    docker_run(["python3", "-c", "from app.main import app; print('OK')"], timeout=30)

    # Get all test files
    all_files = sorted(glob.glob("testing/test_*.py"))
    total_files = len(all_files)
    print(f"Total test files: {total_files}")

    batch_num = 0
    start_time = time.time()

    # Relative guard targets come from durations_quality (MIN_ENTRIES/MIN_UNIQUE/MIN_CV);
    # absolute file-coverage target: cover every known test_*.py file.
    while True:
        covered_count, entry_count, covered_set = get_coverage()
        elapsed = time.time() - start_time

        print(f"\n[{elapsed:.0f}s] Files: {covered_count}/{total_files}, Entries: {entry_count}/{MIN_ENTRIES}")

        # Write a temporary merge and check quality guards
        temp_merged = {}
        for p in sorted(glob.glob(".durations_partial_*.json")):
            with open(p) as f:
                temp_merged.update(json.load(f))
        td = ".test_durations"
        if os.path.exists(td):
            with open(td) as f:
                existing = json.load(f)
            # Existing data takes lower priority than partial results
            for k, v in existing.items():
                temp_merged.setdefault(k, v)

        import statistics
        vs = list(temp_merged.values()) if temp_merged else [0]
        n, uv = len(vs), len(set(vs))
        cv = statistics.stdev(vs) / statistics.mean(vs) if statistics.mean(vs) > 0 else 0

        ok = covered_count >= total_files and quality_ok(entry_count, uv, cv)
        if ok:
            print(f"✓ All targets met! (n={n}, uv={uv}, CV={cv:.2f})")
            break

        if elapsed > 5400:  # 90 min safety limit
            print("✗ Time limit reached")
            break

        # Find uncovered files
        uncovered = [f for f in all_files if f not in covered_set]
        if not uncovered:
            print("All files covered but entry count may be low")
            break

        # Pick a batch of 30 uncovered files with xdist
        batch_size = min(30, len(uncovered))
        batch = uncovered[:batch_size]
        batch_num += 1

        print(f"  Batch {batch_num}: {batch_size} files ({len(uncovered)} remaining)")
        print(f"  Files: {' '.join(batch[:5])}...")

        # Install xdist and run with -n 2
        rc, stdout, stderr = docker_run([
            "bash", "-c",
            f"pip install pytest-xdist -q 2>/dev/null && "
            f"python3 -m pytest {' '.join(batch)} "
            f"--store-durations --durations-path /app/.durations_partial_b{batch_num}.json "
            f"-o timeout=300 -n 2 -q --tb=line"
        ], timeout=600)

        # Check if file was created
        src = f".durations_partial_b{batch_num}.json"
        if os.path.exists(src):
            with open(src) as f:
                d = json.load(f)
            files_in_batch = set()
            for k in d:
                p = k.split("::")[0]
                if p:
                    files_in_batch.add(p)
            print(f"    -> {len(d)} entries across {len(files_in_batch)} files")

            # Merge after every 3 batches
            if batch_num % 3 == 0:
                write_merged()
        else:
            print(f"    -> No durations file produced (tests may have all failed)")
            # Remove these files from consideration by writing an empty partial
            with open(src, "w") as f:
                json.dump({}, f)

    # Final merge
    write_merged()

    # Cleanup
    subprocess.run(["docker", "rm", "-f", PG_NAME], capture_output=True)
    subprocess.run(["docker", "network", "rm", NETWORK], capture_output=True)

    print("\n=== Complete ===")
    # Load final merged data for quality guard check
    with open(".test_durations") as f:
        final_data = json.load(f)
    import statistics
    vs = list(final_data.values())
    n, uv = len(vs), len(set(vs))
    cv = statistics.stdev(vs) / statistics.mean(vs) if statistics.mean(vs) > 0 else 0
    print(f"Final: {n} entries, {uv} unique values, CV={cv:.2f}")

    # Relative guards come from durations_quality (shared with batch-store-durations.sh).
    ok = quality_ok(n, uv, cv)
    print(f"Guard: {'PASS' if ok else 'FAIL'} (needs n>={MIN_ENTRIES}, uv>={MIN_UNIQUE}, CV>{MIN_CV})")
    return 0 if ok else 1

if __name__ == "__main__":
    sys.exit(main())
