#!/usr/bin/env node

import fs from 'node:fs';
import process from 'node:process';
import { setInterval } from 'node:timers';

const tokenPath = process.env.AGENT_TOKEN_FILE;
if (!tokenPath) throw new Error('AGENT_TOKEN_FILE is required');
const token = fs.readFileSync(tokenPath, 'utf8');

process.stdin.setEncoding('utf8');
let buffered = '';
let exitNonzero = false;
process.stdin.on('data', (chunk) => {
  buffered += chunk;
  while (buffered.includes('\n')) {
    const newline = buffered.indexOf('\n');
    const request = buffered.slice(0, newline);
    buffered = buffered.slice(newline + 1);
    process.stdout.write(`${JSON.stringify({ request, token })}\n`);
    if (request === 'hold-open') setInterval(() => undefined, 1_000);
    if (request === 'exit-nonzero') exitNonzero = true;
  }
});
process.stdin.on('end', () => {
  if (exitNonzero) process.exitCode = 7;
});
