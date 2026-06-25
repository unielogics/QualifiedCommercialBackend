#!/usr/bin/env bash
# One-shot bootstrap for the qcbackend EC2 instance (Ubuntu, ARM).
#
# Usage:
#   curl -fsSL https://raw.githubusercontent.com/unielogics/QualifiedCommercialBackend/main/deploy/ec2-bootstrap.sh \
#     | sudo -E bash
#
# Idempotent — safe to re-run.

set -euo pipefail

REGION="${AWS_REGION:-us-east-1}"
SECRET_ID="${SECRET_ID:-qcbackend/prod}"
DOMAIN="${DOMAIN:-api.qualifiedcommercial.com}"
GHCR_IMAGE="${GHCR_IMAGE:-ghcr.io/unielogics/qcbackend:latest}"
LOCAL_TAG="qcbackend:current"
REPO_URL="${REPO_URL:-https://github.com/unielogics/QualifiedCommercialBackend.git}"
REPO_DIR="${REPO_DIR:-/opt/qcbackend-src}"
ENV_FILE="/etc/qcbackend.env"

echo "==> System packages"
apt-get update -y
DEBIAN_FRONTEND=noninteractive apt-get install -y \
  ca-certificates curl gnupg lsb-release jq unzip awscli git \
  postgresql-client \
  debian-keyring debian-archive-keyring apt-transport-https snapd

echo "==> SSM agent (so GitHub Actions can deploy via SSM Send-Command)"
if ! systemctl is-active --quiet snap.amazon-ssm-agent.amazon-ssm-agent; then
  snap install amazon-ssm-agent --classic || true
  systemctl enable --now snap.amazon-ssm-agent.amazon-ssm-agent || true
fi

echo "==> Docker"
if ! command -v docker > /dev/null; then
  install -m 0755 -d /etc/apt/keyrings
  curl -fsSL https://download.docker.com/linux/ubuntu/gpg | gpg --dearmor -o /etc/apt/keyrings/docker.gpg
  chmod a+r /etc/apt/keyrings/docker.gpg
  echo "deb [arch=$(dpkg --print-architecture) signed-by=/etc/apt/keyrings/docker.gpg] https://download.docker.com/linux/ubuntu $(lsb_release -cs) stable" \
    > /etc/apt/sources.list.d/docker.list
  apt-get update -y
  apt-get install -y docker-ce docker-ce-cli containerd.io docker-compose-plugin
  systemctl enable --now docker
fi

echo "==> Caddy"
if ! command -v caddy > /dev/null; then
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/gpg.key' | gpg --dearmor -o /usr/share/keyrings/caddy-stable-archive-keyring.gpg
  curl -1sLf 'https://dl.cloudsmith.io/public/caddy/stable/debian.deb.txt' \
    | tee /etc/apt/sources.list.d/caddy-stable.list
  apt-get update -y
  apt-get install -y caddy
fi

echo "==> Caddyfile (auto-TLS via Let's Encrypt)"
cat > /etc/caddy/Caddyfile <<EOF
${DOMAIN} {
    reverse_proxy 127.0.0.1:8000
    encode gzip
    log {
        output stdout
        format console
    }
}
EOF
systemctl enable --now caddy
systemctl reload caddy

echo "==> Pull secrets from AWS Secrets Manager → ${ENV_FILE}"
cat > /usr/local/bin/qcbackend-refresh-env <<EOF
#!/usr/bin/env bash
set -euo pipefail
aws secretsmanager get-secret-value \\
  --secret-id "${SECRET_ID}" \\
  --region "${REGION}" \\
  --query SecretString --output text \\
| jq -r 'to_entries[] | "\\(.key)=\\(.value)"' > "${ENV_FILE}.new"
chmod 600 "${ENV_FILE}.new"
chown root:root "${ENV_FILE}.new"
mv "${ENV_FILE}.new" "${ENV_FILE}"
EOF
chmod +x /usr/local/bin/qcbackend-refresh-env
/usr/local/bin/qcbackend-refresh-env

# ---------- One-time DB bootstrap on RDS ----------
echo "==> Ensure 'qc' database + pgvector extension exist on RDS"

