#!/usr/bin/env bash
# Install a NEW isolated production-candidate PyMax add-on. No existing addons touched.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите скрипт через sudo bash." >&2
  exit 1
fi

REF="${1:-}"
if [[ ! "$REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Нужен полный 40-символьный SHA проверенной версии." >&2
  exit 1
fi

supervisor_data="$(docker inspect hassio_supervisor \
  --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' \
  2>/dev/null || true)"
if [ -z "$supervisor_data" ] || [ ! -d "$supervisor_data" ]; then
  echo "Не найдены данные Supervisor, установка остановлена." >&2
  exit 1
fi

if [ -d "$supervisor_data/apps/local" ] || [ -d "$supervisor_data/apps" ]; then
  local_store="$supervisor_data/apps/local"
elif [ -d "$supervisor_data/addons/local" ]; then
  local_store="$supervisor_data/addons/local"
else
  local_store="$supervisor_data/apps/local"
fi

install_dir="$local_store/max_pymax_inbox"
if [ -e "$install_dir" ]; then
  echo "Новый аддон уже существует: $install_dir"
  echo "Ничего не изменено и не перезапущено." >&2
  exit 1
fi

mkdir -p "$local_store"
stage="$(mktemp -d "$local_store/.max_pymax_inbox.XXXXXXXX")"
trap 'rm -rf -- "$stage"' EXIT
base="https://raw.githubusercontent.com/RnzTop/max_inbox_qr_pilot/$REF/max_pymax_inbox"
for f in Dockerfile config.yaml run.sh app.py bot_api.py inbox_db.py processor.py callbacks.py README.md DOCS.md; do
  curl --fail --silent --show-error --location --retry 3 --max-time 60 \
    "$base/$f" -o "$stage/$f"
done
mkdir -p "$stage/certs"
for f in russian_trusted_root.pem russian_trusted_sub.pem; do
  curl --fail --silent --show-error --location --retry 3 --max-time 60 \
    "$base/certs/$f" -o "$stage/certs/$f"
done

python3 -m py_compile "$stage/app.py" "$stage/bot_api.py" \
  "$stage/inbox_db.py" "$stage/processor.py" "$stage/callbacks.py"
python3 - "$stage/config.yaml" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as fp:
    cfg = json.load(fp)
assert cfg["slug"] == "max_pymax_inbox"
assert cfg["version"] == "1.0.0-rc2"
assert cfg["boot"] == "auto"
assert cfg["arch"] == ["aarch64"]
assert set(cfg["options"]) == {"bot_token", "work_chat_id"}
PY

chmod 755 "$stage" "$stage/run.sh"
chmod 644 "$stage"/{Dockerfile,config.yaml,app.py,bot_api.py,inbox_db.py,processor.py,callbacks.py,README.md,DOCS.md}
chmod 644 "$stage/certs"/*.pem
mv -- "$stage" "$install_dir"
trap - EXIT
echo "Новое дополнение MAX — Входящие обращения установлено: $install_dir"
echo "Ни один контейнер не запущен, тестовая и рабочая интеграции не изменены."
echo "Далее обновите магазин локальных дополнений Home Assistant."
