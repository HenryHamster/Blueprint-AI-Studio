#!/usr/bin/env node
// Credential-free stdio transport for a trusted Resident MCP broker.

import net from 'node:net';
import path from 'node:path';
import process from 'node:process';
import { clearTimeout, setTimeout } from 'node:timers';

const socketPath = process.argv[2] ?? '';
if (
  !path.isAbsolute(socketPath)
  || !/^blueprint-resident-mcp-[A-Za-z0-9_.-]+\.sock$/.test(path.basename(socketPath))
  || !path.basename(path.dirname(socketPath)).startsWith('blueprint-rmcp-')
) {
  process.stderr.write('resident_mcp_client: invalid private socket path\n');
  process.exit(126);
}

const deadline = Date.now() + 5_000;
let connected = false;
let failed = false;
let activeSocket = null;
const deadlineTimer = setTimeout(() => {
  if (connected || failed) return;
  failed = true;
  activeSocket?.destroy();
  process.stderr.write('resident_mcp_client: broker connection timed out\n');
  process.exitCode = 126;
  process.stdin.destroy();
}, 5_000);

function connect() {
  let attemptConnected = false;
  const socket = net.createConnection({ path: socketPath });
  activeSocket = socket;
  socket.once('connect', () => {
    attemptConnected = true;
    connected = true;
    failed = false;
    clearTimeout(deadlineTimer);
    process.stdin.pipe(socket);
    socket.pipe(process.stdout);
  });
  socket.once('error', (error) => {
    socket.destroy();
    if (!connected && ['ENOENT', 'ECONNREFUSED'].includes(error.code) && Date.now() < deadline) {
      setTimeout(connect, 25);
      return;
    }
    failed = true;
    clearTimeout(deadlineTimer);
    process.stderr.write(`resident_mcp_client: broker connection failed (${error.code ?? 'unknown'})\n`);
    process.exitCode = 126;
    process.stdin.destroy();
  });
  socket.once('close', () => {
    if (attemptConnected && !failed) {
      clearTimeout(deadlineTimer);
      process.stdin.unpipe(socket);
      process.stdin.destroy();
      process.exitCode = 0;
    }
  });
}

connect();
