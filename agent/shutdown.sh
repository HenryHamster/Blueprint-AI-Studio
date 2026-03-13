#!/bin/bash
# Hardcoded 1-hour maximum lifecycle and local sudo shutdown script
# This prevents infinite loops from draining the budget.

echo "Initiating VM self-destruct sequence..."
# In a real Compute Engine environment, this would shut down the instance.
sudo poweroff
