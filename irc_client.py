#!/usr/bin/env python3
"""
IRC Client for irc.saranepal.com:6697 (#Nepal)
- SSL/TLS encrypted connection
- Logs all activity to logs/  (plain text, human-readable)
- Saves chat messages to chats/  (JSON Lines / .jsonl, machine-readable)
"""

import socket
import ssl
import threading
import logging
import time
import os
import sys
import json
from datetime import datetime, timezone
from dotenv import load_dotenv



# ─── Configuration ────────────────────────────────────────────────────────────
load_dotenv()
SERVER   = os.getenv("IRC_SERVER", "irc.saranepal.com")
PORT     = int(os.getenv("IRC_PORT", 6697))
CHANNEL  = os.getenv("IRC_CHANNEL", "#Nepal")
NICK     = os.getenv("IRC_NICK", "PyClient_" + str(int(time.time()))[-4:])
REALNAME = os.getenv("IRC_REALNAME", "Python IRC Client")
IDENT    = os.getenv("IRC_IDENT", "pyclient")

CHATS_DIR = "chats"
LOGS_DIR  = "logs"
PING_INTERVAL = 120   # seconds between keep-alive PINGs
RECONNECT_DELAY = 30  # seconds to wait before reconnecting


# ─── Directory & logging setup ────────────────────────────────────────────────
os.makedirs(CHATS_DIR, exist_ok=True)
os.makedirs(LOGS_DIR,  exist_ok=True)

log_file = os.path.join(LOGS_DIR, f"irc_{datetime.now():%Y%m%d_%H%M%S}.log")

logging.basicConfig(
    level=logging.DEBUG,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.FileHandler(log_file, encoding="utf-8"),
        logging.StreamHandler(sys.stdout),
    ],
)
log = logging.getLogger("irc")


def chat_log_path() -> str:
    """Return today's chat log file path (JSON Lines format)."""
    return os.path.join(CHATS_DIR, f"{CHANNEL.lstrip('#')}_{datetime.now():%Y%m%d}.jsonl")


