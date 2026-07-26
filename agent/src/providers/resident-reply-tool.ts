import fs from 'fs';
import path from 'path';
import { fileURLToPath } from 'url';

export const RESIDENT_REPLY_TOOL_NAME = 'mcp__workday__reply_to_operator';

export interface ResidentReplyToolPaths {
  receiptPath: string;
  stagedReplyPath: string;
}

export interface ProvisionedResidentReplyTool {
  configPath: string;
  toolName: typeof RESIDENT_REPLY_TOOL_NAME;
}

const moduleDir = path.dirname(fileURLToPath(import.meta.url));
const serverSourcePath = path.resolve(moduleDir, '../_agent_scripts/resident_reply_mcp.mjs');

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
  const source = fs.readFileSync(serverSourcePath, 'utf8');
  fs.writeFileSync(scriptPath, source, { encoding: 'utf8', mode: 0o700 });
  fs.chmodSync(scriptPath, 0o700);
  fs.writeFileSync(configPath, JSON.stringify({
    mcpServers: {
      workday: {
        command: process.execPath,
        args: [scriptPath],
        env: {
          RESIDENT_REPLY_RECEIPT_PATH: paths.receiptPath,
          RESIDENT_STAGED_REPLY_PATH: paths.stagedReplyPath,
        },
      },
    },
  }), { encoding: 'utf8', mode: 0o600 });
  fs.chmodSync(configPath, 0o600);
  return { configPath, toolName: RESIDENT_REPLY_TOOL_NAME };
}
