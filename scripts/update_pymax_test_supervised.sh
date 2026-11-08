#!/usr/bin/env bash
# Safely refresh only the isolated MAX PyMax test add-on *source* files.
# Does not restart Supervisor, Core, apps or production MAX integration.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите скрипт через sudo bash." >&2
  exit 1
fi
REF="${1:-}"
if [[ ! "$REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Укажите полный 40-символьный commit SHA проверенной версии." >&2
  exit 1
fi

supervisor_data="$(docker inspect hassio_supervisor --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"
if [ -z "$supervisor_data" ] || [ ! -d "$supervisor_data" ]; then
  echo "Не найден каталог данных Supervisor. Файлы не изменены." >&2
  exit 1
fi

if [ -f "$supervisor_data/apps/local/max_pymax_readonly/config.yaml" ]; then
  install_dir="$supervisor_data/apps/local/max_pymax_readonly"
elif [ -f "$supervisor_data/addons/local/max_pymax_readonly/config.yaml" ]; then
  install_dir="$supervisor_data/addons/local/max_pymax_readonly"
else
  echo "Локальное дополнение max_pymax_readonly не найдено. Файлы не изменены." >&2
  exit 1
fi

files=(app.py id_store.py README.md DOCS.md config.yaml)
stage="$(mktemp -d)"
trap 'rm -rf -- "$stage"' EXIT
base="https://raw.githubusercontent.com/RnzTop/max_inbox_qr_pilot/$REF/max_pymax_readonly"
for f in "${files[@]}"; do
  curl --fail --silent --show-error --location --retry 3 --connect-timeout 10 --max-time 60 "$base/$f" -o "$stage/$f"
done

python3 -m py_compile "$stage/app.py" "$stage/id_store.py"
python3 - "$stage/config.yaml" "$install_dir/config.yaml" <<'PY'
import json
import sys
with open(sys.argv[1], encoding="utf-8") as f:
    updated = json.load(f)
with open(sys.argv[2], encoding="utf-8") as f:
    installed = json.load(f)
assert updated["slug"] == "max_pymax_readonly"
assert installed["slug"] == "max_pymax_readonly"
assert updated["version"] == "0.0.3", updated["version"]
assert updated["boot"] == "manual"
PY

backups="$supervisor_data/.max_pymax_readonly_source_backups"
mkdir -p "$backups"
chmod 700 "$backups"
backup="$(mktemp -d "$backups/before-0.0.3.XXXXXXXX")"
chmod 700 "$backup"
for f in "${files[@]}"; do
  if [ -f "$install_dir/$f" ]; then
    cp -p -- "$install_dir/$f" "$backup/$f"
  fi
done

# Apply config last so the version changes only after all code is present.
for f in "${files[@]}"; do
  install -m 644 -- "$stage/$f" "$install_dir/$f"
done
echo "Исходники PyMax 0.0.3 обновлены: $install_dir"
echo "Резервная копия: $backup"
echo "Данные /data, сессия MAX и существующие метки не изменены."
echo "Сборка и запуск не выполнялись."
echo "В Home Assistant обновите локальный магазин и пересоберите дополнение."
