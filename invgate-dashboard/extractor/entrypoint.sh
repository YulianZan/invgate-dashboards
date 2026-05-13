#!/bin/sh
set -eu

quote_env() {
    printf "%s" "$1" | sed "s/'/'\"'\"'/g"
}

write_env() {
    env_file="/app/runtime.env"
    : > "$env_file"

    for name in \
        INVGATE_URL INVGATE_USER INVGATE_PASS DB_PATH MAX_WORKERS \
        HTTP_MAX_RETRIES REQUEST_DELAY_SECONDS RATE_LIMIT_BACKOFF_SECONDS
    do
        value="$(printenv "$name" || true)"
        if [ -n "$value" ]; then
            printf "export %s='%s'\n" "$name" "$(quote_env "$value")" >> "$env_file"
        fi
    done
}

write_cron() {
    schedule="${CRON_SCHEDULE:-0 * * * *}"
    cat > /etc/cron.d/invgate <<EOF
SHELL=/bin/sh
PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin
$schedule /app/run-extract.sh >> /data/extract.log 2>&1
EOF
    chmod 0644 /etc/cron.d/invgate
    crontab /etc/cron.d/invgate
}

cat > /app/run-extract.sh <<'EOF'
#!/bin/sh
set -eu

. /app/runtime.env

lock_dir="/tmp/invgate-extract.lock"
if ! mkdir "$lock_dir" 2>/dev/null; then
    echo "$(date -u '+%Y-%m-%dT%H:%M:%SZ') Extraccion omitida: ya hay una corrida activa."
    exit 0
fi
trap 'rmdir "$lock_dir"' EXIT INT TERM

cd /app
python extract.py
EOF
chmod +x /app/run-extract.sh

write_env
write_cron

echo "Corriendo carga inicial..."
/app/run-extract.sh
echo "Carga inicial lista. Iniciando cron..."
cron -f
