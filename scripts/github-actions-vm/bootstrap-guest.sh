#!/usr/bin/env bash
set -euo pipefail

install -d -m 0755 -o root -g root /etc/ssh/sshd_config.d
cat > /etc/ssh/sshd_config.d/60-blueprint-ci.conf <<'EOF'
PasswordAuthentication no
KbdInteractiveAuthentication no
PermitRootLogin no
PermitEmptyPasswords no
X11Forwarding no
AllowAgentForwarding no
AllowTcpForwarding no
DisableForwarding yes
PermitTunnel no
EOF

install -d -m 0755 -o root -g root /var/lib/blueprint-actions
install -d -m 0755 -o root -g root /run/blueprint-actions
install -d -m 0755 -o root -g root /etc/systemd/system
install -m 0644 -o root -g root \
  /usr/local/lib/blueprint/github-actions-vm/blueprint-runner-network@.service \
  /etc/systemd/system/blueprint-runner-network@.service
install -m 0644 -o root -g root \
  /usr/local/lib/blueprint/github-actions-vm/blueprint-dockerd@.service \
  /etc/systemd/system/blueprint-dockerd@.service
install -m 0644 -o root -g root \
  /usr/local/lib/blueprint/github-actions-vm/blueprint-github-actions@.service \
  /etc/systemd/system/blueprint-github-actions@.service
/usr/local/lib/blueprint/github-actions-vm/install-volume-maintenance.sh

systemctl mask --now docker.service docker.socket containerd.service
systemctl daemon-reload
systemctl restart ssh

cat > /var/lib/blueprint-actions/BOOTSTRAP_COMPLETE <<'EOF'
GitHub Actions execution is KVM-isolated.
Runner registration is intentionally a separate short-lived-token step.
EOF
chmod 0644 /var/lib/blueprint-actions/BOOTSTRAP_COMPLETE
