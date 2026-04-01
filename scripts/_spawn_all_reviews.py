"""Spawn review tasks for all open PRs targeting a given base branch.

Usage:
    python scripts/_spawn_all_reviews.py <base_branch>          # dry run
    python scripts/_spawn_all_reviews.py <base_branch> --go     # execute

Example:
    python scripts/_spawn_all_reviews.py research/494637c9 --go
"""
import json, sys, urllib.request
from pathlib import Path

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

base_branch = sys.argv[1]
dry_run = "--go" not in sys.argv

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
base = config["api_url"].rstrip("/")
headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}


def api_get(path):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(base + path, headers=headers), timeout=30
    ).read())


def api_post(path, body):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(base + path, data=json.dumps(body).encode(), method="POST", headers=headers), timeout=15
    ).read())


def api_patch(path, body):
    return json.loads(urllib.request.urlopen(
        urllib.request.Request(base + path, data=json.dumps(body).encode(), method="PATCH", headers=headers), timeout=15
    ).read())


# Discover open PRs targeting this base branch via GitHub
repo = config.get("github_repo", "")
if not repo:
    # Fall back: find from any project's tasks
    for st in ["EVALUATING", "COMPLETED"]:
        tasks = api_get(f"/api/admin/tasks?limit=1&status={st}").get("tasks", [])
        if tasks:
            # Try to find repo from task context
            break

# Fetch open PRs via admin API's GitHub integration — or use gh CLI output
# We'll use the task data to find PR info instead
print(f"Base branch: {base_branch}")
print(f"{'DRY RUN' if dry_run else 'EXECUTING'}\n")

# Find parent task from base branch name (research/<task_id_prefix>)
parent_prefix = base_branch.split("/")[-1] if "/" in base_branch else ""
parent_id = None
if parent_prefix:
    for st in ["EVALUATING", "CANCELLED", "COMPLETED", "FAILED"]:
        tasks = api_get(f"/api/admin/tasks?limit=50&status={st}").get("tasks", [])
        for t in tasks:
            if t["id"].startswith(parent_prefix):
                parent_id = t["id"]
                print(f"Parent: {parent_id[:8]} [{st}] {t['title'][:50]}")
                break
        if parent_id:
            break

if not parent_id:
    print(f"WARNING: No parent task found for prefix '{parent_prefix}'")

# Collect all completed tasks to find original create task IDs
all_completed = []
for offset in range(0, 200, 50):
    batch = api_get(f"/api/admin/tasks?limit=50&offset={offset}&status=COMPLETED").get("tasks", [])
    all_completed.extend(batch)
    if len(batch) < 50:
        break

# Build map: branch name -> create task ID
branch_to_create = {}
for t in all_completed:
    if t.get("task_type") == "create" and (t.get("parent_task_id") or "").startswith(parent_prefix):
        vm = t.get("vm_instance_id", "")
        # Branch is blueprint/task-<task_id_prefix>
        task_branch = f"blueprint/task-{t['id'][:8]}"
        branch_to_create[task_branch] = t["id"]

# Find open PRs by checking which branches have open PRs
# Read from stdin or discover from existing review/fix tasks
pr_branches = {}
for t in all_completed:
    p = (t.get("payloads") or [{}])[0]
    pr = p.get("pr_number")
    branch = p.get("branch", "") or (t.get("context") or {}).get("review_loop", {}).get("branch", "")
    pr_base = p.get("base_branch", "") or (t.get("context") or {}).get("review_loop", {}).get("base_branch", "")
    if pr and branch and pr_base == base_branch and pr not in pr_branches:
        pr_branches[pr] = branch

# Also check non-completed tasks
for st in ["EVALUATING", "FAILED", "CANCELLED"]:
    tasks = api_get(f"/api/admin/tasks?limit=50&status={st}").get("tasks", [])
    for t in tasks:
        p = (t.get("payloads") or [{}])[0]
        pr = p.get("pr_number")
        branch = p.get("branch", "") or (t.get("context") or {}).get("review_loop", {}).get("branch", "")
        pr_base = p.get("base_branch", "") or (t.get("context") or {}).get("review_loop", {}).get("base_branch", "")
        if pr and branch and pr_base == base_branch and pr not in pr_branches:
            pr_branches[pr] = branch

if not pr_branches:
    print("No PRs found targeting this base branch.")
    sys.exit(0)

# Check which PRs already have active tasks
active_prs = set()
for st in ["QUEUED", "PROVISIONING", "IN_PROGRESS", "EVALUATING"]:
    tasks = api_get(f"/api/admin/tasks?limit=50&status={st}").get("tasks", [])
    for t in tasks:
        p = (t.get("payloads") or [{}])[0]
        pr = p.get("pr_number")
        if pr:
            active_prs.add(pr)

print(f"\nFound {len(pr_branches)} PRs targeting {base_branch}:")
to_spawn = []
for pr_num in sorted(pr_branches):
    branch = pr_branches[pr_num]
    orig_id = branch_to_create.get(branch, "")
    has_active = pr_num in active_prs
    status = "SKIP (active task exists)" if has_active else "WILL SPAWN"
    print(f"  PR #{pr_num:<4} {branch:<35} original={orig_id[:8] if orig_id else '?':<8}  {status}")
    if not has_active:
        to_spawn.append({"pr": pr_num, "branch": branch, "original_task_id": orig_id})

if dry_run:
    print(f"\nDry run complete. {len(to_spawn)} reviews to spawn. Re-run with --go to execute.")
    sys.exit(0)

print(f"\nSpawning {len(to_spawn)} review tasks...")
for item in to_spawn:
    pr_num = item["pr"]
    branch = item["branch"]
    orig_id = item["original_task_id"]
    print(f"  PR #{pr_num}...", end=" ")
    try:
        resp = api_post("/api/admin/tasks", {
            "title": f"Review PR #{pr_num}",
            "prompt": f"Review PR #{pr_num}",
            "task_type": "review",
            "github_repo": "HenryHamster/BlueprintAIStudio",
            "base_branch": base_branch,
            "pr_number": pr_num,
            "branch": branch,
        })
        task_id = resp["id"]
        api_patch(f"/api/admin/tasks/{task_id}", {
            "context": {"review_loop": {
                "pr_number": pr_num,
                "branch": branch,
                "base_branch": base_branch,
                "original_task_id": orig_id,
                "iteration": 1,
            }},
            "parent_task_id": parent_id,
        })
        print(f"created {task_id[:8]}")
    except Exception as e:
        print(f"FAILED: {e}")
