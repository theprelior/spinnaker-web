#!/bin/bash
# Service status overview

echo "━━━ SpiNNaker2 Service Status ━━━"
echo ""

for svc in redis spinnaker-web spinnaker-celery; do
    STATUS=$(systemctl is-active "$svc" 2>/dev/null)
    if [ "$STATUS" = "active" ]; then
        echo "  ✓  $svc"
    else
        echo "  ✗  $svc ($STATUS)"
    fi
done

echo ""
TAILSCALE_IP=$(ip addr show tailscale0 2>/dev/null | grep 'inet ' | awk '{print $2}' | cut -d/ -f1)
LOCAL_IP=$(hostname -I | awk '{print $1}')
[ -n "$LOCAL_IP"     ] && echo "  Network   : http://$LOCAL_IP:8000"
[ -n "$TAILSCALE_IP" ] && echo "  Tailscale : http://$TAILSCALE_IP:8000"
echo ""
