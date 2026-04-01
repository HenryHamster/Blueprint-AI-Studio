"""Spawn review tasks for all open PRs targeting a given base branch.

Usage:
    python scripts/spawn_reviews.py <base_branch>          # dry run
    python scripts/spawn_reviews.py <base_branch> --go     # execute

Example:
    python scripts/spawn_reviews.py research/494637c9 --go
"""
import sys
from blueprint_api import load_config, api, list_tasks, create_task, patch_task

if len(sys.argv) < 2:
    print(__doc__)
    sys.exit(1)

base_branch = sys.argv[1]
dry_run = "--go" not in sys.argv

print(f"Base branch: {base_branch}")
print(f"{'DRY RUN' if dry_run else 'EXECUTING'}\n")

# Find parent task from base branch name (research/<task_id_prefix>)
parent_prefix = base_branch.split("/")[-1] if "/" in base_branch else ""
parent_id = None
if parent_prefix:
    for st in ["EVALUATING", "CANCELLED", "COMPLETED", "FAILED"]:
        for t in list_tasks(status=st, limit=50):
            if t["id"].startswith(parent_prefix):
                parent_id = t["id"]
                print(f"Parent: {parent_id[:8]} [{st}] {t['title'][:50]}")
                break
        if parent_id:
            break

if not parent_id:
    print(f"WARNING: No parent task found for prefix '{parent_prefix}'")

# Collect completed tasks to find original create task IDs
all_completed = []
for offset in range(0, 200, 50):
    batch = list_tasks(status="COMPLETED", limit=50, offset=offset)
    all_completed.extend(batch)
    if len(batch) < 50:
        break

# Build map: branch name -> create task ID
branch_to_create = {}
for t in all_completed:
    if t.get("task_type") == "create" and parent_prefix and (t.get("parent_task_id") or "").startswith(parent_prefix):
        task_branch = f"blueprint/task-{t['id'][:8]}"
        branch_to_create[task_branch] = t["id"]

# Find PR info from existing review/fix tasks
pr_branches = {}
all_sources = all_completed[:]
for st in ["EVALUATING", "FAILED", "CANCELLED"]:
    all_sources.extend(list_tasks(status=st, limit=50))

for t in all_sources:
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
    for t in list_tasks(status=st, limit=50):
        pr = (t.get("payloads") or [{}])[0].get("pr_number")
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
        resp = create_task(
            title=f"Review PR #{pr_num}",
            prompt=f"Review PR #{pr_num}",
            task_type="review",
            base_branch=base_branch,
            pr_number=pr_num,
            branch=branch,
        )
        task_id = resp["id"]
        patch_task(task_id, {
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
