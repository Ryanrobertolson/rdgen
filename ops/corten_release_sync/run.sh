#!/usr/bin/env bash
set -euo pipefail

SYNC_ROOT="/app/data/stacks/Oracle VPS/rdgen"
IMAGE="corten-release-sync:1"

test -n "${CREDENTIALS_DIRECTORY:-}"
test -r "$CREDENTIALS_DIRECTORY/corten-auth"
test -r "$CREDENTIALS_DIRECTORY/rdgen-auth"
test -r "$SYNC_ROOT/release-sync-config/build-settings.json"

exec docker run --rm --init \
  --name corten-release-sync \
  --user "$(id -u):$(id -g)" \
  --memory 2g --cpus 1.5 --pids-limit 200 --shm-size 1g \
  --cap-drop ALL --security-opt no-new-privileges:true \
  --mount "type=bind,src=$SYNC_ROOT/rdgen-auth/ops/corten_release_sync,dst=/app,readonly" \
  --mount "type=bind,src=$SYNC_ROOT/release-sync-config,dst=/config,readonly" \
  --mount "type=bind,src=$SYNC_ROOT/release-sync-state,dst=/state" \
  --mount "type=bind,src=$CREDENTIALS_DIRECTORY/corten-auth,dst=/run/secrets/corten-auth,readonly" \
  --mount "type=bind,src=$CREDENTIALS_DIRECTORY/rdgen-auth,dst=/run/secrets/rdgen-auth,readonly" \
  "$IMAGE" "$@"
