# Deploy backend (self-hosted runner)

Push to `main`. The workflow:

1. `git pull` in `/var/www/criclab-web-backend`
2. install `requirements.txt` into `.venv312`
3. `python -m compileall app`
4. `pm2 restart criclab-api`

Keep `/var/www/criclab-web-backend/.env` on the instance only.
