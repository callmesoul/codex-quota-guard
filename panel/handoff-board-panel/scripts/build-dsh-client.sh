#!/usr/bin/env bash
# Type-check the panel and build the DSH client bundle into dsh-plugin/lib.
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="/home/callmesoul/.nvm/versions/node/v24.22.1/bin:$PATH"
npx vue-tsc -b
npx vite build --config vite.dsh.config.ts
node scripts/wrap-dsh-client.mjs