import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

/**
 * `typed` deliberately shares the same local-only process with call Fast, but
 * carries a different receipt shape.  It is not a transport or a network
 * client: the parent Coordinator remains the only component that can commit a
 * public message or enqueue Workday work.
 */
export type CallResidentToolRole = 'fast' | 'typed' | 'workday';

export interface CallResidentToolPaths {
  stageDir: string;
  receiptPath?: string;
  /** Host-owned marker; Workday packet emission is valid only for active-call turns. */
  privateTurnMarkerPath?: string;
}

export interface ProvisionedCallResidentTool {
  configPath: string;
  toolNames: readonly string[];
}

type McpConfig = { mcpServers?: Record<string, unknown> };

const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const serverSourcePath = path.resolve(moduleDir, '../_agent_scripts/call_resident_mcp.mjs');

export function provisionCallResidentTool(
  workspace: string,
  role: CallResidentToolRole,
  paths: CallResidentToolPaths,
): ProvisionedCallResidentTool {
  if ((role === 'fast' || role === 'typed') && !paths.receiptPath) {
    throw new Error('Fast foreground tool requires a turn receipt path');
  }
  if (role === 'workday' && !paths.privateTurnMarkerPath) {
    throw new Error('Workday packet emitter requires a private turn marker path');
  }
  fs.mkdirSync(paths.stageDir, { recursive: true, mode: 0o700 });
  fs.chmodSync(paths.stageDir, 0o700);
  const scriptPath = path.join(workspace, '.call-resident-mcp.mjs');
  const configPath = path.join(workspace, '.call-resident-mcp.json');
  fs.writeFileSync(scriptPath, fs.readFileSync(serverSourcePath, 'utf8'), { encoding: 'utf8', mode: 0o700 });
  fs.chmodSync(scriptPath, 0o700);
  const toolNames = role === 'fast'
    ? ['mcp__call__complete_turn', 'mcp__call__request_workday', 'mcp__call__show_display_blocks']
    : role === 'typed'
      ? ['mcp__call__complete_turn', 'mcp__call__request_workday']
      : ['mcp__call__emit_call_packet'];
  fs.writeFileSync(configPath, JSON.stringify({
    mcpServers: {
      call: {
        command: process.execPath,
        args: [scriptPath],
        env: {
          CALL_RESIDENT_ROLE: role,
          CALL_LOCAL_STAGE_DIR: paths.stageDir,
          ...(paths.receiptPath ? { CALL_TURN_RECEIPT_PATH: paths.receiptPath } : {}),
          ...(paths.privateTurnMarkerPath ? {
            BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH: paths.privateTurnMarkerPath,
          } : {}),
        },
      },
    },
  }), { encoding: 'utf8', mode: 0o600 });
  fs.chmodSync(configPath, 0o600);
  return { configPath, toolNames };
}

/**
 * Claude accepts one MCP configuration file per process.  Merge the existing
 * Workday reply server with the call-only server rather than launching a second
 * permissive MCP config.  Conflicting server names are a configuration error.
 */
export function mergeCallMcpConfigs(
  workspace: string,
  baseConfigPath: string,
  callConfigPath: string,
): string {
  const read = (filename: string): McpConfig => {
    const parsed: unknown = JSON.parse(fs.readFileSync(filename, 'utf8'));
    if (!parsed || typeof parsed !== 'object' || Array.isArray(parsed)) throw new Error('invalid MCP config');
    return parsed;
  };
  const base = read(baseConfigPath).mcpServers ?? {};
  const call = read(callConfigPath).mcpServers ?? {};
  for (const name of Object.keys(call)) {
    if (Object.prototype.hasOwnProperty.call(base, name)) throw new Error(`MCP server name collision: ${name}`);
  }
  const target = path.join(workspace, '.resident-call-combined-mcp.json');
  fs.writeFileSync(target, JSON.stringify({ mcpServers: { ...base, ...call } }), {
    encoding: 'utf8', mode: 0o600,
  });
  fs.chmodSync(target, 0o600);
  return target;
}
