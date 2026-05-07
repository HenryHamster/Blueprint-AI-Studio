"""Unit tests for log sanitization in app/broadcast.py.

Pure-function tests for _sanitize_log — no DB, no async, no fixtures.
Run with: bash scripts/test-backend.sh testing/test_broadcast.py --standalone
"""

from app.broadcast import _sanitize_log


# --- Individual pattern coverage ---------------------------------------------


def test_github_personal_access_token():
    out = _sanitize_log("auth=ghp_abc123XYZ_token end")
    assert "ghp_abc123XYZ_token" not in out
    assert "ghp_***" in out
    assert out == "auth=ghp_*** end"


def test_github_server_token():
    out = _sanitize_log("token ghs_serverToken_9 rest")
    assert "ghs_serverToken_9" not in out
    assert "ghs_***" in out


def test_github_pat_token():
    out = _sanitize_log("pat=github_pat_AAA_BBB_ccc tail")
    assert "github_pat_AAA_BBB_ccc" not in out
    assert "github_pat_***" in out


def test_https_x_access_token():
    out = _sanitize_log(
        "clone https://x-access-token:ghs_secret@github.com/org/repo.git"
    )
    assert "ghs_secret" not in out
    assert "https://x-access-token:***@" in out
    assert out.endswith("github.com/org/repo.git")


def test_anthropic_api_key():
    out = _sanitize_log("key=sk-ant-api03-abc-XYZ_123 done")
    assert "sk-ant-api03-abc-XYZ_123" not in out
    assert "sk-ant-***" in out


def test_bearer_token():
    out = _sanitize_log("Authorization: Bearer abc.def.ghi more")
    assert "abc.def.ghi" not in out
    assert "Bearer ***" in out


def test_bearer_token_case_insensitive():
    out = _sanitize_log("authorization: bearer abc.def.ghi")
    assert "abc.def.ghi" not in out
    assert "Bearer ***" in out


def test_oauth_token_equals():
    out = _sanitize_log("oauth_token=xxyyzz1234 tail")
    assert "xxyyzz1234" not in out
    assert "oauth_token=***" in out


def test_oauth_token_colon_case_insensitive():
    out = _sanitize_log("OAUTH_TOKEN: xxyyzz1234 tail")
    assert "xxyyzz1234" not in out
    assert "oauth_token=***" in out


def test_jwt_three_segment():
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ.abcDEF_123-xyz"
    out = _sanitize_log(f"token={jwt} rest")
    assert jwt not in out
    assert "eyJ***" in out


def test_pem_private_key_block():
    pem = (
        "-----BEGIN RSA PRIVATE KEY-----\n"
        "MIIEowIBAAKCAQEAu1SU1LfVLPHCozMxH2Mo4lgOEePzNm0tRgeLezV6ffAt0gun\n"
        "VTLw7onLRnrq0/IzW7yWR7QkrmBL7jTKEn5u+qKhbwKfBstIs+bMY2Zkp18gnTxK\n"
        "-----END RSA PRIVATE KEY-----"
    )
    out = _sanitize_log(f"private key: {pem} trailer")
    assert "MIIEowIBAAKCAQEA" not in out
    assert "***PRIVATE KEY***" in out
    assert out.endswith("trailer")


# --- Negative cases (must NOT be sanitized) ----------------------------------


def test_bare_prefixes_not_sanitized():
    # Prefixes without suffix characters should not match (require + quantifier)
    for safe in ("ghp_", "ghs_", "github_pat_", "sk-ant-"):
        out = _sanitize_log(f"prefix only: {safe} end")
        assert out == f"prefix only: {safe} end"


def test_bearer_without_token_not_sanitized():
    # Bearer pattern requires at least one non-whitespace char after whitespace
    out = _sanitize_log("Bearer ")
    assert out == "Bearer "


def test_two_segment_jwt_not_sanitized():
    # JWT pattern requires three dot-separated segments beginning with eyJ...
    two_seg = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjMifQ"
    out = _sanitize_log(two_seg)
    assert out == two_seg


def test_unrelated_text_unchanged():
    text = "agent log: building module at src/main.py (42 tests passed)"
    assert _sanitize_log(text) == text


def test_empty_string_returned_as_is():
    assert _sanitize_log("") == ""


def test_none_input_returned_as_is():
    # Falsy short-circuit returns input unchanged
    assert _sanitize_log(None) is None


# --- Combination / overlap cases ---------------------------------------------


def test_multiple_distinct_secrets_all_redacted():
    text = (
        "ghp_tokenONE sk-ant-api03-tokenTWO "
        "Authorization: Bearer jwtHere "
        "url=https://x-access-token:ghs_inner@github.com/x/y.git"
    )
    out = _sanitize_log(text)
    assert "ghp_tokenONE" not in out
    assert "sk-ant-api03-tokenTWO" not in out
    assert "jwtHere" not in out
    assert "ghs_inner" not in out
    assert "ghp_***" in out
    assert "sk-ant-***" in out
    assert "Bearer ***" in out
    assert "https://x-access-token:***@" in out


def test_repeated_same_secret_all_redacted():
    out = _sanitize_log("a ghp_ONE b ghp_TWO c")
    assert "ghp_ONE" not in out
    assert "ghp_TWO" not in out
    assert out.count("ghp_***") == 2


def test_bearer_stops_at_quote_and_comma():
    # Bearer pattern excludes whitespace, comma, quote chars to preserve structure
    out = _sanitize_log('headers={"Authorization": "Bearer abc.def", "X": "y"}')
    assert "abc.def" not in out
    assert "Bearer ***" in out
    # Trailing quote and "X" preserved
    assert '"X": "y"' in out
