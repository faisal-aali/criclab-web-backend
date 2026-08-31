module.exports = {
  apps: [
    {
      name: "criclab-api",
      cwd: "/var/www/criclab-web-backend",
      script: "/var/www/criclab-web-backend/.venv312/bin/python",
      args: "-m uvicorn app.main:app --host 127.0.0.1 --port 8000 --workers 1 --timeout-keep-alive 75",
      interpreter: "none",
      instances: 1,
      autorestart: true,
      max_memory_restart: "2G",
      env: {
        APP_ENV: "production",
      },
    },
  ],
}
