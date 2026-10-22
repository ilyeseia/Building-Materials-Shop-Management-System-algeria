#!/bin/bash
# Refresh the Funnel serve handler after the dedicated Tailscale container
# starts.  The Tailscale control plane/edge target is not always restored by
# the persisted serve config alone; an explicit reset + set after the node is
# running recovers it.
set -u
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
base="$(cd "$(dirname "$0")" && pwd)"
container=bmshop-tailscale
state="$base/.container-start"
log="$base/recover.log"
exec 9>"$base/.lock"
/usr/bin/flock -n 9 || exit 0
cur=$(/usr/bin/docker inspect -f '{{.State.StartedAt}}' "$container" 2>/dev/null) || exit 0
last=$(cat "$state" 2>/dev/null || true)
[ "$cur" = "$last" ] && exit 0
if [ "${DRY_RUN:-0}" = "1" ]; then
  echo "$(date -u +%FT%TZ) would refresh after start $cur"
  exit 0
fi
ready=0
for i in $(seq 1 30); do
  if /usr/bin/docker exec "$container" tailscale status >/dev/null 2>&1; then
    ready=1
    break
  fi
  sleep 2
done
if [ "$ready" != "1" ]; then
  echo "$(date -u +%FT%TZ) tailscaled not ready for start $cur" >> "$log"
  exit 0
fi
echo "$(date -u +%FT%TZ) container start $cur; refreshing funnel" >> "$log"
/usr/bin/docker exec "$container" tailscale funnel reset >>"$log" 2>&1 || true
if /usr/bin/docker exec "$container" tailscale funnel --bg http://127.0.0.1:8090 >>"$log" 2>&1; then
  echo "$cur" > "$state"
else
  echo "$(date -u +%FT%TZ) funnel set failed for start $cur" >> "$log"
fi
