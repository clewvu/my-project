#!/usr/bin/env bash
# One-time server bootstrap for Ubuntu 24.04: Docker, Tailscale, the repo, the
# folders. Run as root on a fresh server:
#
#   curl -fsSL https://raw.githubusercontent.com/clewvu/my-project/claude/kalshi-crypto-bot-handoff-w39dj8/deploy/setup.sh | bash
#
# (or copy this file up and run `bash setup.sh`). It stops before anything
# that needs your secrets; deploy/README.md takes it from there.
set -euo pipefail

BRANCH="${BRANCH:-claude/kalshi-crypto-bot-handoff-w39dj8}"
REPO="${REPO:-https://github.com/clewvu/my-project.git}"
DIR="${DIR:-$HOME/kalshi-bot}"

echo "== packages"
apt-get update -q
apt-get install -y -q docker.io docker-compose-v2 git curl ufw
systemctl enable --now docker

echo "== tailscale (private network between this server and your phone)"
if ! command -v tailscale >/dev/null 2>&1; then
  curl -fsSL https://tailscale.com/install.sh | sh
fi

echo "== firewall: SSH and Tailscale only; the dashboard is never on the public internet"
ufw allow OpenSSH >/dev/null
ufw allow in on tailscale0 >/dev/null
ufw --force enable >/dev/null

echo "== repository"
if [ ! -d "$DIR/.git" ]; then
  git clone -b "$BRANCH" "$REPO" "$DIR"
fi
cd "$DIR"
mkdir -p state secrets
[ -f .env ] || cp deploy/.env.server .env
chmod 700 secrets

cat <<'NEXT'

Done. Next, from deploy/README.md:
  1. tailscale up            (prints a login link; open it once, on any device)
  2. copy the key file to secrets/kalshi-key.txt and fill in .env
  3. docker compose -f deploy/docker-compose.yml up -d
NEXT
