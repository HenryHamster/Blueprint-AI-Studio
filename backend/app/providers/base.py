from __future__ import annotations

import json
import logging
import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Any, Literal, Mapping

from app.models import LLMAuthMode, LLMProviderName

logger = logging.getLogger(__name__)


@dataclass(frozen=True)
class ValidationResult:
    secret: str
    provider_metadata: dict[str, Any]


# Cap for parsed retry-hint values. Providers sometimes emit absurd delays
# ("please retry in 99999s"), so treat 4h as a sanity ceiling — matches the
# existing exponential-backoff cap in max_plan_pool.disable_account_model.
_RETRY_HINT_MAX_SECONDS = 4 * 3600


@dataclass(frozen=True)
class RateLimitSignal:
    """Structured result of classifying a rate-limit error string.

    - `kind="rate_limit"` is a transient throttle — exponential backoff resolves it.
    - `kind="quota_exhausted"` is a billing/quota wall — needs operator action
      (refill credits, raise monthly budget). Pool should cool down longer and
      admin UI should surface it distinctly from a temporary 429.
    - `retry_after_seconds` is a server-provided floor parsed from the error
      body (Gemini `Please retry in 39s`, OpenAI `try again after 60.6 seconds`).
      None when no hint is present. Capped at 4h as a sanity ceiling.
    - `matched_pattern` is the substring that matched — useful for logs and
      observability.
    """

    kind: Literal["rate_limit", "quota_exhausted"]
    retry_after_seconds: int | None
    matched_pattern: str


# One shared regex for retry-hint parsing across providers. Matches phrasing
# observed in real OpenAI / Gemini / Codex bodies:
#   "please retry in 39.8s", "please try again in 1.574s",
#   "please try again after 60.616 seconds", "retry after 30 seconds",
#   "try again in 2 minutes"
# Clock-time forms ("resets on 2:49 PM", "try again at Apr 17th") are
# intentionally NOT parsed — we can't convert to a delta without trusting
# the client clock, and a wrong value is worse than no hint.
_RETRY_HINT_RE = re.compile(
    r"(?:please\s+)?(?:retry|try again)(?:\s+after|\s+in)?\s+"
    r"(?P<value>\d+(?:\.\d+)?)\s*"
    r"(?P<unit>seconds?|secs?|s|minutes?|mins?|m|hours?|h)\b",
    re.IGNORECASE,
)


def parse_retry_hint_seconds(text: str) -> int | None:
    """Extract a server-provided retry delay from an error string.

    Returns None if no hint is present or the parsed value is non-positive.
    Bare unit-less numbers (e.g. "retry in 30") are not matched — the unit is
    required to avoid misreading version numbers or token counts.
    """
    if not text:
        return None
    match = _RETRY_HINT_RE.search(text)
    if not match:
        return None
    try:
        value = float(match.group("value"))
    except (TypeError, ValueError):
        return None
    unit = (match.group("unit") or "s").lower()
    if unit.startswith("h"):
        seconds = value * 3600
    elif unit.startswith("m") and not unit.startswith("ms"):
        # "m" / "min" / "minutes" — "ms" isn't in the regex vocabulary but guard anyway.
        seconds = value * 60
    else:
        seconds = value
    if seconds <= 0:
        return None
    return int(min(seconds, _RETRY_HINT_MAX_SECONDS))


