#!/bin/bash
set -e

echo "Starting Agent VM Initialization..."

# 1. Self-Destruct Mechanism (Defense in Depth)
# Forcefully power off the VM after 1 hour (3600 seconds) to prevent runaway costs
# if the orchestrator loses connection or the agent hangs.
(sleep 3600 && echo "Maximum TTL reached. Self-terminating..." && poweroff) &

# 2. Check for required environment variables
if [ -z "$VM_INSTANCE_ID" ] || [ -z "$ORCHESTRATOR_URL" ] || [ -z "$AGENT_SECRET_TOKEN" ]; then
  echo "Error: Missing required environment variables."
  echo "VM_INSTANCE_ID, ORCHESTRATOR_URL, and AGENT_SECRET_TOKEN must be set."
  exit 1
fi

# 3. Execute the actual agent script
echo "Running agent..."
cd /agent
npm start

# 4. Graceful self-termination upon agent script completion
echo "Agent finished. Terminating VM..."
poweroff
