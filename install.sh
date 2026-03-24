#!/usr/bin/env bash

echo "Installiere whatsON..."
pip install --user -e . --break-system-packages || pip install --user -e .
echo "Installation abgeschlossen. Führe 'wo --help' oder 'whatson --help' aus."
