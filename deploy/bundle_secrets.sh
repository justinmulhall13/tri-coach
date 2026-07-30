#!/bin/bash
# Prints the `fly secrets set` command that ships your local Garmin token,
# Anthropic key, and a fresh access token to the deployed app.
# Run from anywhere:  bash coach/deploy/bundle_secrets.sh
set -euo pipefail

TOKEN_FILE="$HOME/.garminconnect/garmin_tokens.json"
ENV_FILE="$(cd "$(dirname "$0")/.." && pwd)/.env"

[[ -f "$TOKEN_FILE" ]] || { echo "❌ No Garmin token at $TOKEN_FILE — open the app locally once to create it."; exit 1; }

GARMIN_B64=$(base64 < "$TOKEN_FILE" | tr -d '\n')

# Pull the Anthropic key from your local .env if present.
ANTHROPIC=""
if [[ -f "$ENV_FILE" ]]; then
  ANTHROPIC=$(grep -E '^ANTHROPIC_API_KEY=' "$ENV_FILE" | head -1 | cut -d= -f2- | tr -d '"' || true)
fi

# A strong random access token for the phone gate (openssl avoids SIGPIPE issues).
ACCESS=$(openssl rand -hex 20)

echo "───────────────────────────────────────────────────────────────"
echo "Your access token (save it — you'll type it once on your phone):"
echo
echo "    $ACCESS"
echo
echo "Now run this to push all secrets to Fly:"
echo "───────────────────────────────────────────────────────────────"
echo
echo "fly secrets set \\"
echo "  ACCESS_TOKEN='$ACCESS' \\"
[[ -n "$ANTHROPIC" ]] && echo "  ANTHROPIC_API_KEY='$ANTHROPIC' \\"
echo "  GARMIN_TOKEN_B64='$GARMIN_B64'"
echo
[[ -z "$ANTHROPIC" ]] && echo "⚠️  ANTHROPIC_API_KEY not found in .env — add it to the command above manually."
