#!/usr/bin/env sh
set -eu

npx --yes skills@1.8.1 remove --global cloudflare grill-me --yes
exec npx --yes skills@1.8.1 add niebloomj/Skills --all --global --copy --force
