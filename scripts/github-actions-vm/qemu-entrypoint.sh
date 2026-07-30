#!/usr/bin/env bash
set -euo pipefail

: "${VM_CPUS:=12}"
: "${VM_MEMORY_MIB:=32768}"
: "${VM_SSH_PORT:=2222}"
: "${VM_DISK:=/vm/overlay.qcow2}"
: "${VM_SEED:=/vm/seed.img}"

[[ "$VM_CPUS" =~ ^[0-9]+$ ]] && (( VM_CPUS >= 2 && VM_CPUS <= 64 ))
[[ "$VM_MEMORY_MIB" =~ ^[0-9]+$ ]] && (( VM_MEMORY_MIB >= 4096 && VM_MEMORY_MIB <= 131072 ))
[[ "$VM_SSH_PORT" =~ ^[0-9]+$ ]] && (( VM_SSH_PORT >= 1024 && VM_SSH_PORT <= 65535 ))
[[ "$VM_DISK" == /vm/* && "$VM_DISK" != *".."* ]]
[[ "$VM_SEED" == /vm/* && "$VM_SEED" != *".."* ]]
[[ -r "$VM_DISK" && -w "$VM_DISK" && ! -L "$VM_DISK" ]]
[[ -r "$VM_SEED" && ! -L "$VM_SEED" ]]
[[ -r /dev/kvm && -w /dev/kvm ]]

exec qemu-system-x86_64 \
  -name blueprint-actions \
  -machine q35,accel=kvm \
  -cpu host \
  -smp "$VM_CPUS" \
  -m "$VM_MEMORY_MIB" \
  -nodefaults \
  -no-reboot \
  -display none \
  -serial stdio \
  -monitor none \
  -device virtio-rng-pci \
  -device virtio-balloon-pci \
  -drive "file=$VM_DISK,if=none,id=osdisk,format=qcow2,cache=none,discard=unmap" \
  -device virtio-blk-pci,drive=osdisk,bootindex=1 \
  -drive "file=$VM_SEED,if=none,id=seed,format=raw,readonly=on" \
  -device virtio-blk-pci,drive=seed \
  -device virtio-net-pci,netdev=net0 \
  -netdev "user,id=net0,hostfwd=tcp:127.0.0.1:$VM_SSH_PORT-:22"