@dataclass(frozen=True)
class UsageSnapshot:
    """Normalized, provider-agnostic account usage-headroom snapshot.

    Produced by :meth:`LLMProvider.probe_usage`. ADVISORY ONLY — consumed by the
    pool's proactive headroom gate/ranking, never by correctness paths. The
    five-hour window is sufficient for a known snapshot; seven-day and any
    provider-specific windows are additive (utilization is a percentage in
    roughly [0, 100]).

    - ``known=False`` is the sentinel "no clean usage source / probe failed".
      Selection MUST treat an unknown snapshot exactly as it behaves with no
      probe at all (not skipped by the gate, ranked neutrally).
    - ``window_utilizations`` maps window name → float utilization percentage.
    - ``resets_at`` maps window name → ISO-8601 reset timestamp or None.
    - ``probed_at`` is the ISO-8601 time the probe ran (None for the unknown
      sentinel that was synthesized without a probe).
    """

    known: bool
    window_utilizations: Mapping[str, float] = field(default_factory=dict)
    resets_at: Mapping[str, str | None] = field(default_factory=dict)
    probed_at: str | None = None
    # Raw provider blob (messages_api_header format) carried through the probe
    # pipeline so the persist layer can store full remaining/limit counts, not
    # just the derived utilization percentage. Not serialized by to_dict(), not
    # considered in equality, hash, or repr — purely a pipeline transport slot.
    raw_rate_limit_blob: dict | None = field(
        default=None, compare=False, hash=False, repr=False
    )
    # A stable, non-sensitive classification for an unsuccessful probe.  This
    # deliberately lives on the snapshot rather than being inferred from log
    # text, so the pool can persist and render a useful last-attempt state
    # without ever exposing provider responses or credentials.
    #
    # Known values are provider-agnostic where possible: ``unsupported``,
    # ``missing_secret``, ``secret_unavailable``, ``authentication``,
    # ``rate_limited``, ``timeout``, ``transport``, ``endpoint``, ``schema``, and
    # ``provider_error``.  Unknown values remain safe additive data.
    probe_failure_kind: str | None = field(
        default=None, compare=False, hash=False, repr=False
    )

    @classmethod
    def unknown(
        cls,
        probed_at: str | None = None,
        *,
        failure_kind: str | None = None,
    ) -> UsageSnapshot:
        return cls(
            known=False,
            probed_at=probed_at,
            probe_failure_kind=failure_kind,
        )

    def load_bearing_utilizations(self) -> tuple[float, ...]:
        """The five-hour / seven-day utilizations that are present + numeric.

        Empty tuple when the snapshot is unknown or carries neither window —
        callers treat that as "unknown" for gate/ranking purposes.
        """
        if not self.known:
            return ()
        out: list[float] = []
        for window in ("five_hour", "seven_day"):
            val = self.window_utilizations.get(window)
            if isinstance(val, bool):
                continue
            if isinstance(val, (int, float)):
                out.append(float(val))
        return tuple(out)

    def to_dict(self) -> dict[str, Any]:
        """Serialize to a JSON-safe dict for DB persistence.

        Callers that persist to DB should also set an ``updated_at`` key with
        the write timestamp so freshness comparisons work across restarts.
        Readable by :meth:`from_dict`.
        """
        d: dict[str, Any] = {
            "source": "oauth_usage_probe",
            "known": self.known,
        }
        if self.probed_at is not None:
            d["probed_at"] = self.probed_at
        if self.probe_failure_kind:
            d["probe_failure_kind"] = self.probe_failure_kind
        if self.known:
            for window, util in self.window_utilizations.items():
                entry: dict[str, Any] = {"utilization": float(util)}
                reset = self.resets_at.get(window)
                if reset:
                    entry["resets_at"] = reset
                d[window] = entry
        return d

    @classmethod
    def from_dict(cls, raw: Any) -> UsageSnapshot | None:
        """Reconstruct from a dict written by :meth:`to_dict`.

        Returns ``None`` on any structural mismatch so callers can treat a
        bad/foreign blob as "no snapshot". Backwards-compatible with old blobs
        that lack the ``known`` or ``probed_at`` fields.
        """
        if not isinstance(raw, Mapping) or raw.get("source") != "oauth_usage_probe":
            return None
        known = raw.get("known", True)
        # Prefer explicit probed_at; fall back to updated_at for old blobs.
        probed_at: str | None = raw.get("probed_at") or raw.get("updated_at")
        failure_kind = raw.get("probe_failure_kind")
        if not isinstance(failure_kind, str) or not failure_kind:
            failure_kind = None
        if not known:
            return cls.unknown(probed_at, failure_kind=failure_kind)
        _reserved = frozenset(
            {"source", "known", "probed_at", "updated_at", "probe_failure_kind"}
        )
        utils: dict[str, float] = {}
        resets: dict[str, str | None] = {}
        for key, entry in raw.items():
            if key in _reserved or not isinstance(entry, Mapping):
                continue
            util = entry.get("utilization")
            if isinstance(util, bool) or not isinstance(util, (int, float)):
                continue
            utils[key] = float(util)
            reset = entry.get("resets_at")
            if isinstance(reset, str):
                resets[key] = reset
        if not utils:
            return None
        return cls(
            known=True,
            window_utilizations=utils,
            resets_at=resets,
            probed_at=probed_at,
            probe_failure_kind=failure_kind,
        )


