#!/usr/bin/env python3
"""
Whatson — A monolithic WhatsApp Web CLI tool powered by Playwright.
Provides CLI commands for reading, sending, and scheduling WhatsApp messages.
"""

import json
import os
import shutil
import subprocess
import sys
import time
import uuid
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

import typer
import unicodedata
import yaml

# Globales Verbose-Flag
VERBOSE = False
from playwright.sync_api import (
    BrowserContext,
    Error as PlaywrightError,
    Page,
    Playwright,
    TimeoutError as PlaywrightTimeout,
    sync_playwright,
)
from tinydb import TinyDB, where

# ==============================================================================
# 1. CONFIGURATION
# ==============================================================================

__version__ = "1.1.0"


class NotAuthenticatedError(Exception):
    """Raised when WhatsApp Web session is missing or expired."""


WHATSON_HOME = Path.home() / ".whatson"
USER_CONFIG_PATH = WHATSON_HOME / "config.yaml"
# Fallback to local config if present in the same dir
PROJECT_CONFIG_PATH = Path(__file__).resolve().parent / "config.yaml"

DEFAULT_CONFIG: Dict[str, Any] = {
    "poll_interval": 60,
    "poll_command": 'echo "New message in {conversation}: {message}"',
    "browser_headless": True,
    "user_data_dir": str(WHATSON_HOME / "browser_data"),
    "rtl_mode": False,
}

def _ensure_home() -> None:
    """Create ~/.whatson/ and seed it with a default config if needed."""
    WHATSON_HOME.mkdir(parents=True, exist_ok=True)
    if not USER_CONFIG_PATH.exists():
        if PROJECT_CONFIG_PATH.exists():
            shutil.copy(PROJECT_CONFIG_PATH, USER_CONFIG_PATH)
        else:
            with open(USER_CONFIG_PATH, "w") as fh:
                yaml.dump(DEFAULT_CONFIG, fh, default_flow_style=False)

def load_config() -> Dict[str, Any]:
    """Return the merged configuration dictionary."""
    _ensure_home()
    config = dict(DEFAULT_CONFIG)
    for path in (PROJECT_CONFIG_PATH, USER_CONFIG_PATH):
        if path.exists():
            with open(path, "r") as fh:
                data = yaml.safe_load(fh) or {}
            config.update(data)
    # Expand ~ in user_data_dir
    config["user_data_dir"] = os.path.expanduser(config["user_data_dir"])
    return config

_cached_config: Optional[Dict[str, Any]] = None

def get_config() -> Dict[str, Any]:
    """Return the (cached) configuration dictionary."""
    global _cached_config
    if _cached_config is None:
        _cached_config = load_config()
    return _cached_config


# ==============================================================================
# 2. STORAGE (TinyDB)
# ==============================================================================

DB_PATH = WHATSON_HOME / "db.json"

def _get_db() -> TinyDB:
    """Return a TinyDB instance (creates the file if needed)."""
    WHATSON_HOME.mkdir(parents=True, exist_ok=True)
    return TinyDB(str(DB_PATH))

def add_plan(conversation: str, text: str, scheduled_time: str) -> str:
    """Add a scheduled message and return its plan_id (UUID)."""
    plan_id = str(uuid.uuid4())[:8]
    db = _get_db()
    plans = db.table("plans")
    plans.insert(
        {
            "plan_id": plan_id,
            "conversation": conversation,
            "text": text,
            "scheduled_time": scheduled_time,
            "status": "pending",
            "created_at": datetime.now().isoformat(),
        }
    )
    db.close()
    return plan_id

def list_plans(status: Optional[str] = None) -> List[Dict[str, Any]]:
    """Return all plans, optionally filtering by status."""
    db = _get_db()
    plans = db.table("plans")
    if status:
        results = plans.search(where("status") == status)
    else:
        results = plans.all()
    db.close()
    return results

def delete_plan(plan_id: str) -> bool:
    """Delete a plan by its plan_id. Returns True if something was deleted."""
    db = _get_db()
    plans = db.table("plans")
    removed = plans.remove(where("plan_id") == plan_id)
    db.close()
    return len(removed) > 0

def get_due_plans() -> List[Dict[str, Any]]:
    """Return all pending plans whose scheduled_time is in the past."""
    now = datetime.now().isoformat()
    db = _get_db()
    plans = db.table("plans")
    results = plans.search(
        (where("status") == "pending") & (where("scheduled_time") <= now)
    )
    db.close()
    return results

def mark_plan(plan_id: str, status: str) -> None:
    """Update a plan's status (e.g. 'sent', 'failed')."""
    db = _get_db()
    plans = db.table("plans")
    plans.update({"status": status}, where("plan_id") == plan_id)
    db.close()


# ==============================================================================
# 3. LOCAL STORE (Conversations & Messages on Disk)
# ==============================================================================

STORE_DIR = WHATSON_HOME / "store"
ID_MAP_PATH = STORE_DIR / "id_map.json"

def _ensure_store() -> None:
    """Create the store directory if needed."""
    STORE_DIR.mkdir(parents=True, exist_ok=True)

def _conversation_dir(name: str) -> Path:
    """Return the directory for a specific conversation (sanitized name)."""
    safe_name = name.replace("/", "_").replace("\\", "_").strip()
    return STORE_DIR / safe_name

def _messages_path(name: str) -> Path:
    return _conversation_dir(name) / "messages.json"

# --- ID Map (stabile IDs) ---

