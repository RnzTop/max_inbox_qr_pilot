#!/usr/bin/env bash
# Install test-only PyMax source into the HA Supervisor local-app store.
# Requires root; never starts, stops, or modifies running containers.
set -euo pipefail

if [ "$(id -u)" -ne 0 ]; then
  echo "Запустите через sudo bash, не через curl | bash."
  exit 1
fi
REF="${1:-}"
if [[ ! "$REF" =~ ^[0-9a-f]{40}$ ]]; then
  echo "Требуется проверенный 40-символьный commit SHA."
  exit 1
fi

supervisor_data="$(docker inspect hassio_supervisor \
  --format '{{range .Mounts}}{{if eq .Destination "/data"}}{{.Source}}{{end}}{{end}}' 2>/dev/null || true)"
if [ -z "$supervisor_data" ] || [ ! -d "$supervisor_data" ]; then
  echo "Не найден каталог данных hassio_supervisor. Установка отменена."
  exit 1
fi

if [ -d "$supervisor_data/apps/local" ] || [ -d "$supervisor_data/apps" ]; then
  local_store="$supervisor_data/apps/local"
elif [ -d "$supervisor_data/addons/local" ]; then
  local_store="$supervisor_data/addons/local"
else
  local_store="$supervisor_data/apps/local"
fi
install_dir="$local_store/max_pymax_readonly"
if [ -e "$install_dir" ]; then
  echo "Каталог уже существует: $install_dir"
  echo "Ничего не перезаписано. Обновление делайте отдельно."
  exit 1
fi

mkdir -p "$local_store"
stage="$(mktemp -d "$local_store/.max_pymax_readonly.XXXXXXXX")"
cleanup() {
  if [ -d "$stage" ]; then rm -rf -- "$stage"; fi
}
trap cleanup EXIT
base="https://raw.githubusercontent.com/RnzTop/max_inbox_qr_pilot/$REF/max_pymax_readonly"
for f in Dockerfile config.yaml run.sh app.py id_store.py history_recovery.py README.md DOCS.md; do
  curl --fail --silent --show-error --location \
    --retry 3 --max-time 40 "$base/$f" -o "$stage/$f"
done
python3 -m py_compile "$stage/app.py" "$stage/id_store.py" "$stage/history_recovery.py"
python3 -m json.tool "$stage/config.yaml" >/dev/null
chmod 755 "$stage" "$stage/run.sh"
chmod 644 "$stage/Dockerfile" "$stage/config.yaml" "$stage/app.py" \
  "$stage/id_store.py" "$stage/history_recovery.py" "$stage/README.md" "$stage/DOCS.md"
mv -- "$stage" "$install_dir"
trap - EXIT
echo "Файлы тестового дополнения установлены: $install_dir"
echo "НИЧЕГО НЕ ЗАПУЩЕНО. Остановите прежний QR-пилот перед запуском PyMax."
echo "Далее: Home Assistant → Настройки → Приложения/Дополнения → Магазин → Проверить обновления."
