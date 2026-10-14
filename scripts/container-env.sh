#!/usr/bin/env bash
# Print `export DOCKER_HOST=...` for a reachable Docker-compatible engine, or nothing
# when the current Docker context already works. Usage: eval "$(scripts/container-env.sh)"
set -euo pipefail
if docker info >/dev/null 2>&1; then
  exit 0
fi
if command -v podman >/dev/null 2>&1; then
  machine="$(podman machine list --format '{{.Name}} {{.Running}}' 2>/dev/null | awk '$2=="true"{print $1; exit}' | tr -d '*')"
  if [ -n "${machine}" ]; then
    sock="$(podman machine inspect "${machine}" --format '{{.ConnectionInfo.PodmanSocket.Path}}' 2>/dev/null || true)"
    if [ -n "${sock}" ] && [ -S "${sock}" ]; then
      echo "export DOCKER_HOST=unix://${sock}"
      exit 0
    fi
  fi
fi
echo "echo 'No reachable Docker or Podman engine; start Docker Desktop or a Podman machine.' >&2; false"
