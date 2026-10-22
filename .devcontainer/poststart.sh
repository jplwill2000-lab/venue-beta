#!/usr/bin/env bash
# Docker-in-Docker host drops container-to-container traffic when
# bridge-nf-call-iptables=1. Turn it off, then bring the stack up.
for i in $(seq 1 30); do docker info >/dev/null 2>&1 && break; sleep 2; done
sudo sysctl -w net.bridge.bridge-nf-call-iptables=0 || true
cd "$(dirname "$0")/.." && docker compose up -d
