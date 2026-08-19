    # required backend-authoring verification. No skip can mint this receipt.
    _VERIFICATION_ROUTE_HELPER_OK=0
    unset -f select_backend_test_route 2>/dev/null || true
    if [ -r scripts/backend-test-route.sh ] \
      && bash -n scripts/backend-test-route.sh >/dev/null 2>&1 \
      && source scripts/backend-test-route.sh \
      && declare -F select_backend_test_route >/dev/null 2>&1; then
      _VERIFICATION_ROUTE_HELPER_OK=1
    else
      echo "[check] required backend verification FAILED — route helper unavailable or invalid"
      FAILED+=("backend verification route helper")
    fi
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
        echo "[check] required backend verification: $_verification_count impacted files; running the 8 most diff-relevant (the remainder are not run here)"
      fi
      mapfile -t _verification_selector_args < <(printf '%s\n' "$_VERIFICATION_SELECTORS" | head -n 8)
      _verification_selectors_json=$(printf '%s\n' "${_verification_selector_args[@]}" | python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]))')
      _verification_postgres_flag=""
      if [ -z "$_VERIFICATION_HARNESS" ] || [ ! -f "$_VERIFICATION_HARNESS" ]; then
        echo "[check] required backend verification FAILED — sanctioned harness missing: $_VERIFICATION_HARNESS"
        FAILED+=("$_VERIFICATION_DIR verification: harness missing")
      elif [ "$_VERIFICATION_ROUTE_HELPER_OK" = "1" ]; then
        if _verification_postgres_flag="$(select_backend_test_route)"; then
          :
        else
          echo "[check] required backend verification state=runtime_unavailable — PostgreSQL runtime unavailable"
          _verification_capability_tmp="${_VERIFY_CAPABILITY_RECEIPT}.$$"
          if ( umask 077; printf 'backend_verification:runtime_unavailable:v1:%s\n' \
            "$_VERIFY_TASK_ID" >"$_verification_capability_tmp" ) \
            && mv -f "$_verification_capability_tmp" "$_VERIFY_CAPABILITY_RECEIPT"; then
            echo "[check] execution capability receipt: $_VERIFY_CAPABILITY_RECEIPT"
          else
            echo "[check] unable to record backend verification capability receipt" >&2
            rm -f "$_verification_capability_tmp" "$_VERIFY_CAPABILITY_RECEIPT"
            FAILED+=("$_VERIFICATION_DIR verification: capability receipt")
          fi
          FAILED+=("$_VERIFICATION_DIR verification: runtime_unavailable")
        fi
      fi
      if [ -n "${_verification_postgres_flag:-}" ]; then
        echo "[check] $_VERIFICATION_DIR required targeted Postgres pytest..."
        _verification_harness_receipt="$_VERIFY_HARNESS_RECEIPT"
        _verification_attribution_dir=$(mktemp -d /tmp/check_verification_attribution.XXXXXX)
        _verification_current_junit="$_verification_attribution_dir/current.xml"
        rm -f "$_verification_harness_receipt" "$_verification_current_junit"
        _verification_exit=0
        if ( set -o pipefail; BLUEPRINT_VERIFICATION_RESULT_FILE="$_verification_harness_receipt" BLUEPRINT_VERIFICATION_JUNIT="$_verification_current_junit" USE_TEST_POSTGRES=true bash "$_VERIFICATION_HARNESS" "$_verification_postgres_flag" "${_verification_selector_args[@]}" 2>&1 | tail -200 ); then
          :
        else
          _verification_exit=$?
        fi
        if [ -s "$_verification_current_junit" ]; then
          _verification_attribution=$(python3 scripts/backend_verification_guard.py \
            --base "$BASE" --repo-root "$PWD" --current-junit "$_verification_current_junit" \
            --current-exit-code "$_verification_exit" --route="$_verification_postgres_flag" \
            --harness "$_VERIFICATION_HARNESS" "${_verification_selector_args[@]}")
        else
          _verification_attribution='{"state":"attribution_unavailable","blocking":true,"error":"current JUnit report missing after targeted harness"}'
        fi
        echo "[check] backend verification attribution: $_verification_attribution"
        # A run with many suppressed baseline failures (each carrying a full
        # traceback) can make this blob large enough to blow the OS argv-size
        # limit (E2BIG / "Argument list too long") if passed as a literal CLI
        # argument below. Persist it to a file and pass the PATH instead so
        # receipt-writing scales with suppressed-failure count, not argv size.
        _verification_attribution_file="$_verification_attribution_dir/attribution.json"
        printf '%s' "$_verification_attribution" >"$_verification_attribution_file"
        _verification_state=$(printf '%s' "$_verification_attribution" | python3 -c 'import json,sys; print(json.load(sys.stdin).get("state","attribution_unavailable"))' 2>/dev/null || printf attribution_unavailable)
        _verification_tests=$(python3 -c 'import sys,xml.etree.ElementTree as ET; print(sum(1 for _ in ET.parse(sys.argv[1]).getroot().iter("testcase")))' "$_verification_current_junit" 2>/dev/null || printf 0)
        _verification_skipped=$(python3 -c 'import sys,xml.etree.ElementTree as ET; print(sum(1 for c in ET.parse(sys.argv[1]).getroot().iter("testcase") if c.find("skipped") is not None))' "$_verification_current_junit" 2>/dev/null || printf 0)
        if [ "$_verification_tests" -gt 0 ] && [ "$_verification_skipped" -eq "$_verification_tests" ]; then
          _verification_state=skipped_test
        fi
        if [ "$_verification_state" = "passed" ] || [ "$_verification_state" = "preexisting_test_failure" ]; then
          echo "[check] $_VERIFICATION_DIR required targeted Postgres pytest OK — state=$_verification_state"
          _verification_selectors_json=$(printf '%s\n' "${_verification_selector_args[@]}" | python3 -c 'import json,sys; print(json.dumps([line.rstrip("\n") for line in sys.stdin if line.rstrip("\n")]))')
          if python3 -c 'import datetime,json,sys,xml.etree.ElementTree as ET; out,task,directory,harness,selectors,attribution_file,junit=sys.argv[1:]; d=json.load(open(attribution_file,encoding="utf-8")); root=ET.parse(junit).getroot(); cases=list(root.iter("testcase")); skipped=sum(1 for c in cases if c.find("skipped") is not None); failed=sum(1 for c in cases if c.find("failure") is not None or c.find("error") is not None); payload={"version":1,"task_id":task,"directory":directory,"created_at":datetime.datetime.now(datetime.timezone.utc).isoformat(),"ruff":{"command":"python3 -m ruff check --select E,F,W --ignore E501 <changed-python-files>","exit_code":0},"pytest":{"command":"USE_TEST_POSTGRES=true bash "+harness+" <impacted-selectors>","harness":harness,"selectors":json.loads(selectors),"targeted":True,"use_test_postgres":True,"postgres_sidecar":True,"state":d["state"],"current_exit_code":int(d.get("current_exit_code",0)),"tests":len(cases),"skipped":skipped,"failures":failed,"new_failures":d.get("new_failures",{}),"suppressed_failures":d.get("suppressed_failures",{}),"disappeared_failures":d.get("disappeared_failures",{}),"skipped_failures":d.get("skipped_failures",{}),"exit_code":0}}; open(out,"w",encoding="utf-8").write(json.dumps(payload,sort_keys=True)+"\n")' "$_VERIFY_RECEIPT" "$_VERIFY_TASK_ID" "$_VERIFICATION_DIR" "$_VERIFICATION_HARNESS" "$_verification_selectors_json" "$_verification_attribution_file" "$_verification_current_junit"; then
            echo "[check] verification receipt: $_VERIFY_RECEIPT"
          else
            echo "[check] required backend verification FAILED — could not write attribution receipt"
            FAILED+=("$_VERIFICATION_DIR verification receipt")
          fi
        else
          echo "[check] required backend verification FAILED — state=$_verification_state"
          rm -f "$_VERIFY_RECEIPT"
          FAILED+=("$_VERIFICATION_DIR verification: $_verification_state")
        fi
        rm -rf "$_verification_attribution_dir"
        rm -f "$_verification_harness_receipt"
      fi
    fi
    rm -f "$_VERIFICATION_CHANGED"