def _load_id_map() -> Dict[str, Any]:
    """Load the persistent ID map. Format: {"next_id": N, "map": {"1": "Name", ...}}"""
    if ID_MAP_PATH.exists():
        with open(ID_MAP_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {"next_id": 1, "map": {}}

def _save_id_map(data: Dict[str, Any]) -> None:
    _ensure_store()
    with open(ID_MAP_PATH, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2, ensure_ascii=False)

def _register_conversation(name: str) -> int:
    """Register a conversation name and return its stable ID. If already known, return existing ID."""
    data = _load_id_map()
    # Check if already registered
    for sid, sname in data["map"].items():
        if sname == name:
            return int(sid)
            
    # Find lowest available ID starting from 1
    existing_ids = {int(k) for k in data["map"].keys() if str(k).isdigit()}
    new_id = 1
    while new_id in existing_ids:
        new_id += 1
        
    data["map"][str(new_id)] = name
    if new_id >= data.get("next_id", 1):
        data["next_id"] = new_id + 1
        
    _save_id_map(data)
    return new_id

def _resolve_id(id_or_name: str) -> str:
    """
    Resolve a numeric ID or name to a conversation name.
    Numbers are looked up in the ID map. Strings are returned as-is.
    """
    if id_or_name.strip().isdigit():
        data = _load_id_map()
        name = data["map"].get(id_or_name.strip())
        if name:
            return name
        raise typer.BadParameter(
            f"Keine Konversation mit ID {id_or_name}. 'whatson list' zeigt alle IDs."
        )
    return id_or_name

def _get_all_registered() -> List[Dict[str, Any]]:
    """Return all registered conversations with their stable IDs and metadata."""
    data = _load_id_map()
    result = []
    for sid, name in sorted(data["map"].items(), key=lambda x: int(x[0])):
        msg_count = 0
        msgs_path = _messages_path(name)
        if msgs_path.exists():
            try:
                with open(msgs_path, "r", encoding="utf-8") as f:
                    msgs = json.load(f)
                msg_count = len(msgs)
            except Exception:
                pass
        result.append({"id": int(sid), "name": name, "messages": msg_count})
    return result

# --- Messages ---

def save_messages(conversation_name: str, messages: List[Dict[str, str]]) -> None:
    """Save messages for a conversation to disk (overwrites)."""
    _ensure_store()
    cdir = _conversation_dir(conversation_name)
    cdir.mkdir(parents=True, exist_ok=True)
    with open(_messages_path(conversation_name), "w", encoding="utf-8") as f:
        json.dump(messages, f, indent=2, ensure_ascii=False)

def load_messages(conversation_name: str) -> List[Dict[str, str]]:
    """Load messages for a conversation from disk. Returns [] if not found."""
    path = _messages_path(conversation_name)
    if not path.exists():
        return []
    with open(path, "r", encoding="utf-8") as f:
        return json.load(f)

def append_messages(conversation_name: str, new_messages: List[Dict[str, str]]) -> int:
    """Append new messages to the local store. Returns number of appended messages."""
    existing = load_messages(conversation_name)
    if not new_messages:
        return 0
    existing.extend(new_messages)
    save_messages(conversation_name, existing)
    return len(new_messages)

def _find_new_messages(
    local_messages: List[Dict[str, str]],
    remote_messages: List[Dict[str, str]],
) -> List[Dict[str, str]]:
    """
    Compare local vs remote messages and return only the new ones.
    Uses a set of (time, text) tuples — order-independent, robust against
    reverse()/scroll-order artifacts.
    """
    if not local_messages:
        return remote_messages
    if not remote_messages:
        return []

    known = {(m.get("time", ""), m.get("text", "")) for m in local_messages}
    return [m for m in remote_messages if (m.get("time", ""), m.get("text", "")) not in known]


# ==============================================================================
# 4. WHATSAPP ENGINE (Playwright)
# ==============================================================================

WHATSAPP_URL = "https://web.whatsapp.com"

# Selector for the QR code element on the login page
SEL_QR = "canvas, [data-testid='qrcode'], div[data-ref]"

# Selectors (supporting EN + DE locales)
SEL_SEARCH_BOX = '[aria-label="Search input textbox"], [aria-label="Sucheingabefeld"], [title="Search input textbox"], [title="Sucheingabefeld"], div[contenteditable="true"][data-tab="3"]'
SEL_SEARCH_BUTTON = '[title="Search input textbox"], [title="Sucheingabefeld"], button[aria-label="Search or start new chat"], button[aria-label="Suchen oder neuen Chat starten"]'
SEL_CHAT_ROWS = (
    '[aria-label="Chat list"] [role="listitem"], '
    '[aria-label="Chat list"] [role="row"], '
    '[aria-label="Chatliste"] [role="listitem"], '
    '[aria-label="Chatliste"] [role="row"]'
)
SEL_MSG_INPUT = '[aria-label="Type a message"], [aria-label="Nachricht eingeben"], div[contenteditable="true"][data-tab="10"]'
SEL_SEND_BUTTON = 'button[aria-label="Send"], button[aria-label="Senden"], span[data-icon="send"]'
SEL_SIDE_PANEL = "#pane-side"

LOAD_TIMEOUT = 60_000
NAV_TIMEOUT = 30_000
MAX_GOTO_RETRIES = 3
RETRY_DELAY_SECS = 3


def _render_qr_terminal(data: str) -> None:
    """Render QR code data as ASCII art directly in the terminal."""
    try:
        import qrcode as _qrcode
        qr = _qrcode.QRCode(border=1)
        qr.add_data(data)
        qr.make(fit=True)
        qr.print_ascii(invert=True)
    except Exception as exc:
        print(f"[whatson] QR-Render-Fehler: {exc}", file=sys.stderr)


class WhatsAppEngine:
    """Manages a Playwright browser session for WhatsApp Web."""

    def __init__(self, headless: Optional[bool] = None) -> None:
        self.cfg: Dict[str, Any] = get_config()
        self._pw: Optional[Playwright] = None
        self._context: Optional[BrowserContext] = None
        self._page: Optional[Page] = None
        # Explicitly requested headless state overrides config later
        self._explicit_headless: Optional[bool] = headless

    def start(self, force_headed: bool = False) -> None:
        """Launch (or connect to) the browser with a persistent context."""
        user_data_dir = self.cfg["user_data_dir"]
        Path(user_data_dir).mkdir(parents=True, exist_ok=True)

        headless = self.cfg.get("browser_headless", True)
        if self._explicit_headless is not None:
            headless = self._explicit_headless
        if force_headed:
            headless = False

        self._pw = sync_playwright().start()
        self._context = self._pw.chromium.launch_persistent_context(
            user_data_dir=user_data_dir,
            headless=headless,
            args=[
                "--disable-blink-features=AutomationControlled",
                "--no-sandbox",
            ],
            user_agent=(
                "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
            ),
            viewport={"width": 1280, "height": 900},
            locale="en-US",
        )
        self._page = self._context.pages[0] if self._context.pages else self._context.new_page()

    def stop(self) -> None:
        """Close browser and Playwright cleanly."""
        self._page = None
        if self._context:
            try:
                self._context.close()
            except Exception:
                pass
            self._context = None
        if self._pw:
            try:
                self._pw.stop()
            except Exception:
                pass
            self._pw = None

    @property
    def page(self) -> Page:
        """Always returns the current live page."""
        if self._page is None:
            raise RuntimeError("Engine not started — call start() first.")
        return self._page

    def _goto_with_retry(self, url: str) -> None:
        """Navigate to a URL with automatic retry on network errors."""
        last_error: Optional[Exception] = None
        for attempt in range(1, MAX_GOTO_RETRIES + 1):
            try:
                self.page.goto(url, wait_until="domcontentloaded", timeout=LOAD_TIMEOUT)
                return
            except (PlaywrightError, PlaywrightTimeout) as exc:
                last_error = exc
                err_msg = str(exc)
                if "ERR_NETWORK" in err_msg or "ERR_NAME" in err_msg or "ERR_CONNECTION" in err_msg:
                    print(
                        f"[whatson] Network error (attempt {attempt}/{MAX_GOTO_RETRIES}): {err_msg[:80]}",
                        file=sys.stderr,
                    )
                    if attempt < MAX_GOTO_RETRIES:
                        time.sleep(RETRY_DELAY_SECS)
                    continue
                raise
        raise RuntimeError(f"Failed to navigate to {url} after {MAX_GOTO_RETRIES} attempts: {last_error}")

    def open_whatsapp(self) -> None:
        self._goto_with_retry(WHATSAPP_URL)

    def wait_for_login(self, timeout_seconds: int = 120) -> bool:
        """Wait until the side panel appears (user has logged in)."""
        try:
            self.page.wait_for_selector(SEL_SIDE_PANEL, timeout=timeout_seconds * 1000)
            return True
        except PlaywrightTimeout:
            return False

    def _extract_qr_data(self) -> str:
        """Try to extract raw QR code data from the WhatsApp Web page.

        Attempts DOM attribute extraction first (fast), then injects jsQR to
        decode the canvas pixel data (works with all WhatsApp Web versions).
        Returns empty string if nothing found.
        """
        # 1) DOM attribute – older WA Web versions stored it in data-ref
        qr_data: str = self.page.evaluate(
            "document.querySelector('div[data-ref]')?.dataset?.ref"
            " || document.querySelector('[data-ref]')?.dataset?.ref"
            " || document.querySelector('[data-testid=\"qrcode\"] [data-ref]')?.dataset?.ref"
            " || ''"
        )
        if qr_data and len(qr_data) > 10:
            return qr_data

        # 2) Canvas decode via jsQR (works with current WA Web versions)
        try:
            self.page.add_script_tag(
                url="https://cdn.jsdelivr.net/npm/jsqr@1.4.0/dist/jsQR.js"
            )
            self.page.wait_for_timeout(800)
            qr_data = self.page.evaluate("""() => {
                const canvas = document.querySelector('canvas');
                if (!canvas) return '';
                try {
                    const ctx = canvas.getContext('2d');
                    const d = ctx.getImageData(0, 0, canvas.width, canvas.height);
                    const code = jsQR(d.data, d.width, d.height);
                    return code ? code.data : '';
                } catch(e) { return ''; }
            }""")
            if qr_data and len(qr_data) > 10:
                return qr_data
        except Exception:
            pass

        return ""

    def auth_qr_headless(self, qr_path: Optional[Path] = None, timeout_seconds: int = 180) -> None:
        """Authenticate via QR code while staying headless (SSH-compatible).

        Decodes the QR code data from the page and renders it as ASCII art
        directly in the terminal — no display or image viewer required.
        Falls back to saving a screenshot only if decoding fails.
        """
        if qr_path is None:
            qr_path = Path("/tmp/whatson-qr.png")

        print("[whatsON] QR-Code Scan erforderlich. Gleich im Terminal angezeigt …", file=sys.stderr)

        deadline = time.monotonic() + timeout_seconds
        last_render = 0.0

        while time.monotonic() < deadline:
            now = time.monotonic()
            if now - last_render >= 18:
                try:
                    qr_data = self._extract_qr_data()
                    if qr_data:
                        _render_qr_terminal(qr_data)
                        print("[whatsON] Bitte jetzt QR-Code mit dem Handy scannen …", file=sys.stderr)
                    else:
                        # Last resort: save screenshot
                        el = self.page.query_selector(SEL_QR)
                        if el:
                            el.screenshot(path=str(qr_path))
                        else:
                            self.page.screenshot(path=str(qr_path))
                        print(
                            f"[whatsON] QR-Code als Screenshot gespeichert: {qr_path}\n"
                            f"[whatsON] Tipp: scp <server>:{qr_path} . — dann lokal öffnen.",
                            file=sys.stderr,
                        )
                    last_render = time.monotonic()
                except Exception as exc:
                    print(f"[whatsON] QR-Fehler: {exc}", file=sys.stderr)

            try:
                self.page.wait_for_selector(SEL_SIDE_PANEL, timeout=2_000)
                print("[whatsON] Erfolgreich authentifiziert! Speichere Session …", file=sys.stderr)
                # Wait for WhatsApp to fully write its auth tokens to IndexedDB/cookies
                self.page.wait_for_timeout(5_000)
                print("[whatsON] Session gespeichert.", file=sys.stderr)
                return
            except PlaywrightTimeout:
                pass

        raise RuntimeError("Login-Timeout — QR-Code wurde nicht gescannt.")

    def ensure_authenticated(self) -> None:
        """Open WhatsApp Web and ensure we are logged in."""
        if self._page is None:
            self.start()

        current_url = self.page.url or ""
        if "web.whatsapp.com" in current_url:
            try:
                self.page.wait_for_selector(SEL_SIDE_PANEL, timeout=30_000)
                return
            except PlaywrightTimeout:
                pass

        self.open_whatsapp()

        try:
            self.page.wait_for_selector(SEL_SIDE_PANEL, timeout=45_000)
            return
        except PlaywrightTimeout:
            pass

        raise NotAuthenticatedError(
            "Nicht authentifiziert — bitte zuerst 'wo auth' ausführen."
        )

    def _search_and_open_chat(self, name: str) -> None:
        """Use the search box to find and open a conversation by name."""
        _t0 = time.monotonic()
        def _vt(msg):
            if VERBOSE:
                elapsed = time.monotonic() - _t0
                typer.echo(f"    [verbose] [{elapsed:.1f}s] {msg}", err=True)

        self.page.keyboard.press("Escape")
        self.page.wait_for_timeout(300)
        _vt("Escape gedrückt")

        try:
            search_btn = self.page.query_selector(SEL_SEARCH_BUTTON)
            if search_btn:
                search_btn.click()
                self.page.wait_for_timeout(500)
        except Exception:
            pass
        _vt("Suchbutton geklickt")

        search_box = self.page.wait_for_selector(SEL_SEARCH_BOX, timeout=10_000)
        if search_box is None:
            raise RuntimeError("Could not find the search box on WhatsApp Web.")
        search_box.click()
        search_box.fill("")
        self.page.wait_for_timeout(300)
        _vt("Suchfeld gefunden und geleert")

        search_box.fill(name)
        self.page.wait_for_timeout(2000)
        _vt(f"Suchbegriff '{name}' eingegeben")

        handle = self.page.evaluate_handle("""(searchTerm) => {
            const lowerName = searchTerm.trim().toLowerCase();
            const spans = document.querySelectorAll('#pane-side span[title]');
            for (const span of spans) {
                const text = (span.getAttribute('title') || '').toLowerCase();
                if (text.includes(lowerName)) {
                    return span.closest('[role="listitem"]') || span.closest('[role="row"]') || span;
                }
            }
            const rows = document.querySelectorAll('#pane-side [role="listitem"], #pane-side [role="row"]');
            for (const row of rows) {
                const span = row.querySelector('span[title]');
                if (span) {
                    const text = (span.getAttribute('title') || '').toLowerCase();
                    if (text.includes(lowerName)) {
                        return row;
                    }
                }
            }
            if (spans.length > 0) {
                return spans[0].closest('[role="listitem"]') || spans[0].closest('[role="row"]') || spans[0];
            }
            return null;
        }""", name)
        _vt("Suchergebnis-Element gefunden")

        el = handle.as_element() if handle else None
        if el:
            try:
                el.scroll_into_view_if_needed(timeout=3000)
                self.page.wait_for_timeout(300)
            except Exception:
                pass
            _vt("Element sichtbar gescrollt")
            try:
                el.click(timeout=5000)
            except Exception:
                el.dispatch_event("click")
            _vt("Chat angeklickt — warte auf Laden")
            # Kurz warten, aber NICHT zu lang — große Gruppen brauchen ewig zum Rendern
            self.page.wait_for_timeout(1000)
            _vt("Chat-Klick abgeschlossen")
            return

        raise RuntimeError(f"Conversation '{name}' not found.")

    def get_conversations(self) -> List[Dict[str, Any]]:
        """Scrape the left-hand chat list."""
        self.ensure_authenticated()
        self.page.wait_for_timeout(2000)

        conversations = self.page.evaluate("""() => {
            const results = [];
            const rows = document.querySelectorAll(
                '[aria-label="Chat list"] [role="listitem"], ' +
                '[aria-label="Chat list"] [role="row"], ' +
                '#pane-side [role="listitem"], ' +
                '#pane-side [role="row"]'
            );
            
            let idx = 1;
            for (const row of rows) {
                const titleEl = row.querySelector("span[title]");
                const chatName = titleEl ? titleEl.getAttribute("title") : null;
                if (!chatName) continue;
                
                let timeText = "";
                const spans = row.querySelectorAll('span');
                for (const s of spans) {
                    const t = s.innerText.trim();
                    if (!t || t === chatName) continue;
                    if (t.length <= 20 && (
                        /\\d{1,2}[:\\/.]\\d{2}/.test(t) ||
                        /yesterday|gestern|today|heute/i.test(t) ||
                        /\\d{1,2}[.\\/]\\d{1,2}/.test(t)
                    )) {
                        timeText = t;
                        break;
                    }
                }
                
                results.push({
                    "id": idx++,
                    "name": chatName,
                    "time": timeText,
                    "row_text": row.innerText
                });
            }
            return results;
        }""")

        return conversations

    # JS snippet used by get_chat_history to extract visible messages from the DOM
    _JS_EXTRACT_MESSAGES = """() => {
        const results = [];
        const allElements = document.querySelectorAll('div.copyable-text');
        for (let i = 0; i < allElements.length; i++) {
            const el = allElements[i];
            const pre = el.getAttribute('data-pre-plain-text') || '';

            let text = '';
            const selectable = el.querySelector('span.selectable-text');
            if (selectable) {
                text = selectable.innerText.trim();
            } else {
                const ltr = el.querySelector('span[dir="ltr"]');
                if (ltr) text = ltr.innerText.trim();
                else text = el.innerText.trim();
            }

            if (!text || text.length < 1) continue;

            const msgParent = el.closest('[class*="message-"]');
            const classes = msgParent ? msgParent.className : '';
            const direction = classes.includes('message-out') ? 'out' : 'in';

            results.push({ pre: pre, text: text, direction: direction });
        }
        return results;
    }"""

    def get_chat_history(self, conversation: str, limit: int = 50, stop_at_msg: Optional[Dict[str, str]] = None) -> List[Dict[str, str]]:
        """Open a chat and scrape text messages.

        Uses an accumulation strategy: after each scroll-up, visible messages
        are extracted and merged (by unique key) into a collection.

        If stop_at_msg is provided containing 'text' and 'time', it stops scraping
        as soon as this message is hit, dramatically speeding up `fetch`.
        """
        MAX_SCROLL_TIME_SECS = 180     # Gesamtes Scroll-Zeitlimit (großzügig für große Chats)
        MAX_STAGNATION_ROUNDS = 8      # Nach N stagnierenden Runden abbrechen
        BASE_WAIT_MS = 2500            # Basis-Wartezeit nach jedem Scroll
        STAGNATION_WAIT_MS = 4000      # Längere Wartezeit bei Stagnation

        _hist_t0 = time.monotonic()
        def _vt(msg):
            if VERBOSE:
                elapsed = time.monotonic() - _hist_t0
                typer.echo(f"    [verbose] [{elapsed:.1f}s] {msg}", err=True)

        self.ensure_authenticated()
        _vt("Authentifizierung OK")

        if VERBOSE:
            typer.echo(f"    [verbose] Öffne Chat '{conversation}' ...", err=True)

        self._search_and_open_chat(conversation)
        _vt("Chat gesucht und angeklickt")

        if VERBOSE:
            typer.echo(f"    [verbose] Chat geöffnet. Prüfe DOM ...", err=True)

        # Warten bis #main da ist, dann bis Nachrichten gerendert sind
        try:
            self.page.wait_for_selector('#main', timeout=10_000)
        except Exception:
            pass
        _vt("#main Selektor geprüft")
        try:
            self.page.wait_for_selector('div.copyable-text', timeout=8_000)
        except Exception:
            pass
        self.page.wait_for_timeout(1200)  # DOM stabilisieren lassen
        _vt("Nachrichten gerendert")

        main_panel = self.page.query_selector('#main')
        if not main_panel:
            typer.echo("    WARNUNG: Chat-Panel (#main) nicht gefunden!", err=True)
            return []

        msg_panel = self.page.query_selector(
            '[role="application"], [aria-label*="Message list"], '
            '[aria-label*="Nachrichtenliste"], #main [role="region"]'
        )
        if msg_panel is None:
            msg_panel = main_panel
        _vt("Message-Panel gefunden")

        # --- Akkumulationsstrategie ---
        # Nachrichten werden nach jedem Scroll extrahiert und in einem
        # dict gesammelt.  Der Schlüssel ist (pre, text), um
        # Duplikate über überlappende Scroll-Fenster hinweg zu vermeiden.
        accumulated: Dict[str, Dict[str, str]] = {}  # key -> {pre, text, direction}

        max_scroll_attempts = max(limit // 4, 15)  # Großzügig Scroll-Versuche erlauben
        stagnation_count = 0
        scroll_start = time.monotonic()

        typer.echo(f"  \u2192 Lade Historie ...", err=True)
        if stop_at_msg:
            if VERBOSE:
                typer.echo(f"    [verbose] Optimierter Fetch-Modus aktiv. Suche nach letzter lokaler Nachricht.", err=True)

        # Klick ins Message-Panel um Fokus zu setzen (wichtig für Keyboard-Scroll)
        try:
            if msg_panel:
                msg_panel.click()
                self.page.wait_for_timeout(300)
        except Exception:
            pass
        
        # Helper to check if we hit the stop message
        def _check_found_stop_msg(msgs: List[Dict[str, str]]) -> bool:
            if not stop_at_msg:
                return False
            target_text = stop_at_msg.get('text', '')
            target_time = stop_at_msg.get('time', '')
            for m in msgs:
                m_text = m.get('text', '')
                m_time = (m.get('pre', '')).strip("[] ")
                if m_text == target_text and m_time == target_time:
                    return True
            return False

        # Erste Extraktion (aktuelle Sicht, noch ohne Scrollen)
        initial_batch = self.page.evaluate(self._JS_EXTRACT_MESSAGES)
        found_stop = False
        for rm in (initial_batch or []):
            key = f"{rm.get('pre', '')}|||{rm.get('text', '')}"
            if key not in accumulated:
                accumulated[key] = rm
                
        if initial_batch and _check_found_stop_msg(initial_batch):
            # stop_msg gefunden, aber es könnten noch NEUERE Nachrichten nach ihr
            # existieren die noch nicht im DOM sind → einmal extra warten + nochmal extrahieren
            self.page.wait_for_timeout(2000)
            extra_batch = self.page.evaluate(self._JS_EXTRACT_MESSAGES)
            for rm in (extra_batch or []):
                key = f"{rm.get('pre', '')}|||{rm.get('text', '')}"
                if key not in accumulated:
                    accumulated[key] = rm
            found_stop = True

        prev_unique_count = len(accumulated)

        sys.stderr.write(f"\r    ... {prev_unique_count} Nachrichten gefunden (initial) ...\x1b[K")
        sys.stderr.flush()

        for attempt in range(max_scroll_attempts):
            if found_stop:
                if VERBOSE:
                    sys.stderr.write(f"\n    [verbose] Letzte lokale Nachricht gefunden! Breche Scrollen frühzeitig ab.\n")
                break
                
            # Limit schon erreicht?
            if len(accumulated) >= limit:
                if VERBOSE:
                    sys.stderr.write(f"\n    [verbose] Limit von {limit} erreicht ({len(accumulated)} gesammelt).\n")
                break

            # Zeitlimit prüfen
            elapsed = time.monotonic() - scroll_start
            if elapsed > MAX_SCROLL_TIME_SECS:
                sys.stderr.write(f"\n    [Zeitlimit] Scroll nach {elapsed:.0f}s abgebrochen ({len(accumulated)} Nachrichten gesammelt).\n")
                sys.stderr.flush()
                break

            # --- Scroll nach oben ---
            # WhatsApp Web virtualisiert den DOM: nur ~30-50 Nachrichten sind
            # gleichzeitig gerendert. Man muss *wirklich* scrollen um ältere
            # Nachrichten nachzuladen (nicht nur scrollTop = 0 setzen!).
            #
            # Strategie: Kombination aus wheel-Events und Keyboard-Scrolling.
            # wheel-Events mit negativem deltaY simulieren echtes Mausrad-Scrollen
            # und triggern WhatsApp Webs lazy-loading am zuverlässigsten.
            # --- Scroll nach oben ---
            # Wir suchen den echten Scroll-Container dynamisch, indem wir vom
            # ersten sichtbaren Nachrichten-Element nach oben gehen, bis wir
            # ein Element mit overflow-y: scroll|auto finden. Dann setzen wir
            # scrollTop auf 0, um den Lade-Trigger zuverlässig auszulösen.
            try:
                self.page.evaluate("""() => {
                    const msgs = document.querySelectorAll('div.copyable-text');
                    if (msgs.length === 0) return;
                    
                    let el = msgs[0];
                    let scrollContainer = null;
                    while (el && el !== document.body) {
                        const style = window.getComputedStyle(el);
                        if (style.overflowY === 'scroll' || style.overflowY === 'auto') {
                            scrollContainer = el;
                            break;
                        }
                        el = el.parentElement;
                    }
                    
                    if (scrollContainer) {
                        // Ein bisschen "anlauf" nehmen, manchmal ignoriert WA den Trigger, 
                        // wenn es schon genau auf 0 steht.
                        if (scrollContainer.scrollTop === 0) {
                            scrollContainer.scrollTop = 100;
                        }
                        setTimeout(() => {
                            if (scrollContainer) scrollContainer.scrollTop = 0;
                        }, 50);
                    } else {
                        // Fallback
                        msgs[0].scrollIntoView();
                    }
                }""")
            except Exception as e:
                msg = str(e).replace('\\n', ' ')
                if VERBOSE:
                    sys.stderr.write(f"\n    [verbose] JS Scroll Error: {msg[:100]}\n")
            
            # Ein bisschen Keyboard Pfeil-Hoch zur Sicherheit
            try:
                self.page.keyboard.press("ArrowUp")
            except Exception:
                pass

            # Dynamisch prüfen, ob das Sync-Banner da ist (nur im Haupt-Chatfenster!)
            is_syncing = False
            try:
                is_syncing = self.page.evaluate("""() => {
                    const mainPanel = document.querySelector('#main');
                    if (!mainPanel) return false;
                    const banners = mainPanel.querySelectorAll('div');
                    for (const b of banners) {
                        if (b.innerText && b.innerText.includes('werden synchronisiert')) {
                            return true;
                        }
                    }
                    return false;
                }""")
            except Exception:
                pass

            wait_ms = STAGNATION_WAIT_MS if stagnation_count > 0 else BASE_WAIT_MS
            self.page.wait_for_timeout(wait_ms)

            # Nachrichten aus dem aktuellen DOM extrahieren und akkumulieren
            batch = self.page.evaluate(self._JS_EXTRACT_MESSAGES)
            for rm in (batch or []):
                key = f"{rm.get('pre', '')}|||{rm.get('text', '')}"
                if key not in accumulated:
                    accumulated[key] = rm
                    
            if batch and _check_found_stop_msg(batch):
                found_stop = True

            new_unique = len(accumulated) - prev_unique_count
            elapsed = time.monotonic() - scroll_start

            sys.stderr.write(f"\r    ... {len(accumulated)} Nachrichten gesammelt (Scroll {attempt+1}/{max_scroll_attempts}, +{new_unique} neu, {elapsed:.0f}s) ...\x1b[K")
            sys.stderr.flush()

            # Stagnation prüfen: keine neuen einzigartigen Nachrichten
            if new_unique == 0:
                stagnation_count += 1
                
                # Wir brechen beim Sync-Banner erst ab, wenn wir wirklich 3 Runden gehangen haben,
                # damit wir nicht wegen 1 Sekunde Lade-Lag sofort skippen.
                if is_syncing and stagnation_count >= 3:
                    if VERBOSE:
                        sys.stderr.write("\n    [verbose] Sync-Boundary im Chat-Panel erreicht (Hintergrund-Sync läuft). Breche ab.\n")
                        sys.stderr.flush()
                    typer.echo(
                        "\n    \u26a0\ufe0f WARNUNG: WhatsApp Web synchronisiert noch alte Nachrichten vom Handy.\n"
                        "    Das Ende des aktuell verfügbaren Verlaufs ist erreicht.\n",
                        err=True
                    )
                    break
                    
                if stagnation_count >= MAX_STAGNATION_ROUNDS:
                    if VERBOSE:
                        sys.stderr.write(f"\n    [verbose] Scroll stagniert ({stagnation_count} Runden ohne neue Nachrichten), breche ab.\n")
                        sys.stderr.flush()
                    break
            else:
                stagnation_count = 0

            prev_unique_count = len(accumulated)

        sys.stderr.write("\n")
        sys.stderr.flush()

        # --- Nachrichten sortieren und zurückgeben ---
        # Die akkumulierten Nachrichten sind chronologisch unsortiert, da
        # wir von unten nach oben gescrollt haben (Neueste zuerst, dann Ältere).
        # Durch Reverse bringen wir sie in eine halbwegs chronologische Reihenfolge
        # (Älteste zuerst), damit "Erste Nachricht" auch wirklich die Älteste ist!
        all_raw = list(accumulated.values())
        all_raw.reverse()

        if VERBOSE:
            typer.echo(f"    [verbose] {len(all_raw)} einzigartige Nachrichten gesammelt", err=True)
            if all_raw:
                typer.echo(f"    [verbose] Älteste Nachricht: {all_raw[0]}", err=True)
            else:
                debug_html = self.page.evaluate("""() => {
                    const el = document.querySelector('div.copyable-text');
                    return el ? el.outerHTML.substring(0, 500) : 'KEIN ELEMENT';
                }""")
                typer.echo(f"    [verbose] DOM-Struktur: {debug_html}", err=True)

        messages: List[Dict[str, str]] = []
        for rm in all_raw:
            messages.append({
                "direction": rm.get("direction", "in"),
                "time": (rm.get("pre", "")).strip("[] "),
                "text": rm.get("text", ""),
            })

        # Nur die letzten `limit` Nachrichten zurückgeben
        messages = messages[-limit:]

        if VERBOSE:
            typer.echo(f"    {len(messages)} Nachrichten geparst", err=True)

        return messages

    def send_message(self, conversation: str, text: str) -> bool:
        """Open a chat, type the message, and send it."""
        self.ensure_authenticated()
        self._search_and_open_chat(conversation)
        self.page.wait_for_timeout(1000)

        msg_input = self.page.wait_for_selector(SEL_MSG_INPUT, timeout=NAV_TIMEOUT)
        if msg_input is None:
            raise RuntimeError("Could not locate the message input field.")

        msg_input.click()
        self.page.wait_for_timeout(300)

        lines = text.split("\\n")
        for i, line in enumerate(lines):
            self.page.keyboard.type(line, delay=20)
            if i < len(lines) - 1:
                self.page.keyboard.down("Shift")
                self.page.keyboard.press("Enter")
                self.page.keyboard.up("Shift")

        self.page.wait_for_timeout(300)

        send_btn = self.page.wait_for_selector(SEL_SEND_BUTTON, timeout=5000)
        if send_btn:
            send_btn.click()
        else:
            self.page.keyboard.press("Enter")

        self.page.wait_for_timeout(1000)
        return True

    def send_file(self, conversation: str, file_path: str, caption: str = "") -> bool:
        """Open a chat and send a file (image, document, etc.) with optional caption."""
        self.ensure_authenticated()
        self._search_and_open_chat(conversation)
        self.page.wait_for_timeout(1000)

        # Büroklammer-Button klicken
        attach_btn = self.page.query_selector(
            'span[data-icon="plus"], span[data-icon="attach-menu-plus"], '
            'span[data-icon="clip"], div[title="Attach"], div[title="Anhängen"]'
        )
        if attach_btn:
            attach_btn.click()
        else:
            # Fallback: direkt den + Button suchen
            plus_btn = self.page.query_selector('[data-testid="conversation-clip"], [aria-label="Attach"], [aria-label="Anhängen"]')
            if plus_btn:
                plus_btn.click()
            else:
                raise RuntimeError("Konnte den Anhang-Button nicht finden.")

        self.page.wait_for_timeout(1000)

        # Datei-Input finden und Datei setzen
        file_input = self.page.query_selector('input[type="file"]')
        if file_input is None:
            # Manchmal muss man erst "Dokument" oder "Fotos & Videos" klicken
            doc_option = self.page.query_selector(
                'span[data-icon="attach-document"], '
                'input[accept="*"]'
            )
            if doc_option:
                doc_option.click()
                self.page.wait_for_timeout(500)
            file_input = self.page.query_selector('input[type="file"]')

        if file_input is None:
            raise RuntimeError("Konnte den Datei-Input nicht finden.")

        file_input.set_input_files(file_path)
        self.page.wait_for_timeout(2000)

        # Caption eingeben falls vorhanden
        if caption:
            caption_input = self.page.query_selector(
                'div[contenteditable="true"][data-tab="10"], '
                'div[contenteditable="true"][role="textbox"]'
            )
            if caption_input:
                caption_input.click()
                self.page.wait_for_timeout(300)
                self.page.keyboard.type(caption, delay=20)
                self.page.wait_for_timeout(300)

        # Senden-Button klicken
        send_btn = self.page.query_selector(
            'span[data-icon="send"], div[aria-label="Send"], div[aria-label="Senden"]'
        )
        if send_btn:
            send_btn.click()
        else:
            self.page.keyboard.press("Enter")

        self.page.wait_for_timeout(2000)
        return True

    def get_last_message(self, conversation: str) -> Optional[Dict[str, str]]:
        """Return the last message in a conversation (for polling)."""
        history = self.get_chat_history(conversation, limit=1)
        return history[-1] if history else None

    def check_status(self) -> Dict[str, Any]:
        """Return basic status info without full authentication."""
        self.open_whatsapp()
        try:
            self.page.wait_for_selector(SEL_SIDE_PANEL, timeout=15_000)
            return {"authenticated": True}
        except PlaywrightTimeout:
            return {"authenticated": False}


# ==============================================================================
# 5. CLI APP (Typer)
# ==============================================================================

HELP_TEXT = f"""
whatsON v{__version__} — WhatsApp Web CLI tool, lokaler Speicher.

Nachrichten werden in ~/.whatson/store/ gespeichert.
Erster Schritt: 'wo get all' zum Herunterladen.
"""

EPILOG_TEXT = """
Schnellstart: wo get all && wo list && wo show 1
            (auch als 'whatson ...' aufrufbar)
"""

app = typer.Typer(
    name="whatson",
    help=HELP_TEXT,
    epilog=EPILOG_TEXT,
    add_completion=False,
    no_args_is_help=True,
)


def _json_out(data) -> None:
    """Pretty-print JSON to stdout."""
    typer.echo(json.dumps(data, indent=2, ensure_ascii=False))


def _resolve_name(name_or_id: str, engine: WhatsAppEngine) -> str:
    """If *name_or_id* is a pure integer, resolve it via stable ID map, then fallback to engine."""
    if name_or_id.strip().isdigit():
        # Try stable ID map first
        data = _load_id_map()
        name = data["map"].get(name_or_id.strip())
        if name:
            return name
        # Fallback to live resolve
        target_id = int(name_or_id.strip())
        convos = engine.get_conversations()
        for c in convos:
            if c.get("id") == target_id:
                return c["name"]
        raise typer.BadParameter(
            f"Konversation #{target_id} nicht gefunden."
        )
    return name_or_id


# ──────────────────────────────────────────────────────────────────
# DELETE – Konversation lokal löschen und ID freigeben
# ──────────────────────────────────────────────────────────────────

@app.command("delete", rich_help_panel="Verwaltung")
def delete_cmd(
    name: str = typer.Argument(..., help="ID oder Name der Konversation zum Löschen."),
):
    """
    Löscht eine Konversation lokal und gibt ihre ID wieder frei.
    Sie wird beim nächsten online 'whatson list' oder 'get' neu angelegt (ggf. mit einer neuen oder der alten freien ID).
    """
    try:
        resolved = _resolve_id(name)
    except typer.BadParameter as e:
        typer.echo(str(e), err=True)
        raise typer.Exit(1)
        
    data = _load_id_map()
    
    # Aus ID-Map entfernen
    deleted_id = None
    for k, v in list(data["map"].items()):
        if v == resolved:
            deleted_id = k
            del data["map"][k]
            break
            
    if deleted_id:
        _save_id_map(data)
        typer.echo(f"[whatson] Konversation '{resolved}' (ID {deleted_id}) aus der ID-Map entfernt.")
    else:
        typer.echo(f"[whatson] Warnung: '{resolved}' war nicht in der ID-Map verzeichnet.", err=True)
        
    # Ordner auf der Festplatte löschen
    conv_dir = _get_conv_dir(resolved)
    if conv_dir.exists():
        import shutil
        shutil.rmtree(conv_dir)
        typer.echo(f"[whatson] Lokale Daten für '{resolved}' wurden komplett gelöscht.")
    else:
        typer.echo(f"[whatson] Keine lokalen Festplattendaten für '{resolved}' gefunden.")

# ──────────────────────────────────────────────────────────────────
# LIST – Lokale Konversationen anzeigen (menschenlesbar)
# ──────────────────────────────────────────────────────────────────

@app.command("list", rich_help_panel="Verwaltung")
def list_cmd(
    offline: bool = typer.Option(False, "--offline", "-o", help="Überspringt den Online-Abgleich und zeigt nur lokale Daten"),
):
    """
    Zeigt alle Konversationen als Tabelle an.
    Gleicht standardmäßig mit WhatsApp Web ab, um den Synchron-Status zu prüfen.
    Bei Timeout oder ohne Internet wird automatisch auf die lokale Ansicht zurückgefallen.
    
    Beispiele:
        whatson list             Tabelle mit Online-Abgleich anzeigen
        whatson list --offline   Nur die lokale Tabelle anzeigen (schneller, ohne Browser)
    """
    local_entries = _get_all_registered()
    
    # Lade die letzten lokalen Nachrichten für den Abgleich
    local_data = {}
    for e in local_entries:
        msgs = load_messages(e["name"])
        last_msg = msgs[-1]["text"] if msgs else ""
        local_data[e["name"]] = {
            "id": e["id"],
            "messages": e["messages"],
            "last_text": last_msg
        }

    online_convs = []
    online = False
    
    if not offline:
        typer.echo("[whatson] Prüfe Online-Status (dauert kurz)...", err=True)
        engine = WhatsAppEngine()
        try:
            engine.start()
            # Nutze ein kurzes Timeout für get_conversations, falls kein Internet
            # Wir warten in get_conversations ohnehin auf #pane-side
            online_convs = engine.get_conversations()
            online = True
        except Exception as e:
            import traceback
            typer.echo(f"[whatson] Hinweis: Online-Abgleich fehlgeschlagen ({type(e).__name__}). Zeige nur lokale Liste.", err=True)
            online = False
        finally:
            engine.stop()

    if not local_entries and not online_convs:
        typer.echo("Keine Konversationen gefunden. Zuerst 'whatson get all' ausführen.", err=True)
        raise typer.Exit(1)

    # Zusammenführen für die Tabelle
    # Wenn online, zeigen wir die online-Reihenfolge. Sonst nur lokal.
    table_rows = []
    
    if online:
        for oc in online_convs:
            name = oc["name"]
            row_text = oc.get("row_text", "")
            
            if name in local_data:
                ld = local_data[name]
                sys_id = ld["id"]
                msg_count = ld["messages"]
                
                # Einfache Logik: Haben wir Nachrichten lokal?
                if msg_count > 0:
                    status = f"OK ({msg_count})"
                else:
                    status = "Leer"
            else:
                # Neu gefunden online -> direkt registrieren, damit Nutzer eine ID bekommt!
                sys_id = _register_conversation(name)
                msg_count = 0
                status = "Neu"
                
            table_rows.append({
                "id": sys_id,
                "name": name,
                "messages": msg_count,
                "status": status
            })
            
        # Füge lokale hinzu, die online nicht mehr sichtbar sind (z.B. versteckt/gelöscht)
        online_names = {oc["name"] for oc in online_convs}
        for name, ld in local_data.items():
            if name not in online_names:
                table_rows.append({
                    "id": ld["id"],
                    "name": name,
                    "messages": ld["messages"],
                    "status": "Nur Lokal"
                })
    else:
        for name, ld in local_data.items():
            table_rows.append({
                "id": ld["id"],
                "name": name,
                "messages": ld["messages"],
                "status": ""
            })

    # Tabelle formatieren
    def _dw(s):
        """Display-Breite eines Strings (Emojis/CJK = 2 Zeichen breit)."""
        w = 0
        for i, c in enumerate(s):
            cp = ord(c)
            eaw = unicodedata.east_asian_width(c)
            cat = unicodedata.category(c)
            
            if eaw in ('W', 'F'):  # Wide / Fullwidth (z.B. CJK, viele Emojis)
                w += 2
            elif cp == 0xFE0F:  # Variation Selector-16 (macht vorheriges Emoji breit)
                w += 1  # Das vorherige Zeichen wurde schon als 1 gezaehlt, jetzt +1
            elif cat in ('So', 'Sk') and eaw == 'A':  # Ambiguous-Breite Symbole (z.B. ⛷)
                w += 2
            elif cat == 'Mn':  # Combining marks (z.B. Variation Selectors) = 0
                pass
            else:
                w += 1
        return w

    def _pad(s, width):
        """String mit Leerzeichen auf Display-Breite auffüllen."""
        return s + ' ' * (width - _dw(s))

    id_w = max((len(str(r["id"])) for r in table_rows), default=2)
    name_w = max((_dw(r["name"]) for r in table_rows), default=4)
    name_w = max(min(name_w, 50), 23)  # Min 23, Max 50

    status_w = 8

    sep = f"+{'-' * (id_w + 2)}+{'-' * (name_w + 2)}+{'-' * 8}+"
    hdr = f"| {'ID'.ljust(id_w)} | {_pad('Name', name_w)} | {'Nachr.'} |"
    
    if online:
        sep += f"{'-' * (status_w + 2)}+"
        hdr += f" {_pad('Status', status_w)} |"

    typer.echo(sep)
    typer.echo(hdr)
    typer.echo(sep)
    
    # Für eine schöne Sortierung: ID zuerst (Zahlen), dann "Nur Online" (-)
    def sort_key(r):
        return int(r["id"]) if str(r["id"]).isdigit() else 999999
        
    for r in sorted(table_rows, key=sort_key):
        name_display = r["name"]
        # Kürzen falls nötig
        while _dw(name_display) > name_w:
            name_display = name_display[:-1]
        line = f"| {str(r['id']).rjust(id_w)} | {_pad(name_display, name_w)} | {str(r['messages']).rjust(6)} |"
        if online:
            line += f" {_pad(r['status'], status_w)} |"
        typer.echo(line)
        
    typer.echo(sep)
    typer.echo(f"  {len(table_rows)} Konversationen gesamt.")


# ──────────────────────────────────────────────────────────────────
# GET – Konversation(en) komplett herunterladen (überschreibt lokal)
# ──────────────────────────────────────────────────────────────────

@app.command("get", rich_help_panel="Nachrichten")
def get_cmd(
    name: str = typer.Argument("all", help="'all' für alle, oder ID/Name einer Konversation."),
    limit: int = typer.Option(100, "--limit", "-n", help="Max Nachrichten zum Laden. Standard ist 100. Klein anfangen, z.B. -n 20."),
    verbose: bool = typer.Option(False, "--verbose", "-v", help="Zeigt Debug-Informationen an."),
    ui: bool = typer.Option(False, "--ui", help="Öffnet den Browser sichtbar (hilft beim Syncen von alten Nachrichten)."),
):
    """
    Lädt Konversation(en) komplett herunter. ÜBERSCHREIBT lokale Daten!

    Standardmäßig werden bis zu 100 Nachrichten geladen. Mit -n lässt sich das anpassen.
    Tipp: Bei neuen Chats erstmal klein anfangen und rantasten (-n 20)!

    Beispiele:
        whatson get all -n 20       Alle Konversationen, je 20 Nachrichten laden
        whatson get all -n 20 -v    Alle Konversationen, 20 Nachrichten, mit ausführlichen Logs (Debug)
        whatson get all --limit 10  Das gleiche wie -n, aber explizit ausgeschrieben
        whatson get 4 -n 50         Konversation #4, 50 Nachrichten laden
        whatson get "Max" -n 200    Konversation "Max", 200 Nachrichten herunterladen
        whatson get 4 -n 50 -v      Konversation #4, 50 Nachrichten, mit Debug-Ausgabe laden
        whatson get 2 -n 1 --ui     Chat 2 laden und Browser sichtbar lassen (für Sync)
    """
    global VERBOSE
    VERBOSE = verbose
    if name.strip().lower() == "all":
        _get_all(limit, ui)
    else:
        _get_one(name, limit, ui)


def _get_all(limit: int, ui: bool = False):
    """Download ALL conversations and their messages from WhatsApp Web."""
    import signal

    PER_CONV_TIMEOUT = 300  # Max Sekunden pro Konversation

    class _ConvTimeout(Exception):
        pass

    def _timeout_handler(signum, frame):
        raise _ConvTimeout(f"Konversation brauchte länger als {PER_CONV_TIMEOUT}s")

    engine = WhatsAppEngine(headless=not ui)
    try:
        engine.start()
        typer.echo("[whatson] Lade Konversationen...", err=True)
        convos = engine.get_conversations()

        if not convos:
            typer.echo("[whatson] Keine Konversationen gefunden.", err=True)
            return

        typer.echo(f"[whatson] {len(convos)} Konversationen gefunden. Lade Nachrichten...", err=True)

        success_count = 0
        fail_count = 0
        skip_count = 0
        for c in convos:
            cname = c.get("name", "Unbekannt")
            stable_id = _register_conversation(cname)
            typer.echo(f"  [{stable_id}] {cname} ...", err=True)

            # SIGALRM-basierter Timeout pro Konversation
            old_handler = signal.signal(signal.SIGALRM, _timeout_handler)
            signal.alarm(PER_CONV_TIMEOUT)
            try:
                engine.page.keyboard.press("Escape")
                time.sleep(1)

                history = engine.get_chat_history(cname, limit=limit)
                signal.alarm(0)  # Timeout aufheben
                save_messages(cname, history)
                success_count += 1
                typer.echo(f"       -> {len(history)} Nachrichten gespeichert.", err=True)
            except _ConvTimeout:
                signal.alarm(0)
                typer.echo(f"       -> TIMEOUT nach {PER_CONV_TIMEOUT}s — übersprungen!", err=True)
                skip_count += 1
                # Recovery: Browser zurück in einen sauberen Zustand bringen
                try:
                    typer.echo(f"       -> Stelle Browser wieder her ...", err=True)
                    engine.page.goto("https://web.whatsapp.com", wait_until="domcontentloaded", timeout=30_000)
                    engine.page.wait_for_selector(SEL_SIDE_PANEL, timeout=15_000)
                    engine.page.keyboard.press("Escape")
                    engine.page.wait_for_timeout(1000)
                    typer.echo(f"       -> Browser OK, weiter geht's.", err=True)
                except Exception as rec_exc:
                    typer.echo(f"       -> Browser-Recovery fehlgeschlagen: {str(rec_exc)[:60]}", err=True)
                    typer.echo(f"       -> Breche ab.", err=True)
                    break
            except Exception as exc:
                signal.alarm(0)
                err_short = str(exc).split("\n")[0][:80]
                typer.echo(f"       -> ÜBERSPRUNGEN: {err_short}", err=True)
                fail_count += 1
            finally:
                signal.signal(signal.SIGALRM, old_handler)

            time.sleep(2)

        typer.echo(f"[whatson] Fertig! {success_count} OK, {fail_count} Fehler, {skip_count} Timeout.", err=True)
    finally:
        engine.stop()


def _get_one(name_or_id: str, limit: int, ui: bool = False):
    """Download a single conversation (overwrites local)."""
    engine = WhatsAppEngine(headless=not ui)
    try:
        engine.start()
        resolved = _resolve_name(name_or_id, engine)
        stable_id = _register_conversation(resolved)
        typer.echo(f"[whatson] Lade [{stable_id}] {resolved} ...", err=True)
        history = engine.get_chat_history(resolved, limit=limit)
        save_messages(resolved, history)
        typer.echo(f"[whatson] {len(history)} Nachrichten gespeichert (überschrieben).", err=True)
    finally:
        engine.stop()


# ──────────────────────────────────────────────────────────────────
# FETCH – Nur neue Nachrichten nachladen (hängt an lokale an)
# ──────────────────────────────────────────────────────────────────

@app.command("fetch", rich_help_panel="Nachrichten")
def fetch_cmd(
    name: str = typer.Argument("all", help="'all' für alle, oder ID/Name einer Konversation."),
    limit: int = typer.Option(50, "--limit", "-n", help="Max Nachrichten zum Vergleichen."),
    json_out: bool = typer.Option(False, "--json", help="Ergebnis als JSON ausgeben."),
):
    """
    Prüft auf neue Nachrichten und hängt diese an den lokalen Speicher an.

    Beispiele:
        whatson fetch all         Alle Konversationen auf neue Nachrichten prüfen
        whatson fetch all -n 10   Alle Konversationen, dabei jeweils max 10 Nachrichten vergleichen
        whatson fetch 4           Nur Konversation #4 auf Updates prüfen
        whatson fetch "Max"       Nur Konversation "Max" auf Updates prüfen
        whatson fetch 4 --json    Ergebnis als JSON (z.B. für Skripte)
    """
    if name.strip().lower() == "all":
        _fetch_all(limit, json_out=json_out)
    else:
        _fetch_one(name, limit, json_out=json_out)


@app.command("scan", rich_help_panel="Nachrichten")
def scan_cmd(
    name: str = typer.Argument(..., help="Chat-ID oder Name"),
    hook: Optional[str] = typer.Option(None, "-w", "--hook", help="Wachtler Hook-ID (z.B. 3b) – push bei neuen Nachrichten"),
    limit: int = typer.Option(50, "-n", "--limit", help="Max Nachrichten zum Vergleichen"),
    json_out: bool = typer.Option(False, "--json", help="Ergebnis als JSON ausgeben"),
):
    """
    Prüft eine Konversation auf neue Nachrichten.
    Bei neuen Nachrichten und gesetztem --hook/-w wird automatisch
    'wachtler <hookid> push <json>' aufgerufen, sodass der Wachtler-Daemon
    die definierte Hook-Aktion ausführt (z.B. TTY-Injection auf Ellas Konsole).

    Beispiele:
        whatson scan 2 -w 3b          Prüfe Chat 2, push an Hook 3b wenn neu
        whatson scan "KI Gruppe" -w 3b --json
    """
    resolved = _resolve_id(name)
    engine = WhatsAppEngine()
    result = {}
    try:
        engine.start()
        local_msgs = load_messages(resolved)
        stop_msg = local_msgs[-1] if local_msgs else None
        remote_msgs = engine.get_chat_history(resolved, limit=limit, stop_at_msg=stop_msg)
        new_msgs = _find_new_messages(local_msgs, remote_msgs)

        if new_msgs:
            count = append_messages(resolved, new_msgs)
            typer.echo(f"[whatson] {resolved}: +{count} neue Nachrichten.", err=True)
            result = {
                "name": resolved,
                "new_messages": count,
                "new": new_msgs,
                "status": "updated",
                "trigger_new_message": 1,
                "last": new_msgs[-1] if new_msgs else {}
            }
            if hook:
                data = json.dumps(result, ensure_ascii=False)
                r = subprocess.run(
                    ["wachtler", hook, "push", data],
                    capture_output=True, text=True
                )
                if r.returncode == 0:
                    typer.echo(f"[whatson] → Wachtler Hook {hook}: push ok", err=True)
                else:
                    typer.echo(f"[whatson] → Wachtler Hook {hook}: push fehlgeschlagen: {r.stderr.strip()}", err=True)
        else:
            typer.echo(f"[whatson] {resolved}: keine neuen Nachrichten.", err=True)
            result = {
                "name": resolved,
                "new_messages": 0,
                "status": "up_to_date",
                "trigger_new_message": 0,
            }
    finally:
        engine.stop()

    if json_out:
        _json_out(result)


def _fetch_all(limit: int, json_out: bool = False):
    entries = _get_all_registered()
    if not entries:
        typer.echo("Keine lokalen Konversationen. Zuerst 'whatson get all' ausführen.", err=True)
        raise typer.Exit(1)

    results = []
    engine = WhatsAppEngine()
    try:
        engine.start()
        total_new = 0

        for e in entries:
            cname = e["name"]
            try:
                if engine._page is None:
                    engine.start()
                engine.page.keyboard.press("Escape")
                time.sleep(1)

                local_msgs = load_messages(cname)
                stop_msg = local_msgs[-1] if local_msgs else None

                remote_msgs = engine.get_chat_history(cname, limit=limit, stop_at_msg=stop_msg)
                new_msgs = _find_new_messages(local_msgs, remote_msgs)

                if new_msgs:
                    count = append_messages(cname, new_msgs)
                    total_new += count
                    last = new_msgs[-1]
                    typer.echo(f"  [{e['id']}] {cname}: +{count} neue Nachrichten", err=True)
                    results.append({"id": e["id"], "name": cname, "new_messages": count, "status": "updated",
                                    "trigger_new_message": 1,
                                    "last_message": {"time": last.get("time", ""), "sender": last.get("sender", last.get("pre", "")), "text": last.get("text", "")}})
                else:
                    last = local_msgs[-1] if local_msgs else {}
                    typer.echo(f"  [{e['id']}] {cname}: aktuell ✓", err=True)
                    results.append({"id": e["id"], "name": cname, "new_messages": 0, "status": "up_to_date",
                                    "trigger_new_message": 0,
                                    "last_message": {"time": last.get("time", ""), "sender": last.get("sender", last.get("pre", "")), "text": last.get("text", "")}})
            except Exception as exc:
                err_short = str(exc).split("\n")[0][:80]
                typer.echo(f"  [{e['id']}] {cname}: FEHLER: {err_short}", err=True)
                results.append({"id": e["id"], "name": cname, "new_messages": 0, "status": "error", "error": err_short})

            time.sleep(2)

        typer.echo(f"[whatson] Fetch fertig. {total_new} neue Nachrichten insgesamt.", err=True)
    finally:
        engine.stop()

    if json_out:
        _json_out({"total_new": total_new, "conversations": results})


def _fetch_one(name_or_id: str, limit: int, json_out: bool = False):
    resolved = _resolve_id(name_or_id)

    engine = WhatsAppEngine()
    try:
        engine.start()
        local_msgs = load_messages(resolved)
        stop_msg = local_msgs[-1] if local_msgs else None

        remote_msgs = engine.get_chat_history(resolved, limit=limit, stop_at_msg=stop_msg)
        new_msgs = _find_new_messages(local_msgs, remote_msgs)

        if new_msgs:
            count = append_messages(resolved, new_msgs)
            total = len(local_msgs) + count
            last = new_msgs[-1]
            typer.echo(f"[whatson] {resolved}: +{count} neue Nachrichten (gesamt: {total})", err=True)
            typer.echo(f"  letzte neue: [{last.get('time', '?')}] {last.get('text', '')[:60]}", err=True)
            result = {"name": resolved, "new_messages": count, "total_messages": total,
                      "status": "updated", "trigger_new_message": 1,
                      "last_message": {"time": last.get("time", ""), "sender": last.get("sender", last.get("pre", "")), "text": last.get("text", "")}}
        else:
            all_local = load_messages(resolved)
            last = all_local[-1] if all_local else {}
            typer.echo(f"[whatson] {resolved}: keine neuen Nachrichten.", err=True)
            typer.echo(f"  letzte bekannte: [{last.get('time', '?')}] {last.get('text', '')[:60]}", err=True)
            result = {"name": resolved, "new_messages": 0, "total_messages": len(local_msgs),
                      "status": "up_to_date", "trigger_new_message": 0,
                      "last_message": {"time": last.get("time", ""), "sender": last.get("sender", last.get("pre", "")), "text": last.get("text", "")}}
    finally:
        engine.stop()

    if json_out:
        _json_out(result)


# ──────────────────────────────────────────────────────────────────
# IMPORT – WhatsApp TXT Export lokal einlesen
# ──────────────────────────────────────────────────────────────────

@app.command("import", rich_help_panel="Nachrichten")
def import_cmd(
    name: Optional[str] = typer.Argument(None, help="ID/Name der Konversation. Leer lassen für Auto-Import (Ordner ./import)"),
    filepath: Optional[str] = typer.Argument(None, help="Pfad zur WhatsApp .txt Export-Datei"),
    me: str = typer.Option("david", "--me", help="Dein eigener Name in der Export-Datei (für direction='out')"),
    import_dir: str = typer.Option("import", "--dir", help="Ordner für den automatischen Massen-Import"),
):
    """
    Importiert einen WhatsApp Chat-Export (.txt Datei vom Handy) in den lokalen Speicher.
    
    Da Whatson oft nicht alle alten Nachrichten aus WhatsApp Web laden kann, 
    kannst du den Chat am Handy exportieren ("Chat exportieren" -> "Ohne Medien") 
    und diese .txt Dateien einlesen.
    
    Ohne Argumente (Auto-Import):
        Liest alle .txt Dateien im Ordner 'import/' und ordnet sie automatisch
        deinen bekannten Chats zu. Startet den Import für jeden passenden Chat.
    
    Mit Argumenten (Manueller Import):
        Lädt gezielt EINE Datei in EINEN Chat.
    
    Das Skript erkennt iOS- und Android-Formate, überspringt Duplikate 
    und mischt die alten Nachrichten in deinen lokalen Verlauf.
    
    Beispiele:
        whatson import                                            (Auto-Import aus Ordner './import')
        whatson import 2 /home/david/Downloads/_chat.txt          (Manueller Import)
        whatson import "KI Gruppe" chat.txt --me "David Frölich"  (Manuell mit eigenem Namen)
    """
    # AUTO-IMPORT MODUS
    if not name and not filepath:
        target_dir = Path(import_dir).absolute()
        if not target_dir.exists() or not target_dir.is_dir():
            typer.echo(f"[FEHLER] Import-Ordner nicht gefunden: {target_dir}", err=True)
            typer.echo("Bitte erstelle den Ordner und lege dort die .txt Exporte ab:\n  mkdir -p import", err=True)
            raise typer.Exit(1)
            
        export_files = list(target_dir.glob("*.txt")) + list(target_dir.glob("*.zip"))
        if not export_files:
            typer.echo(f"[INFO] Keine .txt oder .zip Dateien in {target_dir} gefunden.", err=True)
            return

        typer.echo(f"[AUTO-IMPORT] {len(export_files)} Dateien in {target_dir} gefunden.", err=True)
        known_chats = _get_all_registered()
        name_map = {c["name"].lower(): c["name"] for c in known_chats}
        
        for p in export_files:
            base_name = p.stem
            # Bekannte WhatsApp Praefixe abziehen
            prefixes = ["WhatsApp Chat mit ", "WhatsApp-Chat mit ", "WhatsApp Chat - "]
            chat_name = base_name
            for pre in prefixes:
                if chat_name.startswith(pre):
                    chat_name = chat_name[len(pre):]
            # Entferne potenzielles "(1)", "(2)" usw. am Ende, das von Downloads kommt
            import re
            chat_name = re.sub(r"\(\d+\)$", "", chat_name).strip()
            
            # Fuzzy Matching
            target_name = None
            if chat_name.lower() in name_map:
                target_name = name_map[chat_name.lower()]
            else:
                for known_lower, known_exact in name_map.items():
                    if chat_name.lower() in known_lower or known_lower in chat_name.lower():
                        target_name = known_exact
                        break
                        
            if not target_name:
                typer.echo(f"\n[ÜBERSPRUNGEN] Datei '{p.name}' -> Kein passender lokaler Chat gefunden für '{chat_name}'", err=True)
                continue
                
            typer.echo(f"\n[AUTO-IMPORT] '{p.name}' -> Match mit lokalem Chat: '{target_name}'", err=True)
            _import_txt(target_name, p, me)
            
        typer.echo("\n[AUTO-IMPORT] Abgeschlossen.", err=True)
        return

    # EINGABE FEHLER?
    if (name and not filepath) or (filepath and not name):
        typer.echo("[FEHLER] Für einen manuellen Import müssen Konversations-ID UND Dateipfad angegeben werden.", err=True)
        typer.echo("Oder rufe 'whatson import' ganz ohne Argumente für den Auto-Import auf.", err=True)
        raise typer.Exit(1)

    # MANUELLER IMPORT MODUS
    if name.strip().lower() == "all":
        typer.echo("[FEHLER] 'all' wird beim Import nicht unterstützt.", err=True)
        raise typer.Exit(1)
        
    path = Path(filepath)
    if not path.is_file():
        typer.echo(f"[FEHLER] Datei nicht gefunden: {filepath}", err=True)
        raise typer.Exit(1)

    # Naming auflösen (Engine kurz starten, falls es eine neue Nummer ist)
    resolved = name
    if name.strip().isdigit():
        try:
            resolved = _resolve_id(name)
        except Exception:
            typer.echo(f"[FEHLER] ID {name} lokal nicht bekannt. Starte vorher 'whatson get' für diesen Chat.", err=True)
            raise typer.Exit(1)
            
    _import_txt(resolved, path, me)


def _import_txt(conversation_name: str, filepath: Path, my_name: str):
    import re
    import zipfile
    
    typer.echo(f"[whatson] Lese Export-Datei: {filepath.name} ...", err=True)
    
    lines = []
    if filepath.suffix.lower() == ".zip":
        try:
            with zipfile.ZipFile(filepath, "r") as z:
                txt_files = [f for f in z.namelist() if f.lower().endswith(".txt")]
                if not txt_files:
                    typer.echo(f"  -> Abbruch. Keine .txt Datei im ZIP gefunden.", err=True)
                    return
                with z.open(txt_files[0], "r") as f:
                    lines = f.read().decode("utf-8", errors="replace").splitlines()
        except Exception as e:
            typer.echo(f"  -> Fehler beim Lesen der ZIP-Datei: {e}", err=True)
            return
    else:
        with open(filepath, "r", encoding="utf-8", errors="replace") as f:
            lines = f.readlines()
        
    # Regex für iOS und Android Exporte
    # Android: 18.02.26, 08:02 - Sender: Text
    # Android alt: 18.2.2026, 08:02 - Sender: Text
    # iOS: [18.02.26, 08:02:15] Sender: Text
    # iOS alt: [18.2.26, 08:02:15] Sender: Text
    msg_regex = re.compile(
        r"^(?:\[)?(\d{1,2}\.\d{1,2}\.\d{2,4})[.,]?\s+(\d{1,2}:\d{2}(?::\d{2})?)(?:\]\s+|\s+-\s+)([^:]+?):\s+(.*)$"
    )
    
    system_regex_ios = re.compile(r"^\[\d{1,2}\.\d{1,2}\.\d{2,4}[.,]?\s+\d{1,2}:\d{2}(?::\d{2})?\]\s+(.*)$")
    system_regex_android = re.compile(r"^\d{1,2}\.\d{1,2}\.\d{2,4}[.,]?\s+\d{1,2}:\d{2}\s+-\s+(.*)$")

    parsed_messages = []
    current_msg = None

    for line in lines:
        line_clean = line.strip("\n\r")
        if not line_clean:
            continue
            
        match = msg_regex.match(line_clean)
        
        if match:
            # Neue Nachricht gefunden
            if current_msg:
                parsed_messages.append(current_msg)
                
            raw_date = match.group(1)
            raw_time = match.group(2)
            sender = match.group(3).strip()
            text = match.group(4)
            
            # Formate bereinigen für JSON
            clean_time = raw_time[:5] 
            
            try:
                parts = raw_date.split(".")
                day = str(int(parts[0]))
                month = str(int(parts[1]))
                year = parts[2]
                if len(year) == 2:
                    year = "20" + year
                clean_date = f"{day}.{month}.{year}"
            except Exception:
                clean_date = raw_date
                
            direction = "out" if sender == my_name else "in"
            pre = f"[{clean_time}, {clean_date}] {sender}: "
            
            current_msg = {
                "pre": pre,
                "text": text,
                "time": clean_time,
                "date": clean_date,
                "sender": sender,
                "direction": direction
            }
        else:
            # Check if it's a system message
            sys_match_ios = system_regex_ios.match(line_clean)
            sys_match_android = system_regex_android.match(line_clean)
            
            if sys_match_ios or sys_match_android:
                if current_msg:
                    parsed_messages.append(current_msg)
                    current_msg = None
                continue
                
            # Wenn weder neue Nachricht noch Systemnachricht: Multiline-Text!
            if current_msg:
                current_msg["text"] += f"\n{line_clean}"

    if current_msg:
        parsed_messages.append(current_msg)
        
    typer.echo(f"  -> {len(parsed_messages)} Nachrichten aus Datei geparst.", err=True)
    if not parsed_messages:
        typer.echo("  -> Abbruch. Keine lesbaren Nachrichten gefunden. Evtl. falsches Format?", err=True)
        return
        
    stable_id = _register_conversation(conversation_name)
    local_msgs = load_messages(conversation_name)
    
    def parse_msg_metadata(m: Dict[str, Any]):
        # Check if already modern
        d_str, t_str, sender = m.get("date", ""), m.get("time", ""), m.get("sender", "")
        dt = datetime.min
        
        raw = str(m.get("pre") or m.get("time", "")).replace("[", "").strip()
        
        if "date" not in m and "]" in raw:
            # Legacy format "[08:02, 18.2.2026] Christian:"
            left, right = raw.split("]", 1)
            sender = right.strip(" :")
            time_part, _, date_part = left.partition(",")
            t_str = time_part.strip()
            d_str = date_part.strip()
            
            # fix yy to yyyy
            parts = d_str.split(".")
            if len(parts) == 3 and len(parts[2]) == 2:
                parts[2] = "20" + parts[2]
                d_str = ".".join(parts)
                
        try:
            if d_str and t_str:
                dt = datetime.strptime(f"{d_str} {t_str}", "%d.%m.%Y %H:%M")
        except Exception:
            pass
            
        m["_parsed_dt"] = dt # Cache for sorting
        return d_str, t_str, sender, dt
        
    # Hash check time+sender+text
    existing_hashes = set()
    for m in local_msgs:
        d, t, s, dt = parse_msg_metadata(m)
        h = f"{t}|{s}|{m.get('text', '').strip()}"
        existing_hashes.add(h)
        
    new_added = 0
    merged_list = list(local_msgs)
    
    for pm in parsed_messages:
        d, t, s, dt = parse_msg_metadata(pm)
        h = f"{t}|{s}|{pm.get('text', '').strip()}"
        if h not in existing_hashes:
            merged_list.append(pm)
            existing_hashes.add(h)
            new_added += 1
            
    def sort_key(m):
        dt = m.get("_parsed_dt", datetime.min)
        if "_parsed_dt" in m:
            del m["_parsed_dt"]
        return dt
            
    merged_list.sort(key=sort_key)
    
    save_messages(conversation_name, merged_list)
    typer.echo(f"[whatson] Import erfolgreich für [{stable_id}] {conversation_name}!", err=True)
    typer.echo(f"  Vorher: {len(local_msgs)} Nachrichten", err=True)
    typer.echo(f"  Hinzugefügt: +{new_added} neue alte Nachrichten", err=True)
    typer.echo(f"  Gesamt jetzt: {len(merged_list)} Nachrichten", err=True)



# ──────────────────────────────────────────────────────────────────
# SHOW – Konversation(en) menschenlesbar anzeigen
# ──────────────────────────────────────────────────────────────────

@app.command("show", rich_help_panel="Verwaltung")
def show_cmd(
    name: str = typer.Argument(..., help="'all' für alle, oder ID/Name einer Konversation."),
    limit: int = typer.Option(0, "--limit", "-n", help="Max Nachrichten anzeigen (0 = alle)."),
    rtl: bool = typer.Option(False, "--rtl", help="RTL-Modus: Nachrichtentext wird umgekehrt (Verschlüsselung)."),
):
    """
    Zeigt eine Konversation menschenlesbar an.

    Beispiele:
        whatson show 4            Konversation #4 im Terminal komplett anzeigen
        whatson show 4 -n 10      Konversation #4, nur die letzten 10 Nachrichten anzeigen
        whatson show all          Alle lokal gespeicherten Konversationen zusammen ausgeben
        whatson show all --limit 5 Alle Konversationen, je nur die 5 aktuellsten Nachrichten
        whatson show 4 --rtl      Konversation #4 ausgeben und den Text dabei umdrehen (Entschlüsselung)
    """
    cfg = get_config()
    use_rtl = rtl or cfg.get("rtl_mode", False)
    
    if name.strip().lower() == "all":
        _show_all(limit, rtl=use_rtl)
    else:
        _show_one(name, limit, rtl=use_rtl)


def _format_message(msg: Dict[str, str], rtl: bool = False) -> str:
    """Format a single message for human-readable display."""
    direction = msg.get("direction", "?")
    text = msg.get("text", "")

    # Parse metadata cleanly
    d_str, t_str, sender = msg.get("date", ""), msg.get("time", ""), msg.get("sender", "")
    raw = str(msg.get("pre") or msg.get("time", "")).replace("[", "").strip()
        
    if "date" not in msg and "]" in raw:
        left, right = raw.split("]", 1)
        sender = right.strip(" :")
        time_part, _, date_part = left.partition(",")
        t_str = time_part.strip()
        d_str = date_part.strip()

    timestamp = f"{t_str}, {d_str}" if d_str else t_str
    
    if direction == "out":
        arrow = "\u2192 Du"
        if sender and sender.lower() != "du":
            arrow = f"\u2192 {sender}"
    else:
        arrow = f"\u2190 {sender}" if sender else "\u2190"

    header = f"  {arrow}"
    if timestamp:
        header += f"  [{timestamp}]"

    return f"{header}\n    {text}"


def _show_one(name_or_id: str, limit: int, rtl: bool = False):
    resolved = _resolve_id(name_or_id)
    messages = load_messages(resolved)

    if not messages:
        typer.echo(f"Keine Nachrichten f\u00fcr '{resolved}'. Zuerst 'whatson get {name_or_id}' ausf\u00fchren.", err=True)
        raise typer.Exit(1)

    if limit > 0:
        messages = messages[-limit:]

    # Header
    data = _load_id_map()
    sid = "?"
    for k, v in data["map"].items():
        if v == resolved:
            sid = k
            break

    typer.echo(f"")
    typer.echo(f"  [{sid}] {resolved}  ({len(messages)} Nachrichten)")
    typer.echo("  " + "\u2500" * 50)

    for msg in messages:
        typer.echo(_format_message(msg, rtl=rtl))
        typer.echo("")

    typer.echo("  " + "\u2500" * 50)


def _show_all(limit: int, rtl: bool = False):
    entries = _get_all_registered()
    if not entries:
        typer.echo("Keine lokalen Konversationen. Zuerst 'whatson get all' ausführen.", err=True)
        raise typer.Exit(1)

    for e in entries:
        _show_one(str(e["id"]), limit, rtl=rtl)


# ──────────────────────────────────────────────────────────────────
# CHAT – Nachricht senden (+ lokal speichern)
# ──────────────────────────────────────────────────────────────────

@app.command("send", rich_help_panel="Nachrichten")
def chat_cmd(
    name: str = typer.Argument(..., help="ID oder Name der Konversation."),
    text: str = typer.Argument(None, help="Nachrichtentext oder Pfad zu .txt Datei."),
    file: Optional[str] = typer.Option(None, "-f", "--file", help="Dateianhang senden."),
    image: Optional[str] = typer.Option(None, "-b", "--bild", help="Bild senden."),
    rtl: bool = typer.Option(False, "--rtl", help="RTL-Modus: Text wird umgedreht gesendet."),
):
    """
    Sendet eine Nachricht und speichert sie lokal.

    Beispiele:
        whatson send 4 "Hallo!"                Einfache Textnachricht an Konversation #4 senden
        whatson send "Max" "Wie geht's?"       Textnachricht an Konversation "Max" schicken
        whatson send 4 nachricht.txt           Den Inhalt der Datei nachricht.txt einlesen und senden
        whatson send 4 "Hier!" -f bericht.pdf  Eine PDF-Datei hochladen und dazu den Text "Hier!" schreiben
        whatson send 4 -b foto.jpg             Ausschließlich ein Bild an Konversation #4 senden
        whatson send 4 "Schau!" --rtl          Den Text "Schau!" umgedreht (!uahcS) als Nachricht senden
    """
    cfg = get_config()
    use_rtl = rtl or cfg.get("rtl_mode", False)

    # Attachment-Pfad bestimmen
    attachment = file or image
    if attachment and not os.path.isfile(attachment):
        typer.echo(f"Fehler: Datei nicht gefunden: {attachment}", err=True)
        raise typer.Exit(1)

    # Text bestimmen
    msg_text = text or ""

    # Wenn text eine existierende .txt-Datei ist und kein Attachment -> Inhalt als Nachricht
    if msg_text and os.path.isfile(msg_text) and msg_text.endswith(".txt") and not attachment:
        with open(msg_text, "r", encoding="utf-8") as f:
            msg_text = f.read().strip()
        typer.echo(f"[whatson] Sende Inhalt von '{text}'...", err=True)

    # Mindestens Text oder Attachment muss vorhanden sein
    if not msg_text and not attachment:
        typer.echo("Fehler: Nachricht oder Datei (-f/-b) angeben.", err=True)
        raise typer.Exit(1)

    # RTL-Modus: Text umdrehen
    if use_rtl and msg_text:
        msg_text = msg_text[::-1]

    engine = WhatsAppEngine()
    try:
        engine.start()
        resolved = _resolve_name(name, engine)

        if attachment:
            # Datei/Bild senden (mit optionalem Text als Caption)
            engine.send_file(resolved, attachment, caption=msg_text)
            label = os.path.basename(attachment)
            typer.echo(f"[whatson] Datei gesendet an '{resolved}': {label}")
            # Lokal speichern
            sent_msg = {
                "direction": "out",
                "time": f"[{datetime.now().strftime('%H:%M, %d.%m.%Y')}] Du:",
                "text": f"[Datei: {label}] {msg_text}".strip(),
            }
        else:
            # Nur Text senden
            engine.send_message(resolved, msg_text)
            typer.echo(f"[whatson] Gesendet an '{resolved}': {msg_text[:60]}")
            sent_msg = {
                "direction": "out",
                "time": f"[{datetime.now().strftime('%H:%M, %d.%m.%Y')}] Du:",
                "text": msg_text,
            }

        append_messages(resolved, [sent_msg])
    finally:
        engine.stop()


# ──────────────────────────────────────────────────────────────────
# JOBS – Job-Verwaltung (ersetzt alte plan-Befehle)
# ──────────────────────────────────────────────────────────────────

JOBS_PATH = WHATSON_HOME / "jobs.json"
SCHEDULER_PID_PATH = WHATSON_HOME / "scheduler.pid"
SCHEDULER_SCRIPT = Path(__file__).resolve().parent / "whatson_scheduler.py"

def _load_jobs() -> List[Dict[str, Any]]:
    if JOBS_PATH.exists():
        with open(JOBS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return []

def _save_jobs(jobs: List[Dict[str, Any]]) -> None:
    WHATSON_HOME.mkdir(parents=True, exist_ok=True)
    with open(JOBS_PATH, "w", encoding="utf-8") as f:
        json.dump(jobs, f, indent=2, ensure_ascii=False)

def _gen_job_id() -> str:
    return str(uuid.uuid4())[:6]


@app.command("job-add", rich_help_panel="Scheduler & Jobs")
def job_add_cmd(
    job_type: str = typer.Argument(..., help="Typ: 'message' oder 'fetch'"),
    target: str = typer.Argument(..., help="Konversation ID/Name oder 'all' (für fetch)"),
    text: Optional[str] = typer.Argument(None, help="Nachrichtentext (nur für message)"),
    schedule: str = typer.Option(..., "--schedule", "-s", help="z.B. 'every 30m', 'daily 08:00', 'once 2026-03-02 10:00'"),
):
    """
    Neuen Job anlegen.

    Beispiele:
        whatson job-add message 4 "Morgen!" -s "daily 08:00"            Jeden Tag um 08:00 an Konversation #4 senden
        whatson job-add message "Max" "Test" -s "every 30m"             Alle 30 Minuten "Test" an "Max" schicken
        whatson job-add fetch all -s "every 60m"                        Jede Stunde alle neuen Nachrichten automatisch laden
        whatson job-add fetch 4 -s "daily 12:00"                        Konversation #4 einmal täglich prüfen
        whatson job-add message 4 "Reminder" -s "once 2026-03-02 10:00" Einmaligen Versand definieren
    """
    if job_type not in ("message", "fetch"):
        typer.echo("Fehler: Typ muss 'message' oder 'fetch' sein.", err=True)
        raise typer.Exit(1)

    if job_type == "message" and not text:
        typer.echo("Fehler: Text ist für 'message'-Jobs erforderlich.", err=True)
        raise typer.Exit(1)

    jobs = _load_jobs()
    job_id = _gen_job_id()

    job = {
        "id": job_id,
        "type": job_type,
        "target": target,
        "text": text or "",
        "schedule": schedule,
        "enabled": True,
        "last_run": None,
        "created": datetime.now().isoformat(timespec="seconds"),
    }
    jobs.append(job)
    _save_jobs(jobs)

    typer.echo(f"[whatson] Job '{job_id}' angelegt: {job_type} → {target} [{schedule}]")


@app.command("job-list", rich_help_panel="Scheduler & Jobs")
def job_list_cmd():
    """Alle Jobs anzeigen."""
    jobs = _load_jobs()
    if not jobs:
        typer.echo("Keine Jobs vorhanden. 'whatson job-add ...' zum Anlegen.")
        return

    typer.echo(f"\n{'─' * 70}")
    typer.echo(f"  {'ID':<8} {'Typ':<10} {'Ziel':<15} {'Schedule':<20} {'Status':<8}")
    typer.echo(f"{'─' * 70}")
    for j in jobs:
        status = "✓ AN" if j.get("enabled", True) else "✗ AUS"
        typer.echo(f"  {j['id']:<8} {j['type']:<10} {j['target']:<15} {j['schedule']:<20} {status:<8}")
        if j.get("text"):
            typer.echo(f"           Text: {j['text'][:50]}")
    typer.echo(f"{'─' * 70}\n")


@app.command("job-delete", rich_help_panel="Scheduler & Jobs")
def job_delete_cmd(
    job_id: str = typer.Argument(..., help="Job-ID zum Löschen."),
):
    """Job löschen."""
    jobs = _load_jobs()
    new_jobs = [j for j in jobs if j["id"] != job_id]
    if len(new_jobs) == len(jobs):
        typer.echo(f"Fehler: Job '{job_id}' nicht gefunden.", err=True)
        raise typer.Exit(1)
    _save_jobs(new_jobs)
    typer.echo(f"[whatson] Job '{job_id}' gelöscht.")


@app.command("job-enable", rich_help_panel="Scheduler & Jobs")
def job_enable_cmd(
    job_id: str = typer.Argument(..., help="Job-ID zum Aktivieren."),
):
    """Job aktivieren."""
    jobs = _load_jobs()
    for j in jobs:
        if j["id"] == job_id:
            j["enabled"] = True
            _save_jobs(jobs)
            typer.echo(f"[whatson] Job '{job_id}' aktiviert.")
            return
    typer.echo(f"Fehler: Job '{job_id}' nicht gefunden.", err=True)
    raise typer.Exit(1)


@app.command("job-disable", rich_help_panel="Scheduler & Jobs")
def job_disable_cmd(
    job_id: str = typer.Argument(..., help="Job-ID zum Deaktivieren."),
):
    """Job deaktivieren."""
    jobs = _load_jobs()
    for j in jobs:
        if j["id"] == job_id:
            j["enabled"] = False
            _save_jobs(jobs)
            typer.echo(f"[whatson] Job '{job_id}' deaktiviert.")
            return
    typer.echo(f"Fehler: Job '{job_id}' nicht gefunden.", err=True)
    raise typer.Exit(1)


# ──────────────────────────────────────────────────────────────────
# SCHEDULER – Daemon steuern
# ──────────────────────────────────────────────────────────────────

def _scheduler_pid() -> Optional[int]:
    """Return the PID of the running scheduler, or None."""
    if not SCHEDULER_PID_PATH.exists():
        return None
    try:
        pid = int(SCHEDULER_PID_PATH.read_text().strip())
        os.kill(pid, 0)  # Check if process exists
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        SCHEDULER_PID_PATH.unlink(missing_ok=True)
        return None


@app.command("scheduler", rich_help_panel="Scheduler & Jobs")
def scheduler_cmd(
    action: str = typer.Argument(..., help="start, stop, restart, status"),
):
    """
    Scheduler-Daemon steuern.

    Beispiele:
        whatson scheduler start      Daemon im Hintergrund starten, um Jobs auszuführen
        whatson scheduler stop       Den Daemon und das Ausführen der Jobs komplett stoppen
        whatson scheduler restart    Neustart (erforderlich, falls neue Jobs angelegt wurden)
        whatson scheduler status     Aktuelle PID, Status und alle anstehenden Jobs anzeigen
    """
    if action == "status":
        pid = _scheduler_pid()
        if pid:
            typer.echo(f"[whatson] Scheduler läuft (PID {pid})")
        else:
            typer.echo("[whatson] Scheduler läuft NICHT.")
        jobs = _load_jobs()
        active = [j for j in jobs if j.get("enabled", True)]
        typer.echo(f"  Jobs: {len(jobs)} gesamt, {len(active)} aktiv")

    elif action == "start":
        pid = _scheduler_pid()
        if pid:
            typer.echo(f"[whatson] Scheduler läuft bereits (PID {pid}).")
            return
        if not SCHEDULER_SCRIPT.exists():
            typer.echo(f"Fehler: {SCHEDULER_SCRIPT} nicht gefunden.", err=True)
            raise typer.Exit(1)
        # Start als Hintergrund-Prozess
        import signal
        proc = subprocess.Popen(
            [sys.executable, str(SCHEDULER_SCRIPT)],
            stdout=open(WHATSON_HOME / "scheduler.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        SCHEDULER_PID_PATH.write_text(str(proc.pid))
        typer.echo(f"[whatson] Scheduler gestartet (PID {proc.pid}). Log: {WHATSON_HOME / 'scheduler.log'}")

    elif action == "stop":
        pid = _scheduler_pid()
        if not pid:
            typer.echo("[whatson] Scheduler läuft nicht.")
            return
        try:
            os.kill(pid, 15)  # SIGTERM
            typer.echo(f"[whatson] Scheduler gestoppt (PID {pid}).")
        except ProcessLookupError:
            typer.echo("[whatson] Scheduler war bereits beendet.")
        SCHEDULER_PID_PATH.unlink(missing_ok=True)

    elif action == "restart":
        # Stop then start
        pid = _scheduler_pid()
        if pid:
            try:
                os.kill(pid, 15)
            except ProcessLookupError:
                pass
            SCHEDULER_PID_PATH.unlink(missing_ok=True)
            time.sleep(1)
        # Start
        if not SCHEDULER_SCRIPT.exists():
            typer.echo(f"Fehler: {SCHEDULER_SCRIPT} nicht gefunden.", err=True)
            raise typer.Exit(1)
        proc = subprocess.Popen(
            [sys.executable, str(SCHEDULER_SCRIPT)],
            stdout=open(WHATSON_HOME / "scheduler.log", "a"),
            stderr=subprocess.STDOUT,
            start_new_session=True,
        )
        SCHEDULER_PID_PATH.write_text(str(proc.pid))
        typer.echo(f"[whatson] Scheduler neu gestartet (PID {proc.pid}).")

    else:
        typer.echo("Fehler: Aktion muss 'start', 'stop', 'restart' oder 'status' sein.", err=True)
        raise typer.Exit(1)


# ──────────────────────────────────────────────────────────────────
# ──────────────────────────────────────────────────────────────────
# SCAN – Konversation überwachen, Wachtler-Hook triggern
# ──────────────────────────────────────────────────────────────────

@app.command("scan", rich_help_panel="Nachrichten")
def scan_cmd(
    name: str = typer.Argument(..., help="ID oder Name der Konversation."),
    wachtler_hook: Optional[str] = typer.Option(None, "-w", "--wachtler", help="Wachtler-Hook ID (z.B. 3b) — wird getriggert wenn neue Nachrichten ankommen."),
    interval: int = typer.Option(60, "-i", "--interval", help="Prüfintervall in Sekunden (default: 60)."),
    json_out: bool = typer.Option(False, "--json", help="Ergebnis als JSON ausgeben."),
):
    """
    Überwacht eine Konversation auf neue Nachrichten.
    Stoppt und triggert einen Wachtler-Hook sobald neue Nachrichten ankommen.

    Beispiele:
        whatson scan 2 -w 3b           Alle 60s prüfen, bei neuen Hook 3b triggern
        whatson scan 2 -w 3b -i 30     Alle 30s prüfen
        whatson scan "KI Gruppe" -w 3b
    """
    resolved = _resolve_id(name)
    typer.echo(f"[whatson scan] Überwache '{resolved}' alle {interval}s …", err=True)
    if wachtler_hook:
        typer.echo(f"[whatson scan] Wachtler-Hook: {wachtler_hook}", err=True)

    engine = WhatsAppEngine()
    try:
        engine.start()
        while True:
            try:
                engine.ensure_authenticated()
                local_msgs = load_messages(resolved)
                stop_msg = local_msgs[-1] if local_msgs else None

                remote_msgs = engine.get_chat_history(resolved, limit=20, stop_at_msg=stop_msg)
                new_msgs = _find_new_messages(local_msgs, remote_msgs)

                if new_msgs:
                    count = append_messages(resolved, new_msgs)
                    last = new_msgs[-1]
                    typer.echo(f"[whatson scan] ✓ {resolved}: +{count} neue Nachrichten", err=True)

                    result = {
                        "name": resolved, "new_messages": count, "trigger_new_message": 1,
                        "last_message": {"time": last.get("time", ""), "sender": last.get("sender", ""), "text": last.get("text", "")},
                    }
                    if json_out:
                        _json_out(result)

                    if wachtler_hook:
                        typer.echo(f"[whatson scan] Triggere Wachtler-Hook {wachtler_hook} …", err=True)
                        subprocess.run(["wachtler", "hooks", "trigger", wachtler_hook], check=False)

                    break  # Stoppen nach erstem Fund

                else:
                    typer.echo(f"[whatson scan] Keine neuen Nachrichten. Warte {interval}s …", err=True)

            except Exception as exc:
                typer.echo(f"[whatson scan] FEHLER: {str(exc).split(chr(10))[0][:80]}", err=True)

            # Zurück zur Startseite damit die Session nicht einschläft
            try:
                engine.page.goto(WHATSAPP_URL, wait_until="domcontentloaded", timeout=30_000)
                engine.page.wait_for_selector(SEL_SIDE_PANEL, timeout=15_000)
            except Exception:
                pass

            time.sleep(interval)
    finally:
        engine.stop()


# ──────────────────────────────────────────────────────────────────
# Weitere Befehle (poll, status)
# ──────────────────────────────────────────────────────────────────

@app.command("poll", rich_help_panel="Sonstiges")
def poll_cmd(
    name: str = typer.Argument(..., help="Conversation name to poll."),
):
    """
    Poll a conversation for new messages and run a shell command on each.
    The shell command is defined in config.yaml under 'poll_command'.
    Variables {conversation} and {message} will be replaced.
    """
    cfg = get_config()
    interval = cfg.get("poll_interval", 60)
    poll_command = cfg.get("poll_command", 'echo "New msg: {message}"')

    engine = WhatsAppEngine()
    engine.start()
    last_text: Optional[str] = None

    try:
        resolved = _resolve_name(name, engine)
        typer.echo(
            f"[whatson] Polling '{resolved}' every {interval}s. Press Ctrl+C to stop.",
            err=True,
        )

        while True:
            engine.ensure_authenticated()
            last_msg = engine.get_last_message(resolved)

            if last_msg and last_msg["direction"] == "in":
                text = last_msg["text"]
                if text != last_text:
                    if last_text is not None:
                        typer.echo(f"[whatson] New message: {text}", err=True)
                        cmd = poll_command.replace("{conversation}", resolved).replace(
                            "{message}", text
                        )
                        subprocess.run(cmd, shell=True)
                    last_text = text

            if engine._page:
                engine._page.goto(WHATSAPP_URL)

            time.sleep(interval)
    except KeyboardInterrupt:
        typer.echo("\n[whatson] Polling stopped.", err=True)
    finally:
        engine.stop()


@app.command("auth", rich_help_panel="Sonstiges")
def auth_cmd(
    qr_path: str = typer.Option("/tmp/whatson-qr.png", "--qr-path", help="Pfad für den QR-Code Screenshot."),
):
    """
    Authentifiziert WhatsApp Web per QR-Code (SSH-kompatibel).

    Bleibt im Headless-Modus und speichert den QR-Code als PNG-Screenshot.
    Falls 'chafa' installiert ist, wird der QR-Code direkt im Terminal angezeigt.

    Beispiel:
        whatson auth
        whatson auth --qr-path ~/qr.png
        scp server:/tmp/whatson-qr.png .  # dann lokal öffnen
    """
    engine = WhatsAppEngine()
    try:
        engine.start()
        engine.open_whatsapp()
        try:
            engine.page.wait_for_selector(SEL_SIDE_PANEL, timeout=8_000)
            typer.echo("[whatson] Bereits authentifiziert — kein QR-Scan nötig.", err=True)
            return
        except PlaywrightTimeout:
            pass
        engine.auth_qr_headless(qr_path=Path(qr_path))
    finally:
        engine.stop()


@app.command("status", rich_help_panel="Sonstiges")
def status_cmd():
    """Show authentication and session status."""
    engine = WhatsAppEngine()
    try:
        engine.start()
        status = engine.check_status()
        status["user_data_dir"] = engine.cfg["user_data_dir"]
        _json_out(status)
    finally:
        engine.stop()


def app_entry():
    """Entry point for the setuptools console script."""
    try:
        app()
    except NotAuthenticatedError as e:
        typer.echo(f"[whatsON] {e}\nTipp: 'wo auth' ausführen.", err=True)
        raise SystemExit(1)


if __name__ == "__main__":
    app()
