#!/usr/bin/env bash
# Install the browser-half dependencies (React is only used for slot shells).
set -euo pipefail
cd "$(dirname "$0")/.."
export PATH="/home/callmesoul/.nvm/versions/node/v24.22.1/bin:$PATH"
pnpm add -D react@18 react-dom@18 @types/react@18 @types/react-dom@18