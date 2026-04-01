"""Debug CI checking - replicate exactly what the orchestrator does."""
import json, urllib.request
from pathlib import Path

config = json.loads(Path(__file__).resolve().parent.parent.joinpath(".blueprint-config.json").read_text())
base = config["api_url"].rstrip("/")
headers = {"Authorization": f"Bearer {config['api_key']}", "Content-Type": "application/json"}

# Get CI-waiting tasks
url = base + "/api/admin/tasks?limit=50&status=EVALUATING"
req = urllib.request.Request(url, headers=headers)
tasks = json.loads(urllib.request.urlopen(req, timeout=30).read()).get("tasks", [])

for t in tasks:
    ctx = t.get("context") or {}
    loop = ctx.get("review_loop", {})
    if not loop.get("ci_waiting"):
        tid = t["id"][:8]
        print(f"{tid}  NOT ci_waiting  {t['title'][:50]}  msg={t.get('latest_status_message','')[:80]}")
        continue

    pr_num = loop.get("pr_number")
    tid = t["id"][:8]
    msg = t.get("latest_status_message", "")[:80]
    print(f"\n=== {tid} PR #{pr_num} ===")
    print(f"  Status msg: {msg}")
    print(f"  ci_wait_started: {loop.get('ci_wait_started', 'NOT SET')}")

    # Try to get a GitHub token the way the orchestrator would
    try:
        token_url = base + f"/api/agent/github/token?task_id={t['id']}"
        token_req = urllib.request.Request(token_url, headers=headers)
        token_resp = json.loads(urllib.request.urlopen(token_req, timeout=15).read())
        gh_token = token_resp.get("token", "")
        print(f"  GitHub token: {'got it (' + gh_token[:10] + '...)' if gh_token else 'MISSING'}")

        if not gh_token:
            print("  PROBLEM: No GitHub token available!")
            continue

        # Fetch PR to get head SHA
        gh_headers = {
            "Authorization": f"Bearer {gh_token}",
            "Accept": "application/vnd.github.v3+json",
            "X-GitHub-Api-Version": "2022-11-28"
        }
        pr_url = f"https://api.github.com/repos/HenryHamster/BlueprintAIStudio/pulls/{pr_num}"
        pr_req = urllib.request.Request(pr_url, headers=gh_headers)
        pr_data = json.loads(urllib.request.urlopen(pr_req, timeout=15).read())
        head_sha = pr_data.get("head", {}).get("sha", "")
        pr_state = pr_data.get("state", "?")
        print(f"  PR state: {pr_state}  HEAD SHA: {head_sha[:12] if head_sha else 'NONE'}")

        if not head_sha:
            print("  PROBLEM: No head SHA!")
            continue

        # Check runs
        cr_url = f"https://api.github.com/repos/HenryHamster/BlueprintAIStudio/commits/{head_sha}/check-runs"
        cr_req = urllib.request.Request(cr_url, headers=gh_headers)
        cr_data = json.loads(urllib.request.urlopen(cr_req, timeout=15).read())
        check_runs = cr_data.get("check_runs", [])
        print(f"  Check runs ({len(check_runs)}):")
        all_completed = True
        any_failed = False
        for cr in check_runs:
            s = cr.get("status", "?")
            c = cr.get("conclusion", "?")
            print(f"    {cr.get('name', '?'):<25} status={s:<12} conclusion={c}")
            if s != "completed":
                all_completed = False
            if c == "failure":
                any_failed = True

        if all_completed:
            if any_failed:
                print(f"  VERDICT: CI COMPLETED + FAILED — should spawn fix")
            else:
                print(f"  VERDICT: CI COMPLETED + PASSED — should merge")
        else:
            print(f"  VERDICT: CI still running")

    except Exception as e:
        print(f"  ERROR: {e}")
