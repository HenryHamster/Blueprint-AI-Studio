# Running a compute experiment on your own hardware

`blueprint compute run` executes one experiment **on the machine you are sitting
at**. It does not talk to the orchestrator, it does not need a Blueprint API key,
and it does not provision anything: it builds the environment you describe, runs
your command inside it, reads back the numbers your command wrote, evaluates your
success criterion, and records what actually ran.

It is the fastest path from "I have a hypothesis" to "I have a number", and it
uses the same file contract as the cloud backends and the self-hosted runner — so
a spec that works here works there.

---

## 1. The file contract

Your command does not print its results. It **writes** them, to paths the harness
puts in the environment:

| Variable | Set to | What you do with it |
| --- | --- | --- |
| `BLUEPRINT_RESULT_FILE` | A path to a JSON file that does not exist yet | Write a flat JSON object of metric name to number. This is the only thing read back as a measurement. |
| `BLUEPRINT_ARTIFACT_DIR` | A directory that already exists | Write plots, checkpoints, logs, anything you want to keep. It survives the run. |
| `BLUEPRINT_LOCAL_GPU` | A JSON description of the GPUs found on this host, or absent | Read it if your command needs to know what hardware it got. |

A minimal command:

```bash
python -c "import os, json; open(os.environ['BLUEPRINT_RESULT_FILE'],'w').write(json.dumps({'accuracy': 0.94}))"
```

Three rules the harness enforces, because the failure they prevent is an
experiment that silently measured nothing and reported success:

- If you declare a metric and the result file never appears, or appears without
  that metric, the run is **FAILED**, not SUCCEEDED.
- If the contract variables do not reach your shell, the run aborts instead of
  running your command against an empty environment.
- A malformed result file is a harness failure, not a zero.

## 2. Exit codes

`blueprint compute run` distinguishes *the experiment answered no* from *the
experiment never answered*, so scripting against it is safe:

| Exit | Meaning |
| --- | --- |
| `0` | The run ran, measured what it declared, and the criterion was MET (or none was given). |
| `1` | The experiment command itself exited non-zero. Your code failed; the harness worked. |
| `2` | Harness failure: nothing was measured. A bad spec, an unusable image, a missing or malformed result file, a contract that never reached the shell. |
| `3` | Measured cleanly, and the criterion evaluated NOT MET. This is a real experimental answer, not an error. |

Exit `3` is the one people get wrong: a negative result is a result.

## 3. The simplest possible run

```bash
blueprint compute run "python -c \"import os,json;open(os.environ['BLUEPRINT_RESULT_FILE'],'w').write(json.dumps({'accuracy':0.94}))\"" \
  --metric accuracy --criterion "accuracy >= 0.9"
```

Everything past this point is the same command driven from a spec file, which is
what you want as soon as the command stops fitting on one line.

## 4. Spec files

```bash
blueprint compute run --local experiment.json
```

```json
{
  "command": "python train.py --epochs 3",
  "setup_step": "pip install -r requirements.txt",
  "declared_metric": "accuracy",
  "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.93},
  "container_image": "my-lab/trainer:local",
  "repo_clone": {"owner": "acme", "repo": "trainer", "ref": "main"},
  "env": {"HF_HOME": "/workspace/.hf"},
  "cwd": "/workspace"
}
```

| Field | Effect |
| --- | --- |
| `command` | The experiment. Required unless you use `arms`. |
| `arms` | A list of `{"name", "command", "setup_step"}` objects — a sweep. Use instead of `command`. |
| `declared_metric` | The metric that must come back. Its absence makes the run FAILED. |
| `success_criterion` | `{"metric", "operator", "threshold"}`. Decides exit `0` vs exit `3`. |
| `setup_step` | Shell that runs **before** `command`, in the same shell and the same container. See below. |
| `container_image` | Run inside this image instead of on the host shell. |
| `container_registry_auth` | `{"registry", "username", "password"}`, used only when a pull is actually needed. |
| `repo_clone` | Mount this checkout at `/workspace` inside the container. See below. |
| `env` | Extra environment variables for the command. |
| `cwd` | Working directory for the command. |

**Nothing else is dropped in silence.** A field this path does not act on is
either refused by name — when honouring it would change what gets measured, for
example `success_criteria` or `input_artifact_job_id` — or reported by name on
stderr and recorded under `spec_warnings` in the JSON result, for example
`machine_type`, which configures a cloud launch that a local run does not do.
Paste a cloud payload in here and you will be told exactly which parts of it had
no effect.

## 5. `setup_step`

`setup_step` runs before the command **in the same shell**, folded together as:

```
{ <setup_step> } && { <command> }
```

Two consequences worth knowing:

- An `export` or a `cd` in the setup step is visible to the command. That is the
  point: it is how you install dependencies, fetch a dataset, or set a variable
  the command needs.
- If the setup step fails, the command never runs and the run reports the setup
  step's exit status. A broken environment cannot masquerade as a failed
  experiment.

With `arms`, the spec-level `setup_step` runs for **every** arm, before that
arm's own `setup_step` if it has one.

## 6. Containers, and the image you just built

```json
{"container_image": "my-lab/trainer:local", "command": "python train.py"}
```

The container is started with your result and artifact directories bind-mounted,
so the file contract works unchanged inside it.

### `--pull`: use the image you built

An image you built on this machine exists in no registry, so it can never be
pulled. `--pull` controls when the harness reaches for the network:

