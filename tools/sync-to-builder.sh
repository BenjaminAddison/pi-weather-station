#!/bin/bash
# Copy this repo into the image builder container (docs/image-build.md), excluding local junk.
#   BUILDER_HOST=user@host tools/sync-to-builder.sh
# The container's /work/repo is where image/build.sh expects the repo.
set -euo pipefail
HOST="${BUILDER_HOST:?set BUILDER_HOST=user@host (the machine running the weatherpi-builder container)}"
DEST=/work/repo
cd "$(dirname "$0")/.."
tar --exclude=.git --exclude=.venv --exclude=__pycache__ --exclude=dist --exclude=build \
    --exclude='installer/vendor/*' -czf - . \
  | ssh "$HOST" "docker exec -i weatherpi-builder sh -c 'rm -rf $DEST && mkdir -p $DEST && tar -xzmf - -C $DEST'"
echo "synced to weatherpi-builder:$DEST on $HOST"
