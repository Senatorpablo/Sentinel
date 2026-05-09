#!/bin/bash
# Sentinel deploy script

set -e

echo "=== Sentinel Deploy ==="
cd /opt/sentinel

# Pull latest
git pull origin main 2>/dev/null || true

# Restart services
systemctl daemon-reload
systemctl restart sentinel.service
systemctl restart sentinel-api.service

echo "=== Sentinel restarted ==="
systemctl status sentinel --no-pager
systemctl status sentinel-api --no-pager
