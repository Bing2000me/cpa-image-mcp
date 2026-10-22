#!/usr/bin/env bash
set -euo pipefail
cd "$(dirname "$0")"

if [[ ${EUID} -ne 0 ]]; then
  echo "Run as root: sudo bash install.sh" >&2
  exit 1
fi
if ! command -v systemctl >/dev/null || [[ ! -d /run/systemd/system ]]; then
  echo "This installer requires systemd. OpenRC/Alpine is not supported by this installer." >&2
  exit 1
fi
command -v python3 >/dev/null || { echo "Install Python 3.10+ first." >&2; exit 1; }
python3 -c 'import sys; assert sys.version_info >= (3,10), "Python 3.10+ required"'
python3 -c 'import venv, ensurepip' 2>/dev/null || {
  echo "Python venv/ensurepip is missing. On Debian/Ubuntu, install python3-venv first." >&2
  exit 1
}

env_file=/etc/cpa-image-mcp.env
if [[ ! -f "$env_file" ]]; then
  python3 scripts/configure_env.py prepare --template .env.example --env-file "$env_file"
  echo "Edit $env_file: fill CPA_API_KEY and PUBLIC_HOST, then rerun this installer."
  echo "No user, application directory or service was installed on this first run."
  exit 0
fi
python3 scripts/configure_env.py prepare --template .env.example --env-file "$env_file"
python3 scripts/configure_env.py validate --env-file "$env_file"
chmod 600 "$env_file"

install_dir=/opt/cpa-image-mcp
stamp=$(date -u +%Y%m%dT%H%M%SZ)
backup_dir=/var/backups/cpa-image-mcp/"$stamp"
install -d -m 700 "$backup_dir"
for name in server.py requirements.txt README.md; do
  if [[ -f "$install_dir/$name" ]]; then
    cp -p "$install_dir/$name" "$backup_dir/$name"
  fi
done
cp -p "$env_file" "$backup_dir/cpa-image-mcp.env"
chmod 600 "$backup_dir/cpa-image-mcp.env"
if [[ -f /etc/systemd/system/cpa-image-mcp.service ]]; then
  cp -p /etc/systemd/system/cpa-image-mcp.service "$backup_dir/cpa-image-mcp.service"
fi

id cpaimg >/dev/null 2>&1 || useradd --system --no-create-home --shell /usr/sbin/nologin cpaimg
install -d -m 755 "$install_dir"
install -m 644 server.py requirements.txt README.md "$install_dir/"
python3 -m venv "$install_dir/venv"
"$install_dir/venv/bin/python" -m pip install -q -r "$install_dir/requirements.txt"
install -m 644 cpa-image-mcp.service /etc/systemd/system/cpa-image-mcp.service
systemctl daemon-reload
systemctl enable cpa-image-mcp
systemctl restart cpa-image-mcp
if ! python3 scripts/configure_env.py health --env-file "$env_file"; then
  echo "Health check failed. Inspect: journalctl -u cpa-image-mcp -n 50" >&2
  echo "Previous files, when present, are in $backup_dir" >&2
  exit 1
fi
echo "cpa-image-mcp is active. Backup: $backup_dir"
echo "Read $env_file locally for the connector's Authorization header; secrets are not printed."
echo "Configure your existing HTTPS tunnel/proxy separately; no firewall or CPA settings were changed."
