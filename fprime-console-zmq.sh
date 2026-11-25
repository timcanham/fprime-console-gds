#!/usr/bin/env bash
# fprime-console-zmq.sh:
#
# Convenience launcher that starts `fprime-gds` (owning the comm/middleware layer) and
# `fprime-console-gds` (the terminal UI), wired together over a ZeroMQ transport whose pipe
# names are unique to this script invocation. This avoids collisions with other instances
# run by the same user, or other tools using fprime-gds's default ZMQ pipe names.
#
# Usage: fprime-console-zmq.sh [arguments passed to both fprime-gds and fprime-console-gds]
#
# Example:
#   fprime-console-zmq.sh --dictionary build-artifacts/.../my_deployment.json --comm-adapter ip --ip-address 192.168.1.50 --ip-port 50000

set -euo pipefail

ZMQ_TAG="$(id -un)-$$"
ZMQ_IN="ipc:///tmp/fprime-console-zmq-${ZMQ_TAG}-in"
ZMQ_OUT="ipc:///tmp/fprime-console-zmq-${ZMQ_TAG}-out"

GDS_PID=""
TUI_EXIT=0

cleanup() {
    if [[ -n "$GDS_PID" ]] && kill -0 "$GDS_PID" 2>/dev/null; then
        kill "$GDS_PID" 2>/dev/null || true
        wait "$GDS_PID" 2>/dev/null || true
    fi
    rm -f "${ZMQ_IN#ipc://}" "${ZMQ_OUT#ipc://}" 2>/dev/null || true
}
trap cleanup EXIT INT TERM

fprime-gds --gui none --data-transport-selection zmq --zmq-transport "$ZMQ_IN" "$ZMQ_OUT" "$@" &
GDS_PID=$!

fprime-console-gds --data-transport-selection zmq --zmq-transport "$ZMQ_IN" "$ZMQ_OUT" "$@" || TUI_EXIT=$?

exit "$TUI_EXIT"
