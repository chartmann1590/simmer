#!/usr/bin/env bash
# Deploy the Simmer community server to Cloudflare's free tier (Workers + D1 + Workers AI).
# Run it yourself, once, from a terminal where you're signed in to Cloudflare:  bash community/deploy.sh
# It creates the D1 database (if needed), applies the schema, sets a random hash salt, and deploys.
set -euo pipefail
cd "$(dirname "$0")"
W="npx --yes wrangler@3"
$W whoami >/dev/null 2>&1 || $W login
if grep -q REPLACED_BY_DEPLOY_SH wrangler.toml; then
  id=$($W d1 list --json | python3 -c 'import json,sys; print(next((d["uuid"] for d in json.load(sys.stdin) if d["name"]=="simmer-community"),""))')
  if [ -z "$id" ]; then
    id=$($W d1 create simmer-community | sed -n 's/.*database_id *= *"\([^"]*\)".*/\1/p')
  fi
  [ -n "$id" ] || { echo "Couldn't create or find the D1 database"; exit 1; }
  sed -i.bak "s/REPLACED_BY_DEPLOY_SH/$id/" wrangler.toml && rm -f wrangler.toml.bak
fi
$W d1 execute simmer-community --remote --file schema.sql
if ! $W secret list 2>/dev/null | grep -q HASH_SALT; then
  python3 -c 'import secrets; print(secrets.token_hex(32))' | $W secret put HASH_SALT
fi
$W deploy
echo
echo "Check it:  curl -s https://simmer-community.<your-subdomain>.workers.dev/v1/health"