| `--pull` | Behaviour |
| --- | --- |
| `missing` (default) | Run the image already on this host; pull only if there is none. **This is what makes a locally built image usable.** |
| `always` | Pull first, to refresh a mutable tag. If the pull fails but the image is here, the local copy runs and the failure is recorded, not fatal. |
| `never` | Never reach the network. If the image is not on this host, fail. |

No usable image at all is still a hard failure (exit `2`). This narrows *when* a
pull failure is fatal; it does not make a missing image survivable.

### `repo_clone`: getting your code into the container

A container image holds your *environment*. `repo_clone` is how your *code* gets
in. On a local run it does not clone `owner/repo@ref` the way the cloud backend
does — it **bind-mounts this checkout**:

- Mounted at `/workspace`, which becomes the working directory.
- `PYTHONPATH=/workspace`, so `import your_package` works.
- The mount is the **repository root**, found by asking git — not whatever
  directory you happened to run the command from.
- The commit that was mounted is recorded. If the tree is dirty, or the ref you
  declared is not the ref that is checked out, you are told so, because the
  recorded commit no longer fully describes what ran.

## 7. Sweeps

```json
{
  "arms": [
    {"name": "lr-1e-3", "command": "python train.py --lr 1e-3"},
    {"name": "lr-3e-4", "command": "python train.py --lr 3e-4"}
  ],
  "setup_step": "pip install -r requirements.txt",
  "declared_metric": "accuracy",
  "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.93}
}
```

Each arm gets its own result file and its own subdirectory of
`BLUEPRINT_ARTIFACT_DIR`. The criterion is evaluated against the best arm, and
every arm's metrics are reported.

## 8. What gets recorded

Every run writes `blueprint_environment.json` into the artifact directory, and
the same block appears under `environment` in `--json` output:

- The resolved **image digest** that actually ran. `container_image_digest_pinned`
  in the cloud payload is a claim about the *reference* and fails open to a
  mutable tag; this is the digest docker resolved.
- The **git commit** of the mounted tree, and whether it was dirty.
- The **package set**, frozen with `pip freeze` *inside* the environment that ran
  — after `setup_step`, inside the container — written to
  `blueprint_python_packages.txt`.
- The GPU that was detected, the shell that was used, the `setup_step`, and the
  **names** of any `env` overrides. Names only: overrides routinely carry tokens,
  and this file is written to disk and printed.

That is deliberately enough to re-create the run from what was written down.

## 9. End to end, copy-pasteable

This builds an image that exists in no registry, runs an experiment in it whose
answer depends on both the image and the setup step, and checks a criterion.

```bash
# 1. An environment, built here and pushed nowhere.
printf 'FROM python:3.11-slim\nRUN printf "0.9312" > /opt/bp_baseline\n' \
  | docker build -t blueprint-selftest:local -

# 2. An experiment that can only answer correctly if BOTH the image and the
#    setup step reached the container.
python - <<'PY'
import json
spec = {
    "container_image": "blueprint-selftest:local",
    "setup_step": "printf '0.0088' > /tmp/bp_delta",
    "command": (
        "python3 -c \"import os,json;"
        "b=float(open('/opt/bp_baseline').read());"
        "d=float(open('/tmp/bp_delta').read());"
        "open(os.environ['BLUEPRINT_RESULT_FILE'],'w')"
        ".write(json.dumps({'accuracy': round(b+d,4)}))\""
    ),
    "declared_metric": "accuracy",
    "success_criterion": {"metric": "accuracy", "operator": ">=", "threshold": 0.93},
}
open("/tmp/experiment.json", "w").write(json.dumps(spec, indent=2))
PY

# 3. Run it.
blueprint compute run --local /tmp/experiment.json
echo "exit=$?"
```

Expected:

```
image       : blueprint-selftest:local
docker pull : skipped (image already present on this host)
digest      : sha256:...  (source: local)
setup_step  : printf '0.0088' > /tmp/bp_delta
status      : SUCCEEDED (exit_code=0)
metrics     : {'accuracy': 0.94}
verdict     : MET  (accuracy=0.94 >= 0.93)
packages    : N python packages recorded
environment : /tmp/blueprint-compute-run-.../artifacts/blueprint_environment.json
exit=0
```

`0.9312` came from the image layer and `0.0088` from the setup step. Getting
`0.94` back is the proof that both halves reached the container.

## 10. Troubleshooting

| Symptom | Cause |
| --- | --- |
| Exit `2`, `no local copy of the image exists` | The tag is neither on this host nor pullable. Build it, or fix the reference. |
| Exit `2`, `did not reach the experiment shell` | The contract was not delivered — usually a shell that could not be launched. The run aborts rather than measuring nothing. |
| Exit `2`, declared metric missing | Your command exited 0 without writing `BLUEPRINT_RESULT_FILE`, or wrote a different key. |
| Exit `1` immediately, no metrics | Your `setup_step` failed, so the command never ran. Read the stderr excerpt. |
| `warning: spec field '...' configures a CLOUD launch` | You pasted a cloud payload. That field had no effect here; everything else did. |
| `warning: repo_clone names .../...` | A reminder that the local path mounts this checkout instead of cloning the ref you named. |

## 11. Related

- [PAYLOADS_AND_SCHEMAS.md](PAYLOADS_AND_SCHEMAS.md) — the cloud `compute_experiment`
  payload that this spec is a local subset of.
- [RUNNER_RUNBOOK.md](RUNNER_RUNBOOK.md) — running the same experiments through a
  self-hosted runner instead of by hand.