@dataclass(frozen=True)
class FileMaterial:
    path: str
    content: str
    mode: str = "0600"


@dataclass(frozen=True)
class SecretMaterial:
    env: dict[str, str]
    files: tuple[FileMaterial, ...] = ()


@dataclass(frozen=True)
class CLITranslation:
    """Output of settings_to_cli(): what the agent runner should apply to a CLI invocation.

    - `env`: extra environment variables to set for the subprocess (merged with secret env).
    - `args`: ordered CLI flags to append after the base invocation.
    - `settings_files`: files to materialize on disk before invocation (e.g., ~/.gemini/settings.json).
    - `prompt_fragments`: strings to prepend/append to the system prompt as fallback for knobs
      the CLI cannot express natively (advisor mode, tool_choice on Codex, etc).
    - `unsupported`: knob names from the settings bag that produced no native translation.
      Logged by the runner; operators use this to know when a setting is being silently dropped.
    """

    env: dict[str, str] = field(default_factory=dict)
    args: tuple[str, ...] = ()
    settings_files: tuple[FileMaterial, ...] = ()
    prompt_fragments: tuple[str, ...] = ()
    unsupported: tuple[str, ...] = ()


class LLMProvider(ABC):
    name: LLMProviderName
    supported_auth_modes: tuple[LLMAuthMode, ...]

    @abstractmethod
    def validate_secret(
        self,
        raw: str,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> ValidationResult:
        raise NotImplementedError

    @abstractmethod
    def tier_to_model(self, tier: str, runtime_config: Mapping[str, Any]) -> str | None:
        raise NotImplementedError

    @abstractmethod
    def rate_limit_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        raise NotImplementedError

    # Patterns that mean "billing/quota wall, not a transient rate limit".
    # Subset of rate_limit_patterns() — anything in this set is tagged
    # kind="quota_exhausted" by classify_rate_limit_signal(). Default empty:
    # providers opt in by overriding. Must be lowercase, substring-safe.
    def quota_exhausted_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        return ()

    def dead_auth_patterns(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        """Substrings (lowercased) that unambiguously identify a permanently dead
        OAuth session in provider CLI stderr output.

        Matching any of these auto-disables the account fleet-wide and requires
        operator re-auth. Only declare strings that appear EXCLUSIVELY when the
        auth is irrecoverably broken — never for transient 401s or short-lived
        refresh failures. Default is empty (no dead-auth detection); providers
        with OAuth support override this.
        """
        return ()

    def classify_rate_limit_signal(
        self, auth_mode: LLMAuthMode, text: str
    ) -> RateLimitSignal | None:
        """Scan a combined status+logs string for a rate-limit signature.

        Returns None when no pattern matches. Otherwise returns a
        RateLimitSignal with:
        - kind="quota_exhausted" if the match is in quota_exhausted_patterns()
          (permanent billing wall — needs operator action)
        - kind="rate_limit" otherwise (transient throttle)
        - retry_after_seconds parsed from the full text via parse_retry_hint_seconds

        Base implementation is a plain substring scan driven by
        rate_limit_patterns(). Providers override only when they need custom
        logic beyond the pattern lists.
        """
        if not text:
            return None
        lowered = text.lower()
        try:
            patterns = self.rate_limit_patterns(auth_mode)
        except Exception:
            logger.exception(
                "[PROVIDER] rate_limit_patterns failed for %s/%s",
                getattr(self, "name", "?"),
                auth_mode,
            )
            return None
        quota_patterns: tuple[str, ...] = ()
        try:
            quota_patterns = self.quota_exhausted_patterns(auth_mode)
        except Exception:
            logger.exception(
                "[PROVIDER] quota_exhausted_patterns failed for %s/%s",
                getattr(self, "name", "?"),
                auth_mode,
            )
            quota_patterns = ()
        # Quota-exhausted patterns win when both lists match. Real 429 bodies
        # often contain a quota_exhausted marker alongside a generic
        # "quota exceeded for metric" — we want the billing-wall signal to
        # drive the flat 24h cooldown, not the shorter exponential.
        matched: str | None = None
        kind: Literal["rate_limit", "quota_exhausted"] = "rate_limit"
        for pattern in quota_patterns:
            if pattern and pattern in lowered:
                matched = pattern
                kind = "quota_exhausted"
                break
        if matched is None:
            for pattern in patterns:
                if pattern and pattern in lowered:
                    matched = pattern
                    break
        if matched is None:
            return None
        return RateLimitSignal(
            kind=kind,
            retry_after_seconds=parse_retry_hint_seconds(lowered),
            matched_pattern=matched,
        )

    @abstractmethod
    def required_metadata_fields(self, auth_mode: LLMAuthMode) -> tuple[str, ...]:
        raise NotImplementedError

    def supported_execution_modes(self) -> tuple[str, ...]:
        return ("default",)

    @abstractmethod
    def secret_material(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> SecretMaterial:
        raise NotImplementedError

    def settings_schema(self) -> dict[str, Any]:
        """JSON-schema-shaped declaration of this provider's tunable knobs.

        Consumed by the admin API to drive dynamic UI rendering, and by
        validate_settings() to reject unknown keys / enum violations.
        Default: empty schema (provider exposes no knobs).
        """
        return {"type": "object", "properties": {}, "additionalProperties": False}

    def validate_settings(self, bag: Mapping[str, Any]) -> dict[str, Any]:
        """Validate a settings bag against settings_schema().

        Returns a cleaned copy with only recognized keys and coerced types.
        Raises ValueError on enum violations or structural mismatch. Unknown
        top-level keys are rejected (fail-closed) so typos don't silently no-op.
        """
        return _validate_against_schema(bag or {}, self.settings_schema(), path="")

    def settings_to_cli(
        self,
        bag: Mapping[str, Any],
        auth_mode: LLMAuthMode | None = None,
    ) -> CLITranslation:
        """Translate a validated settings bag into CLI env/args/files/fallbacks.

        `auth_mode` lets providers gate features that depend on the auth
        surface — e.g. OpenAI's service_tier=flex and codex-variant models
        are only reachable on API-key billing, never on ChatGPT-subscription
        OAuth. Optional for back-compat with callers that don't track mode.

        Default implementation returns an empty translation. Providers override
        to map their specific knobs onto their CLI's surface.
        """
        return CLITranslation()

    async def list_available_models(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> list[str] | None:
        """Return the list of model IDs this account/key can call.

        Providers override with a list-models API call so callers can filter
        pool accounts by what the key actually has access to (Gemini preview
        gating, OpenAI org flags, etc).

        Contract:
        - Returns a concrete list (possibly empty) when the call succeeded.
        - Returns None when the provider doesn't expose a list endpoint, the
          auth mode isn't supported, or the call failed. Callers must treat
          None as "unknown — don't filter against it"; a failed refresh must
          not starve a functional account.
        - Never raises. All I/O and parse errors log + return None.
        - Short timeout (~5s). Long timeouts here stall account-save/refresh
          and, if wired into dispatch, stall the whole pool selector.

        Default implementation returns None (no listing capability).
        """
        return None

    async def probe_usage(
        self,
        secret: str | None,
        auth_mode: LLMAuthMode,
        provider_metadata: Mapping[str, Any] | None = None,
    ) -> UsageSnapshot:
        """Return a normalized account usage-headroom snapshot, or "unknown".

        Used by the pool's PROACTIVE headroom gate/ranking to avoid dispatching
        long-running work onto a near-exhausted account before it dies mid-run.

        Contract (anti-fragility is mandatory — this is advisory only):
        - Returns a concrete :class:`UsageSnapshot` (``known=True``) only when a
          clean usage source returned the load-bearing windows.
        - Returns ``UsageSnapshot.unknown()`` for ANY of: no usage source for
          this provider/auth-mode, HTTP/auth/timeout error, JSON schema drift,
          a missing/null/non-numeric load-bearing field, or an unparseable body.
          "unknown" makes selection behave exactly as it does with no probe.
        - NEVER raises. All I/O and parse errors are swallowed → unknown.
        - Short timeout (~5s). Must never be called under the pool lock — it
          does network I/O; the pool probes out-of-band and caches the result.

        Default implementation returns unknown (provider exposes no usage
        source), so a provider/auth-mode with no clean source is unchanged.
        """
        return UsageSnapshot.unknown()

    def fallback_model(self, model: str | None) -> str | None:
        """Return the logical GA counterpart for a preview/restricted model.

        Used by the dispatcher when a task requests a model that none of the
        keys in the pool can access: rather than fail the task outright,
        swap to a stable counterpart (e.g. `gemini-3.1-pro-preview`
        →`gemini-2.5-pro`) so work continues with a quality downgrade. The
        downgrade is logged and recorded on the task context.

        Returns None when:
        - `model` is not a preview/restricted model, or
        - no counterpart is registered.

        Default implementation returns None (no fallback defined). Providers
        override with the preview→GA map for their family.
        """
        return None

    def account_has_credentials(self, account: Mapping[str, Any] | Any) -> bool:
        secret_blob_ref = _account_value(account, "secret_blob_ref")
        oauth_token = _account_value(account, "oauth_token")
        runtime_secret = _account_value(account, "runtime_secret")
        return bool(runtime_secret or secret_blob_ref or oauth_token)

    def ensure_supported_auth_mode(self, auth_mode: LLMAuthMode) -> None:
        if auth_mode not in self.supported_auth_modes:
            allowed = ", ".join(mode.value for mode in self.supported_auth_modes)
            raise ValueError(
                f"{self.name.value} does not support auth_mode={auth_mode.value!r}; allowed: {allowed}"
            )

    def normalize_metadata(
        self, provider_metadata: Mapping[str, Any] | None = None
    ) -> dict[str, Any]:
        metadata = dict(provider_metadata or {})
        missing = [
            field
            for field in self.required_metadata_fields(self.supported_auth_modes[0])
            if field not in metadata
        ]
        if missing:
            raise ValueError(f"Missing provider metadata fields: {', '.join(missing)}")
        return metadata


def validate_required_metadata_fields(
    provider: LLMProvider,
    auth_mode: LLMAuthMode,
    provider_metadata: Mapping[str, Any] | None,
) -> dict[str, Any]:
    metadata = dict(provider_metadata or {})
    missing = [
        field
        for field in provider.required_metadata_fields(auth_mode)
        if not metadata.get(field)
    ]
    if missing:
        raise ValueError(f"Missing provider metadata fields: {', '.join(missing)}")
    return metadata


def _account_value(account: Mapping[str, Any] | Any, key: str) -> Any:
    if isinstance(account, Mapping):
        return account.get(key)
    return getattr(account, key, None)


def parse_json_secret(raw: str, *, require_object: bool = True) -> dict[str, Any]:
    try:
        parsed = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise ValueError("secret must be valid JSON") from exc
    if require_object and not isinstance(parsed, dict):
        raise ValueError("secret JSON must be an object")
    return parsed


def tier_model_from_runtime_config(
    provider: LLMProviderName,
    tier: str,
    runtime_config: Mapping[str, Any],
    defaults: Mapping[str, str],
) -> str | None:
    provider_map = (
        runtime_config.get("provider_tier_model_map", {})
        if isinstance(runtime_config, Mapping)
        else {}
    )
    configured = (
        provider_map.get(provider.value, {})
        if isinstance(provider_map, Mapping)
        else {}
    )
    if isinstance(configured, Mapping) and configured.get(tier):
        return configured[tier]
    return defaults.get(tier)


# ---------------------------------------------------------------------------
# Lightweight schema validator for provider settings bags.
#
# Supports: type (object/string/integer/number/boolean/array), enum, properties,
# additionalProperties (bool), items (for arrays), default. Not a full JSON
# Schema implementation — just enough to validate typed bags without a dep.
# Raises ValueError with a dotted path on any violation. Unknown keys are
# rejected when additionalProperties is False (default).
# ---------------------------------------------------------------------------
def _validate_against_schema(
    value: Any, schema: Mapping[str, Any], *, path: str
) -> Any:
    if not isinstance(schema, Mapping):
        return value
    expected = schema.get("type")
    if expected == "object":
        if value is None:
            value = {}
        if not isinstance(value, Mapping):
            raise ValueError(
                f"{path or '<root>'}: expected object, got {type(value).__name__}"
            )
        props = schema.get("properties") or {}
        extra_ok = schema.get("additionalProperties", False)
        required = schema.get("required") or ()
        cleaned: dict[str, Any] = {}
        for key, raw in dict(value).items():
            if key in props:
                sub_path = f"{path}.{key}" if path else key
                cleaned[key] = _validate_against_schema(raw, props[key], path=sub_path)
            elif extra_ok:
                cleaned[key] = raw
            else:
                raise ValueError(f"{path or '<root>'}: unknown key {key!r}")
        missing = [key for key in required if key not in cleaned]
        if missing:
            raise ValueError(
                f"{path or '<root>'}: missing required keys: {', '.join(missing)}"
            )
        return cleaned
    if expected == "string":
        if value is None:
            return None
        if not isinstance(value, str):
            raise ValueError(f"{path}: expected string")
        enum = schema.get("enum")
        if enum is not None and value not in enum:
            raise ValueError(
                f"{path}: value {value!r} not in allowed values {list(enum)}"
            )
        return value
    if expected == "integer":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"{path}: expected integer")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ValueError(f"{path}: {value} < minimum {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ValueError(f"{path}: {value} > maximum {maximum}")
        return value
    if expected == "number":
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, (int, float)):
            raise ValueError(f"{path}: expected number")
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        if isinstance(minimum, (int, float)) and value < minimum:
            raise ValueError(f"{path}: {value} < minimum {minimum}")
        if isinstance(maximum, (int, float)) and value > maximum:
            raise ValueError(f"{path}: {value} > maximum {maximum}")
        return value
    if expected == "boolean":
        if value is None:
            return None
        if not isinstance(value, bool):
            raise ValueError(f"{path}: expected boolean")
        return value
    if expected == "array":
        if value is None:
            return None
        if not isinstance(value, list):
            raise ValueError(f"{path}: expected array")
        items = schema.get("items")
        if items is None:
            return list(value)
        return [
            _validate_against_schema(item, items, path=f"{path}[{i}]")
            for i, item in enumerate(value)
        ]
    return value


def merge_settings(*bags: Mapping[str, Any] | None) -> dict[str, Any]:
    """Deep-merge a sequence of settings bags (later bags win).

    Used to compose provider_settings_defaults → task_type_provider_overrides →
    payload.provider_settings. Nested objects are merged recursively; scalars,
    arrays, and None values from later bags replace earlier ones wholesale.
    """
    result: dict[str, Any] = {}
    for bag in bags:
        if not bag:
            continue
        _deep_merge_into(result, bag)
    return result


def _deep_merge_into(target: dict[str, Any], source: Mapping[str, Any]) -> None:
    for key, value in source.items():
        existing = target.get(key)
        if isinstance(existing, dict) and isinstance(value, Mapping):
            _deep_merge_into(existing, value)
        else:
            target[key] = value
