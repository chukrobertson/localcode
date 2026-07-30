#!/bin/sh
set -eu

ROOT=$(CDPATH= cd -- "$(dirname -- "$0")/.." && pwd)
export PYTHONPATH="$ROOT${PYTHONPATH:+:$PYTHONPATH}"

python3 -c 'from localcode.memory import MemPalaceManager; status = MemPalaceManager().install(print); print(status.version or status.detail)'
