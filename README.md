# whatsON — WhatsApp Web CLI  (v1.1.0)

Command-line tool for WhatsApp Web automation, powered by Playwright.
Aufrufbar als **`wo`** (Kurzform) oder **`whatson`**.

## Installation

```bash
cd /home/david/whatsON
pip install -e .
playwright install chromium
```

## Usage

```bash
# Authentifizierung prüfen
wo status

# Konversationen auflisten
wo list

# Nachrichten herunterladen
wo get all

# Chat-Verlauf anzeigen
wo show "Kontakt Name"

# Nachricht senden
wo send "Kontakt Name" "Hallo!"

# Scheduler steuern
wo scheduler start

# Hilfe
wo --help
```

> Alle Befehle funktionieren auch mit `whatson` statt `wo`.

## Configuration

Settings live in `~/.whatson/config.yaml` (auto-created on first run):

| Key               | Default | Description                             |
|-------------------|---------|-----------------------------------------|
| `poll_interval`   | 60      | Seconds between poll/scheduler checks   |
| `poll_command`    | echo …  | Shell command on new message            |
| `browser_headless`| true    | Headless after first QR scan            |
| `user_data_dir`   | ~/.whatson/browser_data | Browser session storage |

## First Run

On first use, run `wo auth` to open a visible browser window and scan the
WhatsApp Web QR code. After that, the session is persisted and subsequent
runs are headless.
