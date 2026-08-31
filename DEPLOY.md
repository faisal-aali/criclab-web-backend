# Lightsail — self-hosted runner (backend)

GitHub sends jobs to `/var/www/actions-runner-backend`. This repo is **FastAPI (Python 3.12)**, not Node. PM2 runs `criclab-api`.

## On the instance (once), as `admin`

```bash
python3.12 --version    # required (not 3.13+)
pm2 --version           # npm i -g pm2  if missing
pm2 list

sudo mkdir -p /var/www/criclab-web-backend
sudo chown -R admin:admin /var/www/criclab-web-backend

# Production secrets stay on the box — never in git
nano /var/www/criclab-web-backend/.env
# APP_ENV=production, MONGODB_URI, JWT_SECRET, CORS_ORIGINS, APP_BASE_URL,
# Bedrock / Cloudinary / SMTP as you already use.

sudo systemctl status actions.runner.Techlio-Pvt-Ltd-criclab-web-backend.ip-172-26-12-222.service
```

If an old Node `pm2` process was serving a placeholder API, stop it after the first successful Python start so ports do not clash:

```bash
pm2 list
# pm2 delete <old-name>
```

## From your laptop

Commit `.github/workflows/deploy.yml` plus `deploy/` and push `main`. Then GitHub → Actions → **Deploy Backend**.

First job installs the venv and `pm2 startOrReload` `criclab-api`. Re-run the workflow if `.env` was created after a failed first attempt.

```bash
sudo journalctl -u actions.runner.Techlio-Pvt-Ltd-criclab-web-backend.ip-172-26-12-222.service -f
curl -sS http://127.0.0.1:8000/health
```
