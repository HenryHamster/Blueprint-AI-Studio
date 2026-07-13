"""Build a compute-job VM heartbeat request body as JSON on stdout.

Invoked by the GCP startup script's heartbeat loop each interval. Reads two
optional on-VM files and merges whatever is present into a single JSON object
that the orchestrator's ``/api/agent/compute-jobs/<id>/heartbeat`` endpoint
understands:

  argv[1]  live_tail_path  -- a file holding a base64-encoded JSON live-output
                              tail (already in the wire shape the orchestrator's
                              _parse_live_output_tail expects). Its contents are
                              copied verbatim into the body's "live_output_tail".
  argv[2]  progress_path    -- the job's BLUEPRINT_PROGRESS_FILE (JSONL, one
                              ``{"metric","value",...}`` record per line, written
                              by the experiment). The LAST syntactically-valid
                              line carrying a non-empty string "metric" and a
                              finite numeric "value" becomes
                              ``{"progress": {"metric", "value"}}`` -- the sample
                              the server appends to progress_series so the
                              no-progress watchdog can tell a live job from a
                              dead one.

Both inputs are optional and untrusted: any read/parse error, missing file, or
malformed field is silently skipped so the heartbeat still fires (an empty
object ``{}`` is a valid bare liveness ping). This script must NEVER raise -- a
crash here would silence the liveness signal and get a live job reaped.
"""

import json
import sys

# Only the tail of the progress file is inspected: a multi-day training run can
# append one record per step, so reading the whole file each heartbeat is
# unbounded. The last valid line is all the watchdog needs, and it lives at the
# end. Seeking into the middle of a line only costs us that one partial (invalid,
# skipped) record.
_PROGRESS_TAIL_BYTES = 65536
_METRIC_MAX_CHARS = 128


def _read_live_tail(path):
    try:
        with open(path, "r", encoding="utf-8") as f:
            val = f.read().strip()
    except OSError:
        return None
    return val or None


def _read_progress(path):
    """Return (metric, value) from the last valid JSONL record, or None."""
    try:
        with open(path, "rb") as f:
            f.seek(0, 2)
            size = f.tell()
            f.seek(max(0, size - _PROGRESS_TAIL_BYTES))
            data = f.read()
    except OSError:
        return None
    metric = None
    value = None
    for raw in data.splitlines():
        line = raw.strip()
        if not line:
            continue
        try:
            rec = json.loads(line.decode("utf-8", "replace"))
        except (ValueError, TypeError):
            continue
        if not isinstance(rec, dict):
            continue
        m = rec.get("metric")
        v = rec.get("value")
        if not isinstance(m, str) or not m.strip():
            continue
        # bool is a subclass of int -- reject it explicitly before the numeric test.
        if isinstance(v, bool) or not isinstance(v, (int, float)):
            continue
        fv = float(v)
        # Drop NaN / +-inf: the server would reject them anyway, and a garbled
        # sample must never be the one that keeps the watchdog quiet.
        if fv != fv or fv in (float("inf"), float("-inf")):
            continue
        metric = m.strip()[:_METRIC_MAX_CHARS]
        value = fv
    if metric is None:
        return None
    return metric, value


def build_body(argv):
    body = {}
    if len(argv) > 1 and argv[1]:
        tail = _read_live_tail(argv[1])
        if tail is not None:
            body["live_output_tail"] = tail
    if len(argv) > 2 and argv[2]:
        prog = _read_progress(argv[2])
        if prog is not None:
            body["progress"] = {"metric": prog[0], "value": prog[1]}
    return body


def main(argv):
    try:
        body = build_body(argv)
    except Exception:  # never raise -- a silent heartbeat is better than none
        body = {}
    sys.stdout.write(json.dumps(body))


if __name__ == "__main__":
    main(sys.argv)
