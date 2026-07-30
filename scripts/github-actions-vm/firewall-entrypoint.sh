#!/usr/bin/env bash
set -euo pipefail

# QEMU joins this container's network namespace without NET_ADMIN.  Keep the
# privileged firewall process tiny and give it no host mounts or devices.
iptables -P INPUT DROP
iptables -P FORWARD DROP
iptables -P OUTPUT ACCEPT
iptables -F
ip6tables -P INPUT DROP
ip6tables -P FORWARD DROP
ip6tables -P OUTPUT DROP
ip6tables -F

iptables -A INPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A INPUT -i lo -j ACCEPT
iptables -A OUTPUT -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -A OUTPUT -o lo -j ACCEPT
for destination in \
  10.0.0.0/8 \
  100.64.0.0/10 \
  169.254.0.0/16 \
  172.16.0.0/12 \
  192.168.0.0/16 \
  224.0.0.0/4
do
  iptables -A OUTPUT -d "$destination" -j REJECT
done

exec sleep infinity
