    # required backend-authoring verification. No skip can mint this receipt.
    # Resolve a usable PostgreSQL server bindir for --sandbox-postgres. Prefers
    # `pg_config --bindir` when it is on PATH and points at a real server
    # (images with libpq-dev / postgresql-client install pg_config on PATH).
    # When pg_config is absent from PATH, falls back to the highest-versioned
    # /usr/lib/postgresql/*/bin that actually holds initdb (an image with only
    # the server packages still has the server binaries there). Prints the
    # bindir and returns 0; returns 1 when neither exists — a pg_config that
    # exists but points at a non-server dir stays fail-closed rather than
    # scanning. Keep this helper byte-identical across check,
    # scripts/test-backend.sh and this fragment — mirrored paths must not
    # diverge again.
    resolve_pg_bindir() {
      local bindir cand
      if command -v pg_config >/dev/null 2>&1; then
        bindir="$(pg_config --bindir 2>/dev/null || true)"
        if [ -n "$bindir" ] && [ -x "$bindir/initdb" ]; then
          printf '%s\n' "$bindir"
          return 0
        fi
        return 1
      fi
      for cand in $(ls -d /usr/lib/postgresql/*/bin 2>/dev/null | sort -V | tail -n 1); do
        if [ -x "$cand/initdb" ]; then
          printf '%s\n' "$cand"
          return 0
        fi
      done
      return 1
    }
    _VERIFICATION_RECEIPT=/tmp/project_verification_receipt_@TASK_ID@.json
    _VERIFICATION_DIR='@DIR@'
    _VERIFICATION_HARNESS='@HARNESS@'
    _VERIFICATION_CHANGED=$(mktemp /tmp/check_verification_changed.XXXXXX)
    : >"$_VERIFICATION_CHANGED"
    printf '%s\n' "$CHANGED" | grep -E '@PYTHON_PATH_RE@' | while IFS= read -r f; do
      [ -f "$f" ] && printf '%s\0' "$f" >>"$_VERIFICATION_CHANGED"
    done
    _VERIFICATION_RUFF_OK=0
    if [ ! -s "$_VERIFICATION_CHANGED" ]; then
      echo "[check] required backend verification FAILED — no changed Python file in $_VERIFICATION_DIR"
      FAILED+=("$_VERIFICATION_DIR verification: no Python target")
    else
      echo "[check] $_VERIFICATION_DIR required ruff..."
      if xargs -0 -a "$_VERIFICATION_CHANGED" python3 -m ruff check --select E,F,W --ignore E501 2>&1; then
        echo "[check] $_VERIFICATION_DIR required ruff OK"
        _VERIFICATION_RUFF_OK=1
      else
        echo "[check] $_VERIFICATION_DIR required ruff FAILED"
        FAILED+=("$_VERIFICATION_DIR required ruff")
      fi
    fi
    _VERIFICATION_SELECTORS=""
    if [ "$_VERIFICATION_RUFF_OK" = "1" ]; then
      _verification_stderr=$(mktemp /tmp/check_verification_resolver.XXXXXX)
      _verification_candidates=$(mktemp /tmp/check_verification_selectors.XXXXXX)
      : >"$_verification_candidates"
      printf '%s\n' "$CHANGED" | grep -E '@TEST_PATH_RE@' | while IFS= read -r test_path; do
        [ -f "${test_path%%::*}" ] && printf '%s\n' "$test_path"
      done >>"$_verification_candidates" || true
      if [ "$_VERIFICATION_DIR" = "backend" ] && [ -f backend/testing/impacted_tests.py ]; then
        printf '%s\n' "$CHANGED" | python3 backend/testing/impacted_tests.py \
          >>"$_verification_candidates" 2>"$_verification_stderr"
        _verification_resolver_rc=$?
      else
        _verification_resolver_rc=0
        while IFS= read -r changed_file; do
          [ -n "$changed_file" ] || continue
          case "$changed_file" in
            */test_*.py|*/tests.py) printf '%s\n' "${changed_file#$_VERIFICATION_DIR/}" ;;
            *.py)
              _verification_base=$(basename "$changed_file" .py)
              for candidate in \
                "$_VERIFICATION_DIR/testing/test_${_verification_base}.py" \
                "$_VERIFICATION_DIR/tests/test_${_verification_base}.py"; do
                [ -f "$candidate" ] && printf '%s\n' "${candidate#$_VERIFICATION_DIR/}"
              done
              ;;
          esac
        done < <(printf '%s\n' "$CHANGED" | grep -E '@PYTHON_PATH_RE@') \
          >>"$_verification_candidates"
      fi
      if [ "$_verification_resolver_rc" -ne 0 ]; then
        echo "[check] required backend verification resolver FAILED (exit $_verification_resolver_rc)" >&2
        cat "$_verification_stderr" >&2
        FAILED+=("$_VERIFICATION_DIR verification resolver")
      fi
      _VERIFICATION_SELECTORS=$(
        sed 's|^@SELECTOR_PREFIX_RE@||' "$_verification_candidates" \
          | while IFS= read -r selector; do
              [ -n "$selector" ] || continue
              [ -f "$_VERIFICATION_DIR/${selector%%::*}" ] && printf '%s\n' "$selector"
            done \
          | awk '!seen[$0]++'
      )
      rm -f "$_verification_candidates"
      rm -f "$_verification_stderr"
    fi
    if [ "$_VERIFICATION_RUFF_OK" = "1" ] && [ -z "$_VERIFICATION_SELECTORS" ]; then
      echo "[check] required backend verification FAILED — no impacted pytest file/node was resolved"
      FAILED+=("$_VERIFICATION_DIR verification: no impacted pytest")
    elif [ "$_VERIFICATION_RUFF_OK" = "1" ]; then
      _verification_count=$(printf '%s\n' "$_VERIFICATION_SELECTORS" | grep -c '^' || true)
      if [ "$_verification_count" -gt 8 ]; then
        echo "[check] required backend verification: $_verification_count impacted files; running bounded first 8"
      fi
      mapfile -t _verification_selector_args < <(printf '%s\n' "$_VERIFICATION_SELECTORS" | head -n 8)
      if [ -z "$_VERIFICATION_HARNESS" ] || [ ! -f "$_VERIFICATION_HARNESS" ]; then
        echo "[check] required backend verification FAILED — sanctioned harness missing: $_VERIFICATION_HARNESS"
        FAILED+=("$_VERIFICATION_DIR verification: harness missing")
      else
        _verification_postgres_flag=""
        if PG_BIN="$(resolve_pg_bindir)"; then
          _verification_postgres_flag=--sandbox-postgres
        elif command -v docker >/dev/null 2>&1 && docker info >/dev/null 2>&1; then
          # Developer/bootstrap compatibility. A runner can only advertise this
          # capability when the sandbox branch above works; this fallback lets
          # the same tracked check run outside bwrap while images roll forward.
          _verification_postgres_flag=--postgres
        else
          echo "[check] required backend verification FAILED — PostgreSQL runtime unavailable"
          FAILED+=("$_VERIFICATION_DIR verification: PostgreSQL unavailable")
        fi
      fi
      if [ -n "${_verification_postgres_flag:-}" ]; then
        echo "[check] $_VERIFICATION_DIR required targeted Postgres pytest..."
        _verification_harness_receipt=/tmp/backend_verification_harness_@TASK_ID@.json
        rm -f "$_verification_harness_receipt"
        if ( set -o pipefail; BLUEPRINT_VERIFICATION_RESULT_FILE="$_verification_harness_receipt" USE_TEST_POSTGRES=true bash "$_VERIFICATION_HARNESS" "$_verification_postgres_flag" "${_verification_selector_args[@]}" 2>&1 | tail -200 ) \
          && python3 -c 'import json,sys; d=json.load(open(sys.argv[1],encoding="utf-8")); assert d.get("exit_code")==0 and d.get("targeted") is True and d.get("use_test_postgres") is True and d.get("postgres_sidecar") is True and int(d.get("tests",0))>int(d.get("skipped",0)) and d.get("selectors")' "$_verification_harness_receipt"; then
          _verification_selectors_json=$(printf '%s\n' "${_verification_selector_args[@]}" | python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]))')
          _verification_tmp="${_VERIFICATION_RECEIPT}.$$"
          if python3 -c 'import datetime,json,sys; out,task,directory,harness,selectors,harness_receipt,postgres_flag=sys.argv[1:]; runtime=json.load(open(harness_receipt,encoding="utf-8")); payload={"version":1,"task_id":task,"directory":directory,"created_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"ruff":{"command":"python3 -m ruff check --select E,F,W --ignore E501 <changed-python-files>","exit_code":0},"pytest":{"command":"USE_TEST_POSTGRES=true bash "+harness+" "+postgres_flag+" <impacted-selectors>","harness":harness,"selectors":json.loads(selectors),"targeted":True,"use_test_postgres":True,"postgres_sidecar":True,"tests":runtime["tests"],"skipped":runtime["skipped"],"exit_code":0}}; open(out,"w",encoding="utf-8").write(json.dumps(payload,sort_keys=True)+"\n")' "$_verification_tmp" '@TASK_ID@' "$_VERIFICATION_DIR" "$_VERIFICATION_HARNESS" "$_verification_selectors_json" "$_verification_harness_receipt" "$_verification_postgres_flag" \
            && mv -f "$_verification_tmp" "$_VERIFICATION_RECEIPT"; then
            echo "[check] $_VERIFICATION_DIR required targeted Postgres pytest OK"
            echo "[check] verification receipt: $_VERIFICATION_RECEIPT"
          else
            echo "[check] required backend verification FAILED — could not write receipt"
            rm -f "$_verification_tmp" "$_VERIFICATION_RECEIPT"
            FAILED+=("$_VERIFICATION_DIR verification receipt")
          fi
        else
          echo "[check] $_VERIFICATION_DIR required targeted Postgres pytest FAILED"
          rm -f "$_VERIFICATION_RECEIPT"
          FAILED+=("$_VERIFICATION_DIR required targeted Postgres pytest")
        fi
        rm -f "$_verification_harness_receipt"
      fi
    fi
    rm -f "$_VERIFICATION_CHANGED"