# Extract host/user/pw from DATABASE_URL_SYNC (postgresql+psycopg://user:pw@host:port/db)
ADMIN_URL=$(grep '^DATABASE_URL_SYNC=' "${ENV_FILE}" | cut -d= -f2-)
# Strip the +psycopg dialect suffix and the trailing /qc — we connect to /postgres for admin ops
ADMIN_PG_URL=$(echo "$ADMIN_URL" \
  | sed -E 's#postgresql\+psycopg://#postgresql://#' \
  | sed -E 's#/[^/]+$#/postgres#')

if psql "$ADMIN_PG_URL" -tAc "SELECT 1 FROM pg_database WHERE datname='qc'" | grep -q 1; then
  echo "    qc database already exists"
else
  echo "    creating qc database"
  psql "$ADMIN_PG_URL" -c "CREATE DATABASE qc;"
fi

QC_PG_URL=$(echo "$ADMIN_PG_URL" | sed -E 's#/postgres$#/qc#')
psql "$QC_PG_URL" -c "CREATE EXTENSION IF NOT EXISTS vector;"
echo "    pgvector extension ensured on qc database"

# ---------- Image: pull from GHCR if available, else build locally ----------
echo "==> Resolve container image (${LOCAL_TAG})"
GHCR_AVAILABLE=0
if docker pull "${GHCR_IMAGE}" 2>/dev/null; then
  echo "    pulled ${GHCR_IMAGE}"
  docker tag "${GHCR_IMAGE}" "${LOCAL_TAG}"
  GHCR_AVAILABLE=1
else
  echo "    GHCR image not available yet — building locally from main branch"
  rm -rf "${REPO_DIR}"
  git clone --depth 1 "${REPO_URL}" "${REPO_DIR}"
  docker build -t "${LOCAL_TAG}" "${REPO_DIR}"
fi

# ---------- systemd unit ----------
echo "==> systemd unit for the qcbackend container"
cat > /etc/systemd/system/qcbackend.service <<EOF
[Unit]
Description=Qualified Commercial backend container
Requires=docker.service
After=docker.service network-online.target
StartLimitIntervalSec=60
StartLimitBurst=3

[Service]
Type=simple
Restart=always
RestartSec=5
ExecStartPre=-/usr/bin/docker stop qcbackend
ExecStartPre=-/usr/bin/docker rm qcbackend
ExecStartPre=/usr/local/bin/qcbackend-refresh-env
ExecStart=/usr/bin/docker run --rm --name qcbackend \\
  -p 127.0.0.1:8000:8000 \\
  --env-file ${ENV_FILE} \\
  --health-cmd="python -c 'import urllib.request; urllib.request.urlopen(\"http://127.0.0.1:8000/ready\", timeout=3).read()'" \\
  --health-interval=30s \\
  --health-timeout=5s \\
  --health-retries=3 \\
  ${LOCAL_TAG}
ExecStop=/usr/bin/docker stop qcbackend

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable qcbackend.service

# ---------- Daily secret refresh ----------
echo "==> Daily refresh of secrets from Secrets Manager (in case you rotate)"
cat > /etc/cron.daily/qcbackend-refresh-env <<EOF
#!/usr/bin/env bash
set -euo pipefail
/usr/local/bin/qcbackend-refresh-env
if ! cmp -s "${ENV_FILE}" "${ENV_FILE}.last-restarted" 2>/dev/null; then
  cp "${ENV_FILE}" "${ENV_FILE}.last-restarted"
  systemctl restart qcbackend
fi
EOF
chmod +x /etc/cron.daily/qcbackend-refresh-env

echo
echo "==> Bootstrap complete."
echo "    Image source:            $([ ${GHCR_AVAILABLE} -eq 1 ] && echo 'GHCR' || echo 'locally built')"
echo "    Start the backend now:   systemctl start qcbackend"
echo "    Tail logs:               journalctl -u qcbackend -f"
echo "    Caddy status:            systemctl status caddy"
echo "    Health check:            curl -k https://${DOMAIN}/"
