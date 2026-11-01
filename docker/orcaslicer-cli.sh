#!/bin/sh
# Wrapper around the OrcaSlicer CLI inside the orca-slicer-api container.
#
# The OrcaSlicer CLI arranges with "allow rotations" ENABLED by default (the
# desktop GUI has it disabled), so `--arrange 1` turns parts in 45-degree steps.
# orca-slicer-api has no setting for this, so we inject it here.
# Note: only `--allow-rotations=0` works; `=false` and `--no-allow-rotations` do not.
exec "${ORCASLICER_BINARY:-/app/squashfs-root/AppRun}" --allow-rotations=0 "$@"
