FROM ubuntu@sha256:52df9b1ee71626e0088f7d400d5c6b5f7bb916f8f0c82b474289a4ece6cf3faf

ENV DEBIAN_FRONTEND=noninteractive

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && sed -i 's|http://|https://|g' /etc/apt/sources.list.d/ubuntu.sources \
    && apt-get update \
    && apt-get install -y --no-install-recommends \
        cloud-image-utils \
        iproute2 \
        iptables \
        openssh-client \
        qemu-system-x86 \
        qemu-utils \
        tini \
    && rm -rf /var/lib/apt/lists/*

COPY qemu-entrypoint.sh /usr/local/bin/blueprint-qemu
COPY firewall-entrypoint.sh /usr/local/bin/blueprint-vm-firewall

RUN install -d -m 0755 /vm \
    && chmod 0755 \
      /usr/local/bin/blueprint-qemu \
      /usr/local/bin/blueprint-vm-firewall

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["/usr/local/bin/blueprint-qemu"]
