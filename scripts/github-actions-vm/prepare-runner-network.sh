#!/usr/bin/env bash
set -euo pipefail

index="${1:?runner index is required}"
[[ "$index" =~ ^[0-9]+$ ]] && (( index >= 1 && index <= 16 ))

namespace="blueprint-actions-${index}"
host_veth="bpa${index}h"
namespace_veth="bpa${index}n"
host_address="172.29.${index}.1/30"
namespace_address="172.29.${index}.2/30"
namespace_subnet="172.29.${index}.0/30"
egress_interface="$(
  ip -4 route show default | awk 'NR == 1 { for (i = 1; i <= NF; i++) if ($i == "dev") print $(i + 1) }'
)"
[[ "$egress_interface" =~ ^[A-Za-z0-9_.:-]{1,15}$ ]]

if ! ip netns exec "$namespace" true >/dev/null 2>&1; then
  ip netns add "$namespace"
fi

if ! ip link show dev "$host_veth" >/dev/null 2>&1; then
  ip link add "$host_veth" type veth peer name "$namespace_veth"
  ip link set "$namespace_veth" netns "$namespace"
fi
ip netns exec "$namespace" ip link show dev "$namespace_veth" >/dev/null

ip address replace "$host_address" dev "$host_veth"
ip link set dev "$host_veth" up
ip netns exec "$namespace" ip address replace "$namespace_address" dev "$namespace_veth"
ip netns exec "$namespace" ip link set dev "$namespace_veth" up
ip netns exec "$namespace" ip link set dev lo up
ip netns exec "$namespace" ip route replace default via "172.29.${index}.1"
ip netns exec "$namespace" sysctl -q -w net.ipv6.conf.all.disable_ipv6=1

sysctl -q -w net.ipv4.ip_forward=1
iptables -P FORWARD DROP
iptables -C INPUT -i "$host_veth" -j REJECT >/dev/null 2>&1 \
  || iptables -I INPUT 1 -i "$host_veth" -j REJECT
iptables -C FORWARD -i "$host_veth" -o "$egress_interface" -j ACCEPT >/dev/null 2>&1 \
  || iptables -A FORWARD -i "$host_veth" -o "$egress_interface" -j ACCEPT
iptables -C FORWARD -i "$egress_interface" -o "$host_veth" \
  -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT >/dev/null 2>&1 \
  || iptables -A FORWARD -i "$egress_interface" -o "$host_veth" \
    -m conntrack --ctstate ESTABLISHED,RELATED -j ACCEPT
iptables -t nat -C POSTROUTING -s "$namespace_subnet" -o "$egress_interface" \
  -j MASQUERADE >/dev/null 2>&1 \
  || iptables -t nat -A POSTROUTING -s "$namespace_subnet" -o "$egress_interface" \
    -j MASQUERADE
ip6tables -C INPUT -i "$host_veth" -j REJECT >/dev/null 2>&1 \
  || ip6tables -I INPUT 1 -i "$host_veth" -j REJECT
ip6tables -C FORWARD -i "$host_veth" -j REJECT >/dev/null 2>&1 \
  || ip6tables -I FORWARD 1 -i "$host_veth" -j REJECT
