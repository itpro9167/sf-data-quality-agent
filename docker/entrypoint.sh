#!/bin/sh
# Container entrypoint for the public demo: seed sample data once, then serve the dashboard.
set -eu

# Builds the fake org and a queue of pending proposals on first start (no-op if already present).
sfdq seed-demo

# Render (and most PaaS) inject $PORT; fall back to 8000 for plain `docker run`.
exec sfdq review --host 0.0.0.0 --port "${PORT:-8000}"
