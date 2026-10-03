#!/usr/bin/env python3
"""wa-link: a bidirectional bridge between WhatsApp groups and a local Claude Code.

The bridge logs in to WhatsApp as a *linked device* (like WhatsApp Web) using
neonize (a Python binding of the Go library whatsmeow). Messages arriving in
allowed groups are forwarded to the local `claude` CLI in print mode; Claude's
answer is posted back to the group as a reply to the triggering message.
Each group keeps its own persistent Claude Code session (`--resume`).

Run `./wa-link --help` for options and see README.md for setup.
"""
from __future__ import annotations

import argparse
import faulthandler
import json
import logging
import os
import queue
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
from pathlib import Path

import segno

# neonize 0.5.x imports typing.Self, which only exists since Python 3.11.
import typing
import typing_extensions

if not hasattr(typing, "Self"):
    typing.Self = typing_extensions.Self

from neonize.client import NewClient  # noqa: E402
from neonize.events import (  # noqa: E402
    ConnectedEv,
    LoggedOutEv,
    MessageEv,
    PairStatusEv,
)
from neonize.utils.enum import ChatPresence, ChatPresenceMedia  # noqa: E402
from neonize.utils.jid import Jid2String, build_jid  # noqa: E402

HERE = Path(__file__).resolve().parent
STATE_DIR = HERE / "state"
OUTBOX = STATE_DIR / "outbox"  # files dropped here by `--send` are posted by the running bridge
BOT_MARK = "🤖"  # prefix of every message the bridge sends (also used for loop protection)
MAX_CHUNK = 3500  # WhatsApp allows ~65k chars, but long walls of text are unreadable

SYSTEM_PROMPT = """You are connected to a WhatsApp group through a bridge called wa-link.
Every user turn contains messages from the group; the sender's display name precedes each message.
Your final answer is posted verbatim to the group, so:
- answer concisely, like a helpful participant in a chat;
- use WhatsApp formatting only: *bold*, _italic_, ~strike~, ```monospace```, "- " lists. No Markdown headings, tables or links in [text](url) form;
- you cannot see images, voice notes or other attachments."""

log = logging.getLogger("wa-link")


# --------------------------------------------------------------------------- helpers
def jid_user(jid) -> str:
    return jid.User if jid is not None else ""


def extract_text(msg) -> tuple[str, object | None]:
    """Return (text, contextInfo) of a WhatsApp message protobuf."""
    if msg.conversation:
        return msg.conversation, None
    if msg.HasField("extendedTextMessage"):
        m = msg.extendedTextMessage
        return m.text, m.contextInfo if m.HasField("contextInfo") else None
    for kind in ("imageMessage", "videoMessage", "documentMessage"):
        if msg.HasField(kind):
            m = getattr(msg, kind)
            label = kind.replace("Message", "")
            text = f"[{label} attachment, not visible to you] {m.caption}".strip()
            return text, m.contextInfo if m.HasField("contextInfo") else None
    for kind in ("audioMessage", "stickerMessage", "locationMessage", "contactMessage"):
        if msg.HasField(kind):
            m = getattr(msg, kind)
            ctx = m.contextInfo if m.HasField("contextInfo") else None
            return f"[{kind.replace('Message', '')}, not visible to you]", ctx
    return "", None


