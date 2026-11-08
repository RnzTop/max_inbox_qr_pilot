#!/usr/bin/env bash
# Update only sources of existing max_pymax_inbox addon. Never touch /data.
set -euo pipefail
if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo bash." >&2
  exit 1
fi
REF="${1:-}"
if [[ ! "$REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Нужен 40-символьный commit SHA проверенной версии." >&2
  exit 1
fi
supervisor_data="$(docker inspect hassio_supervisor \
  --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' \
  2>/dev/null || true)"
if [ -z "$supervisor_data" ] || [ ! -d "$supervisor_data" ]; then
  echo "Не найдены данные Supervisor. Ничего не изменено." >&2
  exit 1
fi
if [ -f "$supervisor_data/apps/local/max_pymax_inbox/config.yaml" ]; then
  addon_dir="$supervisor_data/apps/local/max_pymax_inbox"
elif [ -f "$supervisor_data/addons/local/max_pymax_inbox/config.yaml" ]; then
  addon_dir="$supervisor_data/addons/local/max_pymax_inbox"
else
  echo "Дополнение max_pymax_inbox ещё не установлено. Используйте установщик." >&2
  exit 1
fi
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
base="https://raw.githubusercontent.com/RnzTop/max_inbox_qr_pilot/$REF/max_pymax_inbox"
files=(Dockerfile config.yaml run.sh app.py bot_api.py inbox_db.py processor.py callbacks.py README.md DOCS.md)
for file in "${files[@]}"; do
  curl -fsSL --retry 3 --connect-timeout 10 --max-time 60 \
    "$base/$file" -o "$stage/$file"
done
mkdir -p "$stage/certs"
for file in russian_trusted_root.pem russian_trusted_sub.pem; do
  curl -fsSL --retry 3 --connect-timeout 10 --max-time 60 \
    "$base/certs/$file" -o "$stage/certs/$file"
done
python3 -m py_compile "$stage/app.py" "$stage/bot_api.py" \
  "$stage/inbox_db.py" "$stage/processor.py" "$stage/callbacks.py"
python3 - "$stage/config.yaml" "$addon_dir/config.yaml" <<'PY'
import json, sys
with open(sys.argv[1], encoding="utf-8") as f:
    next_cfg = json.load(f)
with open(sys.argv[2], encoding="utf-8") as f:
    old_cfg = json.load(f)
assert next_cfg["slug"] == old_cfg["slug"] == "max_pymax_inbox"
assert next_cfg["version"] == "1.0.0-rc2"
assert set(next_cfg["options"]) == {"bot_token", "work_chat_id"}
assert next_cfg["boot"] == "auto"
PY
backup_root="$supervisor_data/.max_pymax_inbox_source_backups"
mkdir -p "$backup_root"
chmod 700 "$backup_root"
backup="$(mktemp -d "$backup_root/pre-rc2.XXXXXXXX")"
chmod 700 "$backup"
for f in "${files[@]}"; do
  if [ -f "$addon_dir/$f" ]; then cp -p -- "$addon_dir/$f" "$backup/$f"; fi
done
if [ -d "$addon_dir/certs" ]; then
  cp -a -- "$addon_dir/certs" "$backup/certs"
fi
mkdir -p "$addon_dir/certs"
for f in "${files[@]}"; do
  if [ "$f" != "config.yaml" ]; then
    install -m 644 -- "$stage/$f" "$addon_dir/$f"
  fi
done
chmod 755 "$addon_dir/run.sh"
for f in russian_trusted_root.pem russian_trusted_sub.pem; do
  install -m 644 -- "$stage/certs/$f" "$addon_dir/certs/$f"
done
# Version last: no partial Python update should be advertised.
install -m 644 -- "$stage/config.yaml" "$addon_dir/config.yaml"
echo "Исходники MAX PyMax 1.0.0-rc2 обновлены: $addon_dir"
echo "Резервная копия исходников: $backup"
echo "Сессия QR, параметры и очередь /data НЕ изменены."
echo "Контейнеры и Home Assistant НЕ перезапускались."
echo "Теперь обновите локальный магазин и пересоберите только max_pymax_inbox."
