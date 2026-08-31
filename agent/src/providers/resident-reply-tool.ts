import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';
import { cliOpInput, cliOps } from '../handlers/strategic-ops-registry.js';

export const RESIDENT_REPLY_TOOL_NAME = 'mcp__workday__reply_to_operator';
export const RESIDENT_STRATEGIC_TOOL_NAME = 'mcp__workday__strategic_operation';
export const RESIDENT_PROJECT_READ_TOOL_NAME = 'mcp__workday__project_read';

export interface ResidentStrategicToolConfig {
  helperPath: string;
  readHelperPath: string;
  readVerbs: readonly string[];
  orchestratorUrl: string;
  agentTokenFile: string;
  incarnationId: string;
  replyKeyPath: string;
  replyReceiptPath: string;
  stagedReplyPath: string;
  privateTurnMarkerPath: string;
}

export interface ResidentReplyToolPaths {
  receiptPath: string;
  stagedReplyPath: string;
  strategic?: ResidentStrategicToolConfig;
}

export interface ProvisionedResidentReplyTool {
  configPath: string;
  toolName: typeof RESIDENT_REPLY_TOOL_NAME;
  strategicToolName?: typeof RESIDENT_STRATEGIC_TOOL_NAME;
  projectReadToolName?: typeof RESIDENT_PROJECT_READ_TOOL_NAME;
}

const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const serverSourcePath = path.resolve(moduleDir, '../_agent_scripts/resident_reply_mcp.mjs');
const RESIDENT_STRATEGIC_EXCLUDED_OPS = new Set(['create_message', 'resolve_message']);

/**
 * Writes the small stdio MCP server into the Resident's workspace and returns
 * its isolated Claude Code configuration.  The config contains only the
 * receipt/staging paths for this worker; it never carries API credentials.
 */
export function provisionResidentReplyTool(
  workspace: string,
  paths: ResidentReplyToolPaths,
): ProvisionedResidentReplyTool {
  const scriptPath = path.join(workspace, '.resident-reply-mcp.mjs');
  const configPath = path.join(workspace, '.resident-reply-mcp.json');
  const opsPath = path.join(workspace, '.resident-strategic-ops.json');
  const readsPath = path.join(workspace, '.resident-project-reads.json');
  const source = fs.readFileSync(serverSourcePath, 'utf8');
  fs.writeFileSync(scriptPath, source, { encoding: 'utf8', mode: 0o700 });
  fs.chmodSync(scriptPath, 0o700);
  if (paths.strategic) {
    const contracts = Object.fromEntries(cliOps()
      .filter((entry) => !RESIDENT_STRATEGIC_EXCLUDED_OPS.has(entry.op))
      .map((entry) => {
      if (!entry.alias) throw new Error(`CLI strategic op ${entry.op} has no alias`);
      const input = cliOpInput(entry.op);
      if (!input) throw new Error(`CLI strategic op ${entry.op} has no input contract`);
      return [entry.alias, {
        backend_op: entry.op,
        required: input.required ?? [],
        fields: input.fields ?? [],
        requirements: input.requirements ?? [],
        ...(input.example ? { example: input.example } : {}),
      }];
      }));
    fs.writeFileSync(opsPath, JSON.stringify(contracts), { encoding: 'utf8', mode: 0o600 });
    fs.chmodSync(opsPath, 0o600);
    fs.writeFileSync(readsPath, JSON.stringify([...paths.strategic.readVerbs]), { encoding: 'utf8', mode: 0o600 });
    fs.chmodSync(readsPath, 0o600);
  }
  const strategicEnv = paths.strategic ? {
    RESIDENT_STRATEGIC_HELPER_PATH: paths.strategic.helperPath,
    RESIDENT_STRATEGIC_OPS_PATH: opsPath,
    RESIDENT_PROJECT_READ_HELPER_PATH: paths.strategic.readHelperPath,
    RESIDENT_PROJECT_READS_PATH: readsPath,
    RESIDENT_STRATEGIC_PATH: '/usr/local/bin:/usr/bin:/bin',
    RESIDENT_STRATEGIC_LANG: process.env.LANG ?? 'C.UTF-8',
    RESIDENT_STRATEGIC_TERM: process.env.TERM ?? 'dumb',
    ORCHESTRATOR_URL: paths.strategic.orchestratorUrl,
    AGENT_TOKEN_FILE: paths.strategic.agentTokenFile,
    BLUEPRINT_RESIDENT_INCARNATION_ID: paths.strategic.incarnationId,
    BLUEPRINT_RESIDENT_REPLY_KEY_PATH: paths.strategic.replyKeyPath,
    BLUEPRINT_RESIDENT_REPLY_RECEIPT_PATH: paths.strategic.replyReceiptPath,
    BLUEPRINT_RESIDENT_STAGED_REPLY_PATH: paths.strategic.stagedReplyPath,
    BLUEPRINT_RESIDENT_PRIVATE_TURN_MARKER_PATH: paths.strategic.privateTurnMarkerPath,
  } : {};
  fs.writeFileSync(configPath, JSON.stringify({
    mcpServers: {
      workday: {
        command: process.execPath,
        args: [scriptPath],
        env: {
          RESIDENT_REPLY_RECEIPT_PATH: paths.receiptPath,
          RESIDENT_STAGED_REPLY_PATH: paths.stagedReplyPath,
          ...strategicEnv,
        },
      },
    },
  }), { encoding: 'utf8', mode: 0o600 });
  fs.chmodSync(configPath, 0o600);
  return {
    configPath,
    toolName: RESIDENT_REPLY_TOOL_NAME,
    ...(paths.strategic ? { strategicToolName: RESIDENT_STRATEGIC_TOOL_NAME } : {}),
    ...(paths.strategic ? { projectReadToolName: RESIDENT_PROJECT_READ_TOOL_NAME } : {}),
  };
}
