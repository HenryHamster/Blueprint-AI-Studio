# Golden-Task Suite

A replayable task-specification skeleton for validating cost-reduction levers
safely, without risking production quality.

## What This Is

Each **golden-task spec** is a JSON file in `specs/` that describes one task
drawn from real historical patterns. The suite spans **18 specs** across 12+
task types:

| Task Type       | Specs                          | Effort    |
|-----------------|--------------------------------|-----------|
| `create`        | golden-001                     | low       |
| `fix`           | golden-002                     | low       |
| `refactor`      | golden-003                     | medium    |
| `review`        | golden-004                     | medium    |
| `research`      | golden-005                     | medium    |
| `strategic`     | golden-006                     | high      |
| `investigate`   | golden-007                     | medium    |
| `debug`         | golden-008                     | medium    |
| `docs`          | golden-009                     | low       |
| `plan`          | golden-010                     | medium    |
| `migrate`       | golden-011                     | low       |
| `qa`            | golden-012                     | medium    |
| `deps`          | golden-013                     | medium    |
| `memo`          | golden-014                     | high      |
| `perf`          | golden-015                     | medium    |
| `web_research`  | golden-016                     | medium    |
| `security_audit`| golden-017                     | high      |
| `self_audit`    | golden-018                     | medium    |

Every spec has a **machine-checkable expected outcome** (terminal status,
outcome kind, outcome status) so different model / provider / prompt variants
can be compared on:

- **Token count** (prompt + completion)
- **Cost** (USD per spec, derived from token counts × provider rate)
- **Wall time** (end-to-end replay duration)
- **Success rate** (outcome matches expected)
- **Provider/model variant** (run the same spec against multiple LLMs)

## Directory Layout

```
golden_tasks/
  README.md              # This file
  __init__.py            # Package marker
  runner.py              # CLI runner (validate, list, run)
  specs/                 # Golden-task spec JSON files
    golden-001-create-health-check.json
    golden-002-fix-null-pointer.json
    ...
    golden-018-self-audit-maturity.json
```

The end‑to‑end validation test lives at `backend/testing/test_golden_suite.py`
so it is discovered by the project's standard pytest runner.

## Usage

### CLI Runner

```bash
# List available specs
python3 golden_tasks/runner.py --list-specs

# Validate all specs
python3 golden_tasks/runner.py --validate --all

# Validate a single spec
python3 golden_tasks/runner.py --validate golden_tasks/specs/golden-001-create-health-check.json

# Run all specs (skeleton — simulates metrics)
python3 golden_tasks/runner.py --run --all

# Run all specs with a specific model override and write a report
python3 golden_tasks/runner.py --run --all --model claude-opus-4-20251022 --report /tmp/report.json
```

### Pytest (via `./check test`)

```bash
# Run the golden-suite validation test (standalone, no DB needed)
cd backend && pytest testing/test_golden_suite.py -v

# Or through the project's test harness
bash scripts/test-backend.sh testing/test_golden_suite.py
```

## Spec Format

```json
{
  "spec_id": "golden-001",
  "name": "Human-readable name",
  "description": "Longer description of what the task does",
  "task_type": "create",
  "prompt": "The instruction given to the agent",
  "effort": "low",
  "source_info": "Where this pattern came from",
  "expected": {
    "terminal_status": "COMPLETED",
    "outcome_status": "success",
    "outcome_kind": "code_merge"
  },
  "tags": ["create", "simple", "cost-baseline"]
}
```

### Fields

| Field              | Required | Description                                      |
|--------------------|----------|--------------------------------------------------|
| `spec_id`          | ✓        | Unique identifier (`golden-NNN`)                 |
| `name`             | ✓        | Short human-readable title                       |
| `description`      | ✓        | Longer description                                |
| `task_type`        | ✓        | One of the 28 valid task types                   |
| `prompt`           | ✓        | The agent instruction (1+ chars)                 |
| `effort`           | —        | `low`, `medium`, `high`, or `max`                |
| `source_info`      | —        | Where this pattern came from (PR, analysis, etc) |
| `expected`         | ✓        | Expected outcome block (see below)               |
| `tags`             | —        | String tags for filtering                         |

### Expected Block

| Field             | Required | Description                                  |
|-------------------|----------|----------------------------------------------|
| `terminal_status` | ✓        | `COMPLETED`, `FAILED`, or `CANCELLED`        |
| `outcome_status`  | ✓        | `success`, `neutral`, or `failure`           |
| `outcome_kind`    | ✓        | One of the 10 outcome kinds                  |
| `evidence_checks` | —        | Optional list of checkable outcome criteria  |

## Adding a New Spec

1. Pick the next available `spec_id` (e.g. `golden-019`).
2. Choose a task type from the valid set.
3. Write the prompt as a realistic agent instruction.
4. Set the expected outcome to match the task type's canonical outcome.
5. Save the JSON to `golden_tasks/specs/`.
6. Run `python3 golden_tasks/runner.py --validate --all` to confirm.
7. Run `pytest backend/testing/test_golden_suite.py` to confirm the test suite passes.

## Using the Suite for Cost-Lever Validation

The intended workflow:

1. **Baseline**: run all specs against the current production model/config.
   Record token counts, wall time, cost, and success rate.
2. **Apply lever**: change one variable (model, prompt template, reviewer fanout,
   effort level).
3. **Compare**: re-run the same specs. Compare metrics to the baseline.
4. **Validate**: confirm that success rate does not regress. A cost reduction
   that drops success rate below the baseline is not a valid improvement.

The `--report` flag writes a structured JSON report suitable for automated
comparison (e.g., in CI or a notebook).
