#!/usr/bin/env bash
set -euo pipefail

if ! command -v claude >/dev/null 2>&1; then
  echo "claude-glm: the underlying 'claude' CLI was not found on PATH." >&2
  exit 127
fi

case "${1:-}" in
  --help | -h | help | --version | version)
    exec claude "$@"
    ;;
esac

resolve_token() {
  if [[ -n "${ANTHROPIC_AUTH_TOKEN:-}" ]]; then
    printf '%s' "$ANTHROPIC_AUTH_TOKEN"
    return 0
  fi

  if command -v security >/dev/null 2>&1; then
    security find-generic-password \
      -a "${USER:-$(id -un)}" \
      -s "${ZAI_KEYCHAIN_SERVICE:-zai-coding-plan-key}" \
      -w 2>/dev/null || true
  fi
}

token="$(resolve_token)"
if [[ -z "$token" ]]; then
  cat >&2 <<'MSG'
claude-glm: missing GLM Coding Plan key.

Set ANTHROPIC_AUTH_TOKEN for this process, or on macOS store the key with:

  printf "Z.AI Coding Plan key: "
  stty -echo
  IFS= read -r ZAI_CODING_PLAN_KEY
  stty echo
  printf "\n"
  security add-generic-password -U -a "$USER" -s zai-coding-plan-key -w "$ZAI_CODING_PLAN_KEY"
  unset ZAI_CODING_PLAN_KEY
MSG
  exit 2
fi

export ANTHROPIC_AUTH_TOKEN="$token"
export ANTHROPIC_BASE_URL="${ANTHROPIC_BASE_URL:-https://api.z.ai/api/anthropic}"
export ANTHROPIC_DEFAULT_HAIKU_MODEL="${ANTHROPIC_DEFAULT_HAIKU_MODEL:-glm-4.7}"
export ANTHROPIC_DEFAULT_SONNET_MODEL="${ANTHROPIC_DEFAULT_SONNET_MODEL:-glm-5.2[1m]}"
export ANTHROPIC_DEFAULT_OPUS_MODEL="${ANTHROPIC_DEFAULT_OPUS_MODEL:-glm-5.2[1m]}"
export CLAUDE_CODE_AUTO_COMPACT_WINDOW="${CLAUDE_CODE_AUTO_COMPACT_WINDOW:-1000000}"
export CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC="${CLAUDE_CODE_DISABLE_NONESSENTIAL_TRAFFIC:-1}"
export API_TIMEOUT_MS="${API_TIMEOUT_MS:-3000000}"

exec claude "$@"
