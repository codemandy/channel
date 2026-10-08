#!/usr/bin/env bash
# Give Channel its R2 access key: sets R2_ACCOUNT_ID, R2_BUCKET and (as Secrets)
# R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY on the Vercel project (Production), writes
# all four R2_* values to .env.local, and redeploys. publish.py (and so the Mac
# app) reads .env.local to upload the favorites, so run this on each Mac that
# publishes; on a second Mac answer the prompts and skip the redeploy with
# --local-only.
# The key comes from Cloudflare → R2 → Manage API tokens (Object Read & Write
# on the innercity-life bucket); the archive's and artdoc's token works too. Values are read
# with hidden input and passed on stdin, so they never reach the shell history,
# the process list or the terminal.
set -euo pipefail

APP="$(cd "$(dirname "$0")/.." && pwd)"
ENV_FILE="$APP/.env.local"

# Same dialog as ../inner-city.life/scripts/set-password.sh.
ask() { # prompt, hidden (yes/no), default
  local hidden=""; [[ "$2" == yes ]] && hidden="with hidden answer"
  if command -v osascript >/dev/null; then
    osascript -e "text returned of (display dialog \"$1\" default answer \"$3\" $hidden with title \"innercity-life.com · channel\")"
  else
    local value
    if [[ "$2" == yes ]]; then read -rs -p "$1: " value; echo >&2; else read -r -p "$1 [$3]: " value; fi
    printf '%s' "${value:-$3}"
  fi
}

current() { [[ -f "$ENV_FILE" ]] && grep -E "^$1=" "$ENV_FILE" | tail -1 | cut -d= -f2- | tr -d '"' || true; }

ACCOUNT_ID="$(ask "Cloudflare account ID (R2 → Overview, right-hand side)" no "$(current R2_ACCOUNT_ID)")"
BUCKET="$(ask "R2 bucket" no "$(current R2_BUCKET || true)")"; BUCKET="${BUCKET:-innercity-life}"
KEY_ID="$(ask "R2 Access Key ID" yes "")"
SECRET="$(ask "R2 Secret Access Key" yes "")"
[[ -n "$ACCOUNT_ID" && -n "$KEY_ID" && -n "$SECRET" ]] || { echo "Missing a value, nothing changed."; exit 1; }

LOCAL_ONLY=""; [[ "${1:-}" == "--local-only" ]] && LOCAL_ONLY=1

if [[ -z "$LOCAL_ONLY" ]]; then
  echo "→ channel: setting R2_ACCOUNT_ID, R2_BUCKET, R2_ACCESS_KEY_ID and R2_SECRET_ACCESS_KEY"
  printf '%s' "$ACCOUNT_ID" | vercel env add R2_ACCOUNT_ID production --force --yes --cwd "$APP" >/dev/null
  printf '%s' "$BUCKET" | vercel env add R2_BUCKET production --force --yes --cwd "$APP" >/dev/null
  printf '%s' "$KEY_ID" | vercel env add R2_ACCESS_KEY_ID production --force --sensitive --yes --cwd "$APP" >/dev/null
  printf '%s' "$SECRET" | vercel env add R2_SECRET_ACCESS_KEY production --force --sensitive --yes --cwd "$APP" >/dev/null
fi

echo "→ .env.local: writing the R2_* values for publish.py"
touch "$ENV_FILE"
tmp="$(mktemp)"
grep -vE '^R2_(ACCOUNT_ID|BUCKET|ACCESS_KEY_ID|SECRET_ACCESS_KEY)=' "$ENV_FILE" > "$tmp" || true
{ printf 'R2_ACCOUNT_ID=%s\nR2_BUCKET=%s\nR2_ACCESS_KEY_ID=%s\nR2_SECRET_ACCESS_KEY=%s\n' "$ACCOUNT_ID" "$BUCKET" "$KEY_ID" "$SECRET"; } >> "$tmp"
mv "$tmp" "$ENV_FILE"; chmod 600 "$ENV_FILE"
unset KEY_ID SECRET

[[ -n "$LOCAL_ONLY" ]] && { echo "Done. publish.py can upload from this Mac now."; exit 0; }
echo "→ channel: redeploying"
vercel deploy --prod --yes --cwd "$APP" >/dev/null
echo "Done. Open https://channel.innercity-life.com"
