#!/usr/bin/env bash

echo "Installiere Whatson..."
pip install --user -e . --break-system-packages || pip install --user -e .
echo "Installation abgeschlossen. Führe 'whatson --help' aus."
