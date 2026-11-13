#!/bin/sh
# Make the Python environment the OTA tools need, in .venv/ next to this
# script: bleak (BLE) and cbor2 (SMP), from requirements.txt.  Run again to
# update.  Then use
#
#     tools/ota/.venv/bin/python tools/ota/g1_ota_web.py
#     tools/ota/.venv/bin/python tools/ota/g1_ota.py application_update.zip
#
# Needs python3 with venv and pip.  System site packages stay visible, so
# distro-provided packages are reused rather than built.
set -eu
cd "$(dirname "$0")"

if [ ! -x .venv/bin/python ]; then
    echo "setup.sh: creating .venv"
    python3 -m venv --system-site-packages .venv
fi
.venv/bin/python -m pip install --quiet --upgrade pip
.venv/bin/python -m pip install --quiet --requirement requirements.txt
echo "setup.sh: ready; run the tools with $(pwd)/.venv/bin/python"
