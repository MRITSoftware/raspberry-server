module.exports = {
  apps: [{
    name: 'mrit-server',
    script: 'tuya_server.py',
    interpreter: 'python3',
    instances: 1,
    autorestart: true,
    watch: false,
    max_memory_restart: '200M',
    log_date_format: 'YYYY-MM-DD HH:mm:ss',
    env_file: '.env',
    env: {
      FLASK_ENV: 'production'
    }
  }]
}
