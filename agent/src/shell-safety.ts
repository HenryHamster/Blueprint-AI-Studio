// Shared shell allowlist for build_config values that are interpolated into
// generated bash snippets and reviewer instructions.
export const SHELL_CMD_RE = /^[A-Za-z0-9 _./@:=,+-]+$/;
