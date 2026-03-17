# Zagreb Pub Shade Map — Operations Runbook

## Infrastructure overview

| Component | Details |
|-----------|---------|
| VM | GCE e2-micro, `us-central1-a`, project `pub-shade-map` |
| External IP | `34.60.197.214` |
| Domain | `pub-shade-map.duckdns.org` (DuckDNS → VM IP) |
| Backend | Python/FastAPI, systemd service `pub-shade-map` |
| Frontend | GitHub Pages at `https://skuxy.github.io/pub-shade-map` |
| Reverse proxy | Caddy (handles HTTPS, port 443 → 127.0.0.1:8000) |
| gcloud CLI | `/home/skux/pub-shade-map/google-cloud-sdk/bin/gcloud` |

---

## SSH access

```bash
# SSH into the VM
/home/skux/pub-shade-map/google-cloud-sdk/bin/gcloud \
  compute ssh pub-shade-map --zone us-central1-a --project pub-shade-map

# Run a one-off command without interactive shell
/home/skux/pub-shade-map/google-cloud-sdk/bin/gcloud \
  compute ssh pub-shade-map --zone us-central1-a --project pub-shade-map \
  --command "your command here"
```

---

## Deploy code update

```bash
# Push local commits, then pull + restart on VM
git push origin main
/home/skux/pub-shade-map/google-cloud-sdk/bin/gcloud \
  compute ssh pub-shade-map --zone us-central1-a --project pub-shade-map \
  --command "cd ~/pub-shade-map && git pull && sudo systemctl restart pub-shade-map"
```

---

## Service management (run on VM)

```bash
# Status
sudo systemctl status pub-shade-map

# Restart
sudo systemctl restart pub-shade-map

# Stop / Start
sudo systemctl stop pub-shade-map
sudo systemctl start pub-shade-map

# Tail live logs
sudo journalctl -u pub-shade-map -f

# Recent logs (last 100 lines)
sudo journalctl -u pub-shade-map --no-pager -n 100
```

---

## Health check

```bash
# Quick check from anywhere
curl https://pub-shade-map.duckdns.org/health | python3 -m json.tool

# What to look for:
#   status: "ok"
#   pubs_loaded: 445
#   buildings_loaded: 107658
#   strtree_ready: true
#   shade_cache.precompute_running: true (while computing) / false (done)
#   shade_cache.pubs_cached_today: should grow over time; 445 = fully cached
#   memory.system_available_mb: warn if < 100 MB
```

---

## Re-fetch OSM data

OSM cache auto-refreshes after 7 days on restart. To force it manually:

```bash
# On VM: delete cache files and restart
/home/skux/pub-shade-map/google-cloud-sdk/bin/gcloud \
  compute ssh pub-shade-map --zone us-central1-a --project pub-shade-map \
  --command "rm ~/pub-shade-map/data/pubs.geojson ~/pub-shade-map/data/buildings.geojson; sudo systemctl restart pub-shade-map"

# Or via the admin endpoint (requires REFRESH_KEY env var to be set):
curl -X POST https://pub-shade-map.duckdns.org/api/admin/refresh \
  -H "X-Refresh-Key: YOUR_KEY"
```

---

## Clear shade cache (force recompute)

```bash
/home/skux/pub-shade-map/google-cloud-sdk/bin/gcloud \
  compute ssh pub-shade-map --zone us-central1-a --project pub-shade-map \
  --command "rm -rf ~/pub-shade-map/data/shade_cache/ && sudo systemctl restart pub-shade-map"
```

---

## Run backend tests on VM

```bash
/home/skux/pub-shade-map/google-cloud-sdk/bin/gcloud \
  compute ssh pub-shade-map --zone us-central1-a --project pub-shade-map \
  --command "cd ~/pub-shade-map/backend && source venv/bin/activate && python tests/test_shadow.py"
```

---

## Caddy (HTTPS reverse proxy)

```bash
# On VM:
sudo systemctl status caddy
sudo systemctl restart caddy
sudo nano /etc/caddy/Caddyfile

# Caddyfile should contain:
# pub-shade-map.duckdns.org {
#     reverse_proxy 127.0.0.1:8000
# }
```

---

## DuckDNS (dynamic DNS)

DuckDNS points `pub-shade-map.duckdns.org` → `34.60.197.214`.

If the VM gets a new IP (rare on e2-micro reserved IPs), update at:
`https://www.duckdns.org` — log in and update the `pub-shade-map` subdomain.

Or update via API:
```bash
# Replace TOKEN and NEW_IP
curl "https://www.duckdns.org/update?domains=pub-shade-map&token=TOKEN&ip=NEW_IP"
```

---

## Monitor memory

```bash
# From VM:
free -h
# or via health endpoint — system_available_mb < 100 MB = trouble

# The VM has 1 GB RAM + 1 GB swap.
# Normal RSS for the backend process: ~200–450 MB (Shapely + 107k polygons).
# If OOM-killing occurs, reduce PRECOMPUTE_WORKERS in backend/api/main.py.
```

---

## VM start/stop (cost saving)

```bash
# Stop VM (saves compute cost; disk retained)
/home/skux/pub-shade-map/google-cloud-sdk/bin/gcloud \
  compute instances stop pub-shade-map --zone us-central1-a --project pub-shade-map

# Start VM
/home/skux/pub-shade-map/google-cloud-sdk/bin/gcloud \
  compute instances start pub-shade-map --zone us-central1-a --project pub-shade-map

# Note: e2-micro in us-central1 is Always Free tier — no cost to keep running.
```

---

## GitHub Pages (frontend)

Frontend deploys automatically on every push to `main` via
`.github/workflows/deploy-pages.yml`.

Check deployment status:
```bash
gh run list --repo skuxy/pub-shade-map --workflow deploy-pages.yml
```

Live URL: `https://skuxy.github.io/pub-shade-map`

---

## Backend API base URL

Configured in `frontend/js/config.js`:
- On `localhost` → `http://localhost:8000`
- Elsewhere → `https://pub-shade-map.duckdns.org`

---

## Useful API endpoints

| Endpoint | Description |
|----------|-------------|
| `GET /health` | Service health + cache stats |
| `GET /api/pubs` | All pubs GeoJSON with sunny scores |
| `GET /api/shade/{id}?date=YYYY-MM-DD` | Shade timeline for one pub |
| `GET /api/current-status` | Sun/shade status for all pubs right now |
| `GET /api/weather?lat=…&lon=…` | 3-day weather forecast |
| `POST /api/admin/refresh` | Force OSM re-fetch (needs `X-Refresh-Key` header) |

---

## Python environment (on VM)

```bash
# Activate venv
cd ~/pub-shade-map/backend && source venv/bin/activate

# Install a new dependency
pip install package-name
pip freeze > requirements.txt

# Run app directly (useful for debugging)
uvicorn api.main:app --reload --host 127.0.0.1 --port 8000
```
