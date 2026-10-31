#!/bin/sh
set -eu
umask 077

instance_id="${INSTANCE_ID:?INSTANCE_ID is required}"
port="${SERVER_PORT:?SERVER_PORT is required}"
instances_root="/data/instances"
root="$instances_root/$instance_id"
state="$root/state"
config="$root/config"
runtime_root="/run/factorio"
runtime="$runtime_root/$instance_id"
saves="$state/saves"
mods="$state/mods"

mkdir -p "$saves" "$mods" "$state/script-output" "$config" "$runtime"
chmod 0711 "$instances_root"
chmod 0711 "$runtime_root"

# Preserve prior runs before Factorio rotates factorio-current.log. Keep the
# history bounded so player/event reconstruction survives container replacement
# without consuming unbounded disk space.
history="$state/factorio-history.log"
current_log="$state/factorio-current.log"
if [ -s "$current_log" ]; then
    cat "$current_log" >> "$history"
fi
if [ -f "$history" ] && [ "$(wc -c < "$history")" -gt 20971520 ]; then
    tail -c 10485760 "$history" > "$history.tmp"
    mv "$history.tmp" "$history"
fi

if [ ! -f "$config/config.ini" ]; then
    cat > "$config/config.ini" <<EOF
[path]
read-data=/opt/factorio/data
write-data=$state

[general]
locale=

[other]
check-updates=false
enable-crash-log-uploading=false
EOF
fi

if [ ! -f "$config/server-settings.json" ]; then
    python3 - "$config/server-settings.json" <<'PY'
import json
import os
import sys

public = os.environ.get("SERVER_PUBLIC", "false").lower() == "true"
settings = {
    "name": os.environ.get("SERVER_NAME", "Factorio Server"),
    "description": os.environ.get("SERVER_DESCRIPTION", ""),
    "tags": [],
    "max_players": int(os.environ.get("MAX_PLAYERS", "10")),
    "visibility": {"public": public, "lan": True},
    "username": os.environ.get("FACTORIO_USERNAME", ""),
    "token": os.environ.get("FACTORIO_TOKEN", ""),
    "game_password": os.environ.get("GAME_PASSWORD", ""),
    "require_user_verification": True,
    "max_upload_in_kilobytes_per_second": 0,
    "max_upload_slots": 5,
    "minimum_latency_in_ticks": 0,
    "max_heartbeats_per_second": 60,
    "ignore_player_limit_for_returning_players": False,
    "allow_commands": "admins-only",
    "autosave_interval": 20,
    "autosave_slots": 5,
    "afk_autokick_interval": 0,
    "auto_pause": True,
    "only_admins_can_pause_the_game": True,
    "autosave_only_on_server": True,
    "non_blocking_saving": True,
}
with open(sys.argv[1], "w", encoding="utf-8") as output:
    json.dump(settings, output, indent=2)
    output.write("\n")
PY
fi

# List every bundled mod disabled; Factorio enables unlisted ones.
if [ ! -f "$mods/mod-list.json" ]; then
    {
        printf '{\n  "mods": [\n    {"name": "base", "enabled": true}'
        for info in /opt/factorio/data/*/info.json; do
            name=$(basename "$(dirname "$info")")
            case "$name" in base | core) continue ;; esac
            printf ',\n    {"name": "%s", "enabled": false}' "$name"
        done
        printf '\n  ]\n}\n'
    } > "$mods/mod-list.json"
fi

chmod 0600 "$config/server-settings.json" "$mods/mod-list.json"

chown -R factorio:factorio "$root"
chown -R factorio:factorio "$runtime"

if ! find "$saves" -maxdepth 1 -type f -name '*.zip' -print -quit | grep -q .; then
    if [ "${UPLOAD_SLOT:-false}" = "true" ]; then
        echo "Upload slot $instance_id is empty; container will remain stopped until a save is uploaded."
        exit 0
    fi
    gosu factorio /opt/factorio/bin/x64/factorio \
        --config "$config/config.ini" \
        --create "$saves/default.zip"
fi

console="$runtime/console.fifo"
[ -p "$console" ] || { rm -f "$console"; gosu factorio mkfifo -m 0600 "$console"; }
exec 0<>"$console"

exec gosu factorio /opt/factorio/bin/x64/factorio \
    --config "$config/config.ini" \
    --server-settings "$config/server-settings.json" \
    --port "$port" \
    --start-server-load-latest