def split_chunks(text: str, limit: int = MAX_CHUNK) -> list[str]:
    chunks = []
    while len(text) > limit:
        cut = text.rfind("\n\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind("\n", 0, limit)
        if cut < limit // 2:
            cut = text.rfind(" ", 0, limit)
        if cut <= 0:
            cut = limit
        chunks.append(text[:cut].rstrip())
        text = text[cut:].lstrip()
    if text:
        chunks.append(text)
    return chunks


QUOTE_FILLER = "⠀"  # blank Braille pattern: keeps the quote bar on otherwise empty lines


def quote_style(text: str) -> str:
    """Render text as a WhatsApp quote block ("> " on every line), with BOT_MARK on its own line."""
    lines = []
    for line in text.split("\n"):
        line = re.sub(r"^>\s?", "", line)  # quotes do not nest in WhatsApp
        lines.append("> " + (line if line.strip() else QUOTE_FILLER))
    return BOT_MARK + "\n" + "\n".join(lines)


class Sessions:
    """Persistent mapping chat JID -> Claude Code session id."""

    def __init__(self, path: Path):
        self.path = path
        self.lock = threading.Lock()
        self.data = json.loads(path.read_text()) if path.exists() else {}

    def get(self, chat: str) -> str | None:
        with self.lock:
            return self.data.get(chat)

    def set(self, chat: str, session_id: str | None) -> None:
        with self.lock:
            if session_id:
                self.data[chat] = session_id
            else:
                self.data.pop(chat, None)
            tmp = self.path.with_suffix(".tmp")
            tmp.write_text(json.dumps(self.data, indent=2))
            tmp.replace(self.path)


# --------------------------------------------------------------------------- bridge
class Bridge:
    def __init__(self, args: argparse.Namespace):
        self.args = args
        self.client = NewClient(str(STATE_DIR / "whatsapp.sqlite3"))
        self.sessions = Sessions(STATE_DIR / "sessions.json")
        self.trigger = re.compile(args.trigger, re.IGNORECASE) if args.trigger else None
        self.allowed_groups = set(args.group)
        self.allowed_senders = {s.lstrip("+") for s in args.allow_sender}
        self.me_users: set[str] = set()
        self.group_names: dict[str, str] = {}
        self.sent_ids: set[str] = set()
        self.context: dict[str, list[str]] = {}  # untriggered messages per chat
        self.queues: dict[str, queue.Queue] = {}
        self.lock = threading.Lock()
        self.started = time.time()
        self.outbox_started = False
        self.claude_bin = shutil.which(args.claude_bin) or args.claude_bin

        c = self.client
        c.event(ConnectedEv)(self.on_connected)
        c.event(PairStatusEv)(self.on_pair)
        c.event(LoggedOutEv)(self.on_logged_out)
        c.event(MessageEv)(self.on_message)
        c.event.qr(self.on_qr)
        if args.pair_phone:
            c.event.paircode(self.on_paircode)

    # ---- connection events
    def on_qr(self, _c, data: bytes):
        qr = segno.make_qr(data)
        png = STATE_DIR / "pairing-qr.png"
        qr.save(str(png), scale=8, border=4)
        print("\nScan this QR code with the phone that owns the bridge's WhatsApp account:\n"
              "WhatsApp > Settings > Linked devices > Link a device.\n", flush=True)
        qr.terminal(compact=True)
        print(f"\n(Also saved as {png}; it refreshes every ~20 s.)\n", flush=True)

    def on_paircode(self, _c, code: str, connected: bool):
        if not connected:
            print(f"\n*** Pairing code: {code}\n"
                  "On the phone: WhatsApp > Linked devices > Link a device > "
                  "'Link with phone number instead', and enter the code.\n", flush=True)

    def on_pair(self, _c, ev):
        log.info("Paired as %s", Jid2String(ev.ID))
        (STATE_DIR / "pairing-qr.png").unlink(missing_ok=True)

    def on_logged_out(self, _c, _ev):
        log.error("Logged out by WhatsApp. Delete state/whatsapp.sqlite3 and pair again.")
        os._exit(2)

    def on_connected(self, _c, _ev):
        # Calling back into the client from inside an event callback can block, so use a thread.
        log.debug("Connected event received")
        threading.Thread(target=self.after_connect, daemon=True).start()

    def after_connect(self):
        try:
            me = self.client.get_me()
            self.me_users = {u for u in (jid_user(me.JID), jid_user(me.LID)) if u}
            log.info("Connected to WhatsApp as +%s (%s)", jid_user(me.JID), me.PushName)
        except Exception as e:  # noqa: BLE001
            log.warning("Cannot get own identity: %s", e)
        if not self.args.list_groups and not self.outbox_started:
            self.outbox_started = True
            threading.Thread(target=self.outbox_loop, daemon=True).start()
        try:
            groups = self.client.get_joined_groups()
        except Exception as e:  # noqa: BLE001
            log.warning("Cannot fetch group list: %s", e)
            return
        for g in groups:
            self.group_names[Jid2String(g.JID)] = g.GroupName.Name
        if self.args.list_groups:
            print("\nGroups this account is a member of:\n")
            for jid, name in sorted(self.group_names.items(), key=lambda x: x[1].lower()):
                mark = "  [allowed]" if jid in self.allowed_groups else ""
                print(f"  {jid}   {name}{mark}")
            print(f"\nStart the bridge with:  ./wa-link --group <JID> [--group <JID> ...]\n", flush=True)
            self.client.stop()
            os._exit(0)
        for jid in self.allowed_groups:
            log.info("Serving group %s (%s)", jid, self.group_names.get(jid, "not a member yet?"))
        if not self.allowed_groups:
            log.warning("No --group given: nothing will be answered. Use --list-groups to find JIDs.")

    # ---- incoming messages
    def on_message(self, _c, ev):
        info = ev.Info
        src = info.MessageSource
        chat = Jid2String(src.Chat)
        if not src.IsGroup or info.ID in self.sent_ids:
            return
        if chat not in self.allowed_groups:
            log.debug("Ignoring message in non-allowed group %s (%s)", chat, self.group_names.get(chat, "?"))
            return
        text, ctx = extract_text(ev.Message)
        if not text or text.startswith(BOT_MARK):
            return
        ts = info.Timestamp / 1000 if info.Timestamp > 1e12 else info.Timestamp
        if ts and ts < time.time() - self.args.max_age:
            log.debug("Skipping old message %s", info.ID)
            return
        sender = info.Pushname or jid_user(src.Sender)
        # In groups WhatsApp often identifies senders by an anonymous LID rather than the
        # phone number; SenderAlt carries the other form when known.
        sender_ids = {u for u in (jid_user(src.Sender), jid_user(src.SenderAlt)) if u}
        if src.IsFromMe or sender_ids & self.me_users:
            sender_ids |= self.me_users
        if self.allowed_senders and not sender_ids & self.allowed_senders:
            log.info("Sender %s (%s) not in --allow-sender, ignoring", sender,
                     ", ".join(sorted(sender_ids)))
            return

        addressed, clean = self.is_addressed(text, ctx)
        if not addressed:
            log.info("[%s] %s: %s  (not addressed to Claude, kept as context)",
                     self.group_names.get(chat, chat), sender, text[:200])
            with self.lock:
                buf = self.context.setdefault(chat, [])
                buf.append(f"{sender}: {text}")
                del buf[: -self.args.context_messages]
            return
        log.info("[%s] %s: %s", self.group_names.get(chat, chat), sender, clean[:200])
        self.queue_for(chat).put((ev, sender, clean))

    def is_addressed(self, text: str, ctx) -> tuple[bool, str]:
        """Decide whether the message is meant for Claude; return cleaned text."""
        addressed = self.args.all
        clean = text
        if ctx is not None:
            mentioned = {jid.split("@")[0] for jid in ctx.mentionedJID}
            if mentioned & self.me_users:
                addressed = True
                for u in mentioned & self.me_users:
                    clean = clean.replace(f"@{u}", "")
            if ctx.participant and ctx.participant.split("@")[0].split(":")[0] in self.me_users \
                    and ctx.quotedMessage is not None:
                quoted, _ = extract_text(ctx.quotedMessage)
                if quoted.startswith(BOT_MARK):  # a reply to one of Claude's answers
                    addressed = True
        if self.trigger:
            m = self.trigger.match(clean.strip())
            if m:
                addressed = True
                clean = clean.strip()[m.end():]
        return addressed, clean.strip(" \t\n,:")

    def queue_for(self, chat: str) -> queue.Queue:
        with self.lock:
            q = self.queues.get(chat)
            if q is None:
                q = self.queues[chat] = queue.Queue()
                threading.Thread(target=self.worker, args=(chat, q), daemon=True).start()
            return q

    # ---- talking to Claude Code
    def worker(self, chat: str, q: queue.Queue):
        while True:
            ev, sender, text = q.get()
            try:
                self.handle(chat, ev, sender, text)
            except Exception:  # noqa: BLE001
                log.exception("Failed to handle message")
                self.reply(ev, self.bot_text("Internal error in wa-link, see its log."))

    def handle(self, chat: str, ev, sender: str, text: str):
        cmd = text.strip().lower()
        if cmd in ("/reset", "/new"):
            self.sessions.set(chat, None)
            self.reply(ev, self.bot_text("Started a fresh Claude Code session."))
            return
        if cmd == "/status":
            sid = self.sessions.get(chat) or "none yet"
            self.reply(ev, self.bot_text(f"wa-link is running. Session: {sid}\nWorkdir: {self.args.workdir}"))
            return
        if not text:
            text = "(empty message)"

        with self.lock:
            backlog = self.context.pop(chat, [])
        group = self.group_names.get(chat, chat)
        prompt = f'[WhatsApp group "{group}"]\n'
        if backlog:
            prompt += "Other messages in the group since your last reply (for context):\n"
            prompt += "\n".join(backlog) + "\n\n"
        prompt += f"Message addressed to you:\n{sender}: {text}"

        stop_typing = threading.Event()
        threading.Thread(target=self.typing, args=(ev.Info.MessageSource.Chat, stop_typing),
                         daemon=True).start()
        try:
            answer = self.ask_claude(chat, prompt)
        finally:
            stop_typing.set()
        for chunk in split_chunks(answer.strip() or "(empty answer)"):
            self.reply(ev, self.bot_text(chunk))

    def ask_claude(self, chat: str, prompt: str) -> str:
        a = self.args
        base = [self.claude_bin, "-p", "--output-format", "json",
                "--append-system-prompt", SYSTEM_PROMPT]
        if a.model:
            base += ["--model", a.model]
        if a.permission_mode:
            base += ["--permission-mode", a.permission_mode]
        if a.allowed_tools:
            base += ["--allowedTools", *a.allowed_tools]
        base += a.claude_arg

        sid = self.sessions.get(chat)
        for attempt in (1, 2):
            cmd = base + (["--resume", sid] if sid else [])
            log.debug("Running %s", cmd)
            try:
                p = subprocess.run(cmd, input=prompt, capture_output=True, text=True,
                                   cwd=a.workdir, timeout=a.timeout)
            except subprocess.TimeoutExpired:
                return f"Sorry, Claude Code did not finish within {a.timeout} s."
            try:
                out = json.loads(p.stdout)
            except json.JSONDecodeError:
                out = None
            if out is None or (p.returncode != 0 and not out.get("result")):
                err = (p.stderr or p.stdout).strip()[-500:]
                if sid and attempt == 1 and ("session" in err.lower() or "conversation" in err.lower()):
                    log.warning("Session %s could not be resumed, starting a new one", sid)
                    sid = None
                    continue
                log.error("claude failed (%s): %s", p.returncode, err)
                return f"Claude Code failed: {err or 'exit code %d' % p.returncode}"
            if out.get("session_id"):
                self.sessions.set(chat, out["session_id"])
            return str(out.get("result", ""))
        return "Claude Code failed."

    # ---- talking to WhatsApp
    def typing(self, chat_jid, stop: threading.Event):
        while not stop.is_set():
            try:
                self.client.send_chat_presence(chat_jid, ChatPresence.CHAT_PRESENCE_COMPOSING,
                                               ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
            except Exception:  # noqa: BLE001
                pass
            stop.wait(10)
        try:
            self.client.send_chat_presence(chat_jid, ChatPresence.CHAT_PRESENCE_PAUSED,
                                           ChatPresenceMedia.CHAT_PRESENCE_MEDIA_TEXT)
        except Exception:  # noqa: BLE001
            pass

    def bot_text(self, text: str) -> str:
        return f"{BOT_MARK} {text}" if self.args.no_quote_style else quote_style(text)

    def reply(self, ev, text: str):
        resp = self.client.reply_message(text, ev)
        self.sent_ids.add(resp.ID)

    def outbox_loop(self):
        """Post messages queued by `wa-link --send` (JSON files {"to": jid, "text": ...})."""
        while True:
            for f in sorted(OUTBOX.glob("*.json")):
                try:
                    item = json.loads(f.read_text())
                    to, text = item["to"], item["text"]
                    if to not in self.allowed_groups:
                        log.warning("Outbox: %s is not an allowed --group, dropping %s", to, f.name)
                    else:
                        resp = self.client.send_message(build_jid(*to.split("@")), self.bot_text(text))
                        self.sent_ids.add(resp.ID)
                        log.info("Outbox: sent to %s: %s", self.group_names.get(to, to), text[:200])
                except Exception as e:  # noqa: BLE001
                    log.error("Outbox: cannot send %s: %s", f.name, e)
                f.unlink(missing_ok=True)
            time.sleep(2)

    def run(self):
        if self.args.pair_phone:  # the code can be requested only once the socket is up
            threading.Thread(target=self._pair_phone, daemon=True).start()
        self.client.connect()

    def _pair_phone(self):
        time.sleep(3)
        if self.client.is_logged_in:
            return
        try:
            code = self.client.PairPhone(self.args.pair_phone.lstrip("+"), show_push_notification=True)
            self.on_paircode(self.client, code, False)
        except Exception as e:  # noqa: BLE001
            log.error("Phone pairing failed: %s", e)


# --------------------------------------------------------------------------- main
def main():
    env = os.environ.get
    ap = argparse.ArgumentParser(
        description="Bridge WhatsApp group messages to a local Claude Code and post answers back.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    ap.add_argument("--list-groups", action="store_true",
                    help="connect (pairing first if needed), print the groups of the account and exit")
    ap.add_argument("--send", nargs=2, metavar=("GROUP_JID", "TEXT"),
                    help="queue TEXT for the running bridge to post to GROUP_JID, then exit")
    ap.add_argument("--pair-phone", metavar="NUMBER",
                    help="pair with an 8-character code for this phone number instead of a QR code")
    ap.add_argument("--group", action="append", default=env("WA_GROUPS", "").split() or [],
                    metavar="JID", help="group JID (…@g.us) to serve; repeatable [env WA_GROUPS]")
    ap.add_argument("--trigger", default=env("WA_TRIGGER", r"^@?claude\b"),
                    help="regex (case-insensitive, matched at start) that addresses Claude. "
                         "Mentioning the bot's number or replying to its answer also works. "
                         "Empty string disables. [env WA_TRIGGER]")
    ap.add_argument("--all", action="store_true",
                    help="answer every message in the allowed groups, not just addressed ones")
    ap.add_argument("--allow-sender", action="append", default=env("WA_ALLOW_SENDERS", "").split() or [],
                    metavar="PHONE", help="only accept requests from these numbers (international "
                    "format, digits only); repeatable [env WA_ALLOW_SENDERS]")
    ap.add_argument("--workdir", default=env("WA_WORKDIR", str(HERE / "workspace")),
                    help="working directory for Claude Code [env WA_WORKDIR]")
    ap.add_argument("--permission-mode", default=env("WA_PERMISSION_MODE"),
                    help="passed to claude --permission-mode (e.g. acceptEdits, bypassPermissions). "
                         "Default: tools needing approval are refused. [env WA_PERMISSION_MODE]")
    ap.add_argument("--allowed-tools", nargs="*", default=env("WA_ALLOWED_TOOLS", "").split() or [],
                    help='passed to claude --allowedTools, e.g. "WebSearch" "Bash(git log:*)" '
                         "[env WA_ALLOWED_TOOLS]")
    ap.add_argument("--no-quote-style", action="store_true",
                    help="send answers as plain text after the robot emoji instead of a quote block")
    ap.add_argument("--model", default=env("WA_MODEL"), help="claude --model [env WA_MODEL]")
    ap.add_argument("--claude-arg", action="append", default=[],
                    help="extra raw argument for claude; repeatable")
    ap.add_argument("--claude-bin", default=env("CLAUDE_BIN", "claude"), help="claude executable")
    ap.add_argument("--timeout", type=int, default=600, help="max seconds per Claude call")
    ap.add_argument("--context-messages", type=int, default=30,
                    help="how many unaddressed group messages to pass along as context")
    ap.add_argument("--max-age", type=int, default=900,
                    help="ignore messages older than this many seconds (e.g. after downtime)")
    ap.add_argument("-v", "--verbose", action="store_true")
    args = ap.parse_args()

    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(asctime)s %(levelname)s %(name)s: %(message)s", force=True)
    for noisy in ("whatsmeow", "Whatsmeow", "neonize", "Client"):
        logging.getLogger(noisy).setLevel(logging.INFO if args.verbose else logging.WARNING)

    if args.send:
        OUTBOX.mkdir(parents=True, exist_ok=True)
        tmp = OUTBOX / f".{time.time_ns()}.tmp"
        tmp.write_text(json.dumps({"to": args.send[0], "text": args.send[1]}))
        tmp.rename(tmp.with_name(tmp.name[1:-4] + ".json"))
        print("Queued; the running bridge posts it within a few seconds (see state/wa-link.log).")
        return
    # `kill -USR1 <pid>` dumps all Python thread stacks to the log (ptrace may be unavailable).
    faulthandler.register(signal.SIGUSR1, all_threads=True)
    STATE_DIR.mkdir(exist_ok=True)
    OUTBOX.mkdir(exist_ok=True)
    Path(args.workdir).mkdir(parents=True, exist_ok=True)
    if not args.list_groups and not shutil.which(args.claude_bin):
        sys.exit(f"Cannot find the Claude Code executable '{args.claude_bin}' (use --claude-bin).")
    Bridge(args).run()


if __name__ == "__main__":
    main()
