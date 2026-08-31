# Backend CI and deploy

1. `git push` runs `python3 -m compileall app` locally (see `.githooks/pre-push`). A failing check never reaches GitHub.
2. GitHub **CI** compiles the app. **Deploy** runs only if that job passes.

Enable the local hook once:

```bash
git config core.hooksPath .githooks
```

The instance directory must already be a git clone. Do not put a PAT in `origin`. Keep `/var/www/criclab-web-backend/.env` on the instance only.

Video analysis runs in **criclab-video-service** workers (not this process). Jobs stay `queued` until a worker is running.
