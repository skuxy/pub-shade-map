# Deployment Guide

## Local

```bash
cd backend
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python scripts/fetch_data.py   # one-time OSM fetch (~2 min)
uvicorn api.main:app --reload --port 8000
```

Open http://localhost:8000.

---

## Google Cloud Run (easiest, ~$3–5/month for always-on)

### Prerequisites
```bash
# Install gcloud CLI: https://cloud.google.com/sdk/docs/install
gcloud auth login
gcloud config set project YOUR_PROJECT_ID
gcloud services enable run.googleapis.com artifactregistry.googleapis.com
```

### Deploy
```bash
# From repo root:
gcloud run deploy pub-shade-map \
  --source backend/ \
  --region europe-west3 \
  --platform managed \
  --allow-unauthenticated \
  --memory 512Mi \
  --cpu 1 \
  --min-instances 1 \
  --set-env-vars DATA_DIR=/tmp/data
```

> `--min-instances 1` keeps the service warm (avoids cold starts).  
> `europe-west3` = Frankfurt, closest to Zagreb. Free egress within EU.

Cloud Run will build the Docker image automatically from `backend/Dockerfile`.

Once deployed, copy the service URL (e.g. `https://pub-shade-map-xxx-ew.a.run.app`)
and set it in `frontend/js/config.js`:

```js
const BACKEND_URL = 'https://pub-shade-map-xxx-ew.a.run.app';
```

Then commit and push — GitHub Actions redeploys the frontend automatically.

### Persistent data (optional)

By default Cloud Run uses `/tmp` which resets on restart (triggers OSM re-fetch ~2 min).
To avoid this, mount a Cloud Storage bucket via Cloud Run volume mounts (requires billing):

```bash
gcloud run services update pub-shade-map \
  --add-volume name=data,type=cloud-storage,bucket=YOUR_BUCKET \
  --add-volume-mount volume=data,mount-path=/data \
  --set-env-vars DATA_DIR=/data \
  --region europe-west3
```

---

## Google Compute Engine e2-micro (always-free, always-on)

GCP's Always Free tier includes 1 × e2-micro VM in us-central1, us-west1, or us-east1.
Runs 24/7 at no cost indefinitely.

### 1. Create the VM

```bash
gcloud compute instances create pub-shade-map \
  --zone us-central1-a \
  --machine-type e2-micro \
  --image-family debian-12 \
  --image-project debian-cloud \
  --boot-disk-size 30GB \
  --tags http-server
```

Allow HTTP traffic:
```bash
gcloud compute firewall-rules create allow-http-pub-shade \
  --allow tcp:8000 \
  --target-tags http-server
```

### 2. SSH in and set up

```bash
gcloud compute ssh pub-shade-map --zone us-central1-a
```

Inside the VM:
```bash
sudo apt-get update && sudo apt-get install -y git python3-pip python3-venv

git clone https://github.com/skuxy/pub-shade-map.git
cd pub-shade-map/backend

python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt

python scripts/fetch_data.py   # fetch OSM data once
```

### 3. Run as a systemd service (survives reboots)

```bash
sudo tee /etc/systemd/system/pub-shade-map.service << 'SERVICE'
[Unit]
Description=Zagreb Pub Shade Map API
After=network.target

[Service]
User=YOUR_USERNAME
WorkingDirectory=/home/YOUR_USERNAME/pub-shade-map/backend
ExecStart=/home/YOUR_USERNAME/pub-shade-map/backend/venv/bin/uvicorn api.main:app --host 0.0.0.0 --port 8000
Restart=always
RestartSec=5
Environment=DATA_DIR=/home/YOUR_USERNAME/pub-shade-map/data

[Install]
WantedBy=multi-user.target
SERVICE

sudo systemctl daemon-reload
sudo systemctl enable pub-shade-map
sudo systemctl start pub-shade-map
```

Replace `YOUR_USERNAME` with your Linux username on the VM (check with `whoami`).

### 4. Get the external IP

```bash
gcloud compute instances describe pub-shade-map \
  --zone us-central1-a \
  --format='get(networkInterfaces[0].accessConfigs[0].natIP)'
```

Set it in `frontend/js/config.js`:
```js
const BACKEND_URL = 'http://YOUR_EXTERNAL_IP:8000';
```

> For HTTPS (recommended for GitHub Pages), set up a domain + Caddy or nginx with
> Let's Encrypt. Otherwise browsers may block mixed HTTP/HTTPS requests.

---

## After any backend deployment

1. Set `BACKEND_URL` in `frontend/js/config.js`
2. `git add frontend/js/config.js && git commit -m "Set backend URL" && git push`
3. GitHub Actions automatically redeploys the frontend to GitHub Pages
