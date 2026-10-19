# ADR 0008: Container runtime on the development machine

Status: accepted (2026-10-14)

## Context
The development machine has the Docker CLI (29.8.1) and Compose (5.5.1) but Docker
Desktop is not running. A Podman 5.7.1 machine is running and serves a Docker-compatible
API socket. A Homebrew PostgreSQL 16 on port 5432 hosts unrelated projects.

## Decision
- Compose files target the Docker Compose specification and run with either Docker or
  Podman's Docker-compatible socket. `scripts/container-env.sh` selects a working engine:
  the current Docker context if reachable, otherwise the Podman machine socket.
- OpsBridge PostgreSQL runs in its own container on host port 5433. The Homebrew server on
  5432 is never used or modified.

## Consequences
- Verified locally only on macOS arm64 with Podman. Linux Docker is expected to work but is
  verified only when CI runs remotely.
