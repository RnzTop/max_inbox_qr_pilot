#!/bin/sh
set -eu
chown -R pilot:pilot /data
chmod 700 /data

# Optional overrides from this app's dedicated Samba-visible configuration
# folder. No changes to the system image are needed for Python-only edits.
SCRIPT="/app/pilot.py"
if [ -f /config/pilot.py ]; then
    SCRIPT="/config/pilot.py"
fi
exec gosu pilot env PYTHONPATH="/config:/app" python -u "$SCRIPT"
