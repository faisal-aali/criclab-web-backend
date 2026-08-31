module.exports = {
  apps: [
    {
      name: "criclab-api",
      cwd: "/var/www/criclab-web-backend",
      script: "/var/www/criclab-web-backend/deploy/start-api.sh",
      interpreter: "bash",
      instances: 1,
      exec_mode: "fork",
      autorestart: true,
      max_memory_restart: "2G",
      kill_timeout: 10000,
      env: {
        APP_ENV: "production",
        PYTHONUNBUFFERED: "1",
      },
    },
  ],
}
