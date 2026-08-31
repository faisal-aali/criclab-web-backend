# Backend CI and deploy

1. `git push` runs `python3 -m compileall app` locally (see `.githooks/pre-push`). A failing check never reaches GitHub.
2. GitHub **CI** compiles the app. **Deploy** runs only if that job passes.

Enable the local hook once:

```bash
git config core.hooksPath .githooks
```

The instance directory must already be a git clone. Add repo secret `GH_PAT` (a GitHub PAT with `repo` access). Keep `/var/www/criclab-web-backend/.env` on the instance only.
