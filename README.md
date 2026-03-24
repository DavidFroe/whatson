# whatson — WhatsApp Web CLI

Command-line tool for WhatsApp Web automation, powered by Playwright.

## Installation

```bash
cd /home/david/whatson
pip install -e .
playwright install chromium
```

## Usage

```bash
# Check authentication status
whatson status

# List active conversations
whatson conversation

# Read chat history
whatson get "Contact Name"

# Send a message
whatson chat "Contact Name" --text "Hello!"

# Schedule a message
whatson plan "Contact Name" --text "Reminder!" --time "2026-03-01 09:00"

# List / delete scheduled messages
whatson plan-list
whatson plan-delete <plan_id>

# Run the scheduler daemon (sends due plans, runs continuously)
whatson run-scheduler

# Poll a conversation for new messages
whatson poll "Contact Name"
```

## Configuration

Settings live in `~/.whatson/config.yaml` (auto-created on first run):

| Key               | Default | Description                             |
|-------------------|---------|-----------------------------------------|
| `poll_interval`   | 60      | Seconds between poll/scheduler checks   |
| `poll_command`    | echo …  | Shell command on new message            |
| `browser_headless`| true    | Headless after first QR scan            |
| `user_data_dir`   | ~/.whatson/browser_data | Browser session storage |

## First Run

On first use, `whatson` opens a visible browser window so you can scan the WhatsApp Web QR code. After that, the session is persisted and subsequent runs are headless.