def save_chat(sender: str, message: str, msg_type: str = "message",
              channel: str = CHANNEL, extra: dict | None = None) -> None:
    """Append one JSON object (newline-delimited) to today's channel log.

    Schema
    ------
    {
        "ts":       "<ISO-8601 UTC timestamp>",
        "channel":  "#Nepal",
        "type":     "message" | "action" | "join" | "part" | "quit" |
                    "kick" | "nick" | "notice" | "mode",
        "nick":     "<sender nick>",
        "message":  "<text>",          # omitted when empty
        ...extra fields per type...
    }
    """
    record: dict = {
        "ts":      datetime.now(timezone.utc).isoformat(),
        "channel": channel,
        "type":    msg_type,
        "nick":    sender,
    }
    if message:
        record["message"] = message
    if extra:
        record.update(extra)

    with open(chat_log_path(), "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


# ─── IRC Client ───────────────────────────────────────────────────────────────
class IRCClient:
    def __init__(self):
        self.sock: ssl.SSLSocket | None = None
        self._stop = threading.Event()
        self._ping_thread: threading.Thread | None = None
        self._read_thread: threading.Thread | None = None

    # ── Low-level send ────────────────────────────────────────────────────────
    def send(self, raw: str) -> None:
        line = raw.rstrip("\r\n") + "\r\n"
        log.debug(">> %s", line.rstrip())
        self.sock.sendall(line.encode("utf-8", errors="replace"))

    # ── Connect & register ────────────────────────────────────────────────────
    def connect(self) -> bool:
        log.info("Connecting to %s:%d (SSL/TLS) …", SERVER, PORT)
        try:
            ctx = ssl.create_default_context()
            # Uncomment the two lines below if the server uses a self-signed cert:
            # ctx.check_hostname = False
            # ctx.verify_mode   = ssl.CERT_NONE

            raw_sock = socket.create_connection((SERVER, PORT), timeout=30)
            self.sock = ctx.wrap_socket(raw_sock, server_hostname=SERVER)
            log.info("TLS handshake OK — cipher: %s", self.sock.cipher())
        except Exception as exc:
            log.error("Connection failed: %s", exc)
            return False

        # Register
        self.send(f"NICK {NICK}")
        self.send(f"USER {IDENT} 0 * :{REALNAME}")
        return True

    # ── Keep-alive thread ─────────────────────────────────────────────────────
    def _ping_loop(self) -> None:
        while not self._stop.is_set():
            time.sleep(PING_INTERVAL)
            if not self._stop.is_set():
                try:
                    self.send(f"PING {SERVER}")
                except Exception as exc:
                    log.warning("PING failed: %s", exc)
                    break

    # ── Message parser ────────────────────────────────────────────────────────
    @staticmethod
    def parse(raw: str) -> tuple[str, str, str, list[str]]:
        """Parse a raw IRC line → (prefix, command, target, params)."""
        prefix = ""
        if raw.startswith(":"):
            prefix, _, raw = raw[1:].partition(" ")
        parts = raw.split(" ", 1)
        command = parts[0].upper()
        rest = parts[1] if len(parts) > 1 else ""

        params: list[str] = []
        if " :" in rest:
            head, _, trailing = rest.partition(" :")
            if head:
                params = head.split()
            params.append(trailing)
        else:
            params = rest.split() if rest else []

        target = params[0] if params else ""
        return prefix, command, target, params

    # ── Handle one line ───────────────────────────────────────────────────────
    def handle(self, raw: str) -> None:
        log.debug("<< %s", raw)
        prefix, command, target, params = self.parse(raw)
        nick = prefix.split("!")[0] if "!" in prefix else prefix

        if command == "PING":
            self.send(f"PONG :{params[0] if params else ''}")

        elif command in ("001", "002", "003", "004"):
            log.info("Server: %s", " ".join(params[1:]) if len(params) > 1 else raw)

        elif command == "376":   # End of MOTD
            log.info("Joining %s …", CHANNEL)
            self.send(f"JOIN {CHANNEL}")

        elif command == "433":   # Nick already in use
            new_nick = NICK + "_"
            log.warning("Nick %s taken — retrying as %s", NICK, new_nick)
            self.send(f"NICK {new_nick}")

        elif command == "JOIN":
            log.info("%s joined %s", nick, target)
            save_chat(nick, "", msg_type="join")
            if nick.lower() == NICK.lower():
                log.info("Successfully joined %s", CHANNEL)

        elif command == "PART":
            reason = params[-1] if len(params) > 1 else ""
            log.info("%s left %s", nick, target)
            save_chat(nick, reason, msg_type="part")

        elif command == "QUIT":
            reason = params[-1] if params else ""
            log.info("%s quit (%s)", nick, reason)
            save_chat(nick, reason, msg_type="quit")

        elif command == "NICK":
            new_nick = params[-1] if params else ""
            log.info("%s is now known as %s", nick, new_nick)
            save_chat(nick, "", msg_type="nick", extra={"new_nick": new_nick})

        elif command == "PRIVMSG":
            chan_or_user = target
            message = params[-1] if len(params) > 1 else ""
            log.info("[%s] <%s> %s", chan_or_user, nick, message)

            if chan_or_user.lower() == CHANNEL.lower():
                # Detect CTCP ACTION (/me)
                if message.startswith("\x01ACTION") and message.endswith("\x01"):
                    action_text = message[8:-1]
                    save_chat(nick, action_text, msg_type="action")
                else:
                    save_chat(nick, message, msg_type="message")

            # Respond to !ping in channel
            if message.strip() == "!ping":
                self.send(f"PRIVMSG {chan_or_user} :pong!")

        elif command == "NOTICE":
            message = params[-1] if len(params) > 1 else ""
            log.info("NOTICE from %s: %s", nick or SERVER, message)

        elif command == "MODE":
            log.info("MODE %s by %s: %s", target, nick, " ".join(params[1:]))

        elif command == "KICK":
            kicked = params[1] if len(params) > 1 else "?"
            reason = params[-1] if len(params) > 2 else ""
            log.info("%s was kicked from %s by %s (%s)", kicked, target, nick, reason)
            save_chat(nick, reason, msg_type="kick", extra={"kicked": kicked})
            if kicked.lower() == NICK.lower():
                log.warning("We were kicked! Rejoining in 5 s …")
                time.sleep(5)
                self.send(f"JOIN {CHANNEL}")

        elif command in ("353", "366"):  # NAMES list
            log.debug("NAMES: %s", " ".join(params))

        elif command == "ERROR":
            log.error("Server ERROR: %s", " ".join(params))

    # ── Read loop ─────────────────────────────────────────────────────────────
    def _read_loop(self) -> None:
        buf = ""
        while not self._stop.is_set():
            try:
                data = self.sock.recv(4096)
                if not data:
                    log.warning("Server closed the connection.")
                    break
                buf += data.decode("utf-8", errors="replace")
                while "\r\n" in buf:
                    line, buf = buf.split("\r\n", 1)
                    if line:
                        self.handle(line)
            except ssl.SSLError as exc:
                log.error("SSL error: %s", exc)
                break
            except OSError as exc:
                if not self._stop.is_set():
                    log.error("Socket error: %s", exc)
                break
        self._stop.set()

    # ── Start / stop ──────────────────────────────────────────────────────────
    def run(self) -> None:
        if not self.connect():
            return

        self._stop.clear()

        self._ping_thread = threading.Thread(target=self._ping_loop, daemon=True, name="ping")
        self._ping_thread.start()

        self._read_thread = threading.Thread(target=self._read_loop, daemon=True, name="reader")
        self._read_thread.start()

        log.info("Client running. Type a message and press Enter to send to %s.", CHANNEL)
        log.info("Commands: /quit  /part  /names  /me <action>  /msg <nick> <text>")

        try:
            while not self._stop.is_set():
                try:
                    user_input = input()
                except EOFError:
                    break

                if not user_input.strip():
                    continue

                if user_input.startswith("/quit"):
                    reason = user_input[5:].strip() or "Goodbye!"
                    self.send(f"QUIT :{reason}")
                    time.sleep(1)
                    self._stop.set()

                elif user_input.startswith("/part"):
                    self.send(f"PART {CHANNEL}")

                elif user_input.startswith("/names"):
                    self.send(f"NAMES {CHANNEL}")

                elif user_input.startswith("/me "):
                    action = user_input[4:]
                    self.send(f"PRIVMSG {CHANNEL} :\x01ACTION {action}\x01")
                    save_chat(NICK, action, msg_type="action")

                elif user_input.startswith("/msg "):
                    parts = user_input[5:].split(" ", 1)
                    if len(parts) == 2:
                        self.send(f"PRIVMSG {parts[0]} :{parts[1]}")
                    else:
                        print("Usage: /msg <nick> <message>")

                else:
                    # Plain message → send to channel
                    self.send(f"PRIVMSG {CHANNEL} :{user_input}")
                    save_chat(NICK, user_input, msg_type="message")

        except KeyboardInterrupt:
            log.info("Interrupted — quitting.")
            try:
                self.send("QUIT :Client disconnected")
            except Exception:
                pass
        finally:
            self._stop.set()
            try:
                self.sock.close()
            except Exception:
                pass
            log.info("Disconnected.")


# ─── Entry point ──────────────────────────────────────────────────────────────
def main() -> None:
    while True:
        client = IRCClient()
        client.run()
        log.info("Reconnecting in %d seconds … (Ctrl+C to exit)", RECONNECT_DELAY)
        try:
            time.sleep(RECONNECT_DELAY)
        except KeyboardInterrupt:
            log.info("Exiting.")
            break


if __name__ == "__main__":
    main()
