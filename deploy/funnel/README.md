# Dedicated Tailscale Funnel for the building-materials app

This directory replaces tsdproxy as the public HTTPS publisher for the
application.  The public hostname does not change:

`https://building-materials-shop-management-system.tail7d68dd.ts.net/`

## Why this exists

tsdproxy v1.4.7 calls Tailscale's `ListenFunnel`, which sets the persisted
`AllowFunnel` bit, but the proxy can end up with **no serve handler**.  The
local tailnet URL works, while the public Funnel edge has no target (or only a
stale target on some ingress IPs).  The same design also creates a new
ephemeral tsnet device on many restarts, which changes the tailnet IP and
makes control-plane target propagation worse.

The dedicated sidecar uses the official Tailscale image, an explicit
`tailscale funnel` serve handler, and a persistent device state.  The
application itself is unchanged.

## Topology

- App: `127.0.0.1:8090` on the host (`building-materials-shop-app-1`).
- Funnel: `bmshop-tailscale`, host networking, userspace Tailscale.
- Public: Tailscale Funnel `:443` -> `http://127.0.0.1:8090`.
- App compose: `tsdproxy.enable: "false"`.  Never enable tsdproxy for the app
  while this sidecar is running; both would compete for the same hostname.

## State volume

`bmshop-tailscale-state` contains the device identity, prefs and TLS
certificate cache.  It must not be deleted or recreated casually.

Verified cold backup on the production host:

`/home/sysuser/backups/bmshop-tailscale-state-20261022T092439Z-cold.tgz` (9.1K)

Restore while the container is stopped:

```bash
docker stop bmshop-tailscale
docker run --rm -v bmshop-tailscale-state:/dst \
  -v /home/sysuser/backups:/backup alpine sh -c \
  'rm -rf /dst/* && tar xzf /backup/bmshop-tailscale-state-20261022T092439Z-cold.tgz -C /dst'
docker start bmshop-tailscale
```

The state was originally seeded from the persistent tsdproxy state at
`/data/default/building-materials-shop-management-system` in the
`tsdproxy_data` volume.  A rebuild that loses the volume needs a Tailscale
auth key (stored on the host at
`/home/sysuser/n8n-directory/tsdproxy/config/authkey`, never commit it) and a
fresh `tailscale up`; if an old device still owns the hostname, Tailscale may
assign a `-1` suffix.  That requires admin cleanup.

## Start and verify

```bash
cd deploy/funnel
docker volume create bmshop-tailscale-state   # first time only; seed before start
docker compose up -d
docker exec bmshop-tailscale tailscale status
docker exec bmshop-tailscale tailscale funnel reset
docker exec bmshop-tailscale tailscale funnel --bg http://127.0.0.1:8090
docker exec bmshop-tailscale tailscale funnel status
```

Expected local status:

```text
https://building-materials-shop-management-system.tail7d68dd.ts.net (Funnel on)
|-- / proxy http://127.0.0.1:8090
```

Verify all three public A records, not only MagicDNS/localhost:

```bash
h=building-materials-shop-management-system.tail7d68dd.ts.net
for ip in 176.58.90.46 176.58.90.63 176.58.90.145; do
  curl -sS -o /dev/null -w "$ip %{http_code}\n" --max-time 15 \
    --resolve "$h:443:$ip" "https://$h/healthz"
done
```

## Restart recovery

The control-plane Funnel edge target is not always restored by the persisted
serve config after a container or host restart.  The installed host watchdog
`ensure-funnel.sh` (cron, every minute) detects a new
`docker inspect .State.StartedAt` and runs `tailscale funnel reset` followed
by `tailscale funnel --bg http://127.0.0.1:8090` after the tailscaled CLI is
ready.  It records the last handled start in `.container-start` and logs to
`recover.log`.

The sequence was exercised manually after the 2026-10-22 cold restart: after
reset + set, all three ingress IPs returned 200 within about two minutes.  The
cron detection was dry-run tested; an automated restart triggered by cron has
not been separately timed.  A full host reboot has **not** been tested.

Cron entry on the production host:

```cron
* * * * * /home/sysuser/apps/building-materials-shop/funnel/ensure-funnel.sh >/dev/null 2>&1
```

## Security

- Public exposure is only Funnel `:443` to the app; the app port remains bound
  to `127.0.0.1`.
- No SSH or admin service is published through this sidecar.  The unrelated
  Windows SSH Funnel was reset on 2026-10-22 and reports `No serve config`.
- Tailscale ACLs still govern tailnet access; public Funnel uses the app's
  own authentication and rate limits.

## Status and limits

- Public HTTPS (IPv4) is **PASS**: all three ingress IPs returned `/healthz`
  200 and `/` 302 -> `/login` 200; external probes passed; TLS certificate is
  a valid Let's Encrypt certificate for the exact hostname.
- Restart resilience is **PARTIAL**: recovery works with the watchdog, but a
  restart can interrupt the public path until the edge target is refreshed.
- Public IPv6, a long soak, and concurrent load are **NOT TESTED**.
- Tailscale admin/control-plane access is **BLOCKED**; the underlying edge
  target propagation cannot be inspected or forced from the admin console.
