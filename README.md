# wa-link: WhatsApp group ⇄ local Claude Code

`wa-link` signs in to WhatsApp as a **linked device**, the same way WhatsApp Web does. It watches the
groups you allow. When someone in one of them talks to Claude, the message goes to the local
`claude` CLI and Claude's answer is posted back to the group as a reply.

* WhatsApp side: [neonize](https://github.com/krypton-byte/neonize), a Python binding of the Go library
  *whatsmeow*. It needs no browser and no Node. Its native library lives inside `venv/`.
* Claude side: `claude -p --output-format json`, with one persistent session per group
  (`--resume`). Claude remembers earlier messages from the same group.
* All dependencies are in `./venv` and listed in `requirements.txt`.

## Install (already done here)

```sh
python3 -m venv venv
./venv/bin/pip install -r requirements.txt
```

## 1. Choose the WhatsApp account the bridge uses

The bridge is a linked device of a normal WhatsApp account, so it acts as that account.

* **Recommended: a separate number for the bot** (a spare SIM, or a WhatsApp Business account on a
  second number). Claude then shows up in the group as its own participant.
* **Your own account.** This also works. Claude's answers will appear as messages from you, starting with 🤖.
  Start your request with `claude ...`. Your phone stays fully usable.

## 2. Pair (one time)

```sh
./wa-link --list-groups
```

A QR code appears in the terminal. It is also saved to `state/pairing-qr.png`. On the phone of the
bot account, open **WhatsApp → Settings → Linked devices → Link a device** and scan it.
If you can't scan a QR code, use `./wa-link --list-groups --pair-phone 420123456789` and enter the 8-character code
under **Link a device → Link with phone number instead**.

The login is saved in `state/whatsapp.sqlite3`. Keep this file private: anyone who has it can
use the WhatsApp account.

## 3. Invite the connector to the group

The connector is the bot account's phone number, so you invite it like any other person:

1. Save the bot's number as a contact on your phone. Name it "Claude", for example.
2. In the group, open **Group info → Add participants** and pick that contact. You can also use
   **Invite via link** and open the link on the bot's phone.
   (If the bridge uses your own account, skip this step. You are already in the group.)
3. Find the group's JID:
   ```sh
   ./wa-link --list-groups
   #   120363401234567890@g.us   My Team
   ```

## 4. Run

```sh
./wa-link --group 120363401234567890@g.us
```

To keep it running after logout, use `nohup ./wa-link --group … > wa-link.log 2>&1 &`, `tmux`, or a
systemd user unit.

Or put your settings into `wa-link.env` (copy `wa-link.env.example`; not versioned) and use
`./restart.sh [DELAY_S]`: it stops a running bridge and starts a new one in the background, logging to
`state/wa-link.log`. Claude can restart its own bridge with `setsid nohup ./restart.sh 45 >/dev/null 2>&1 &`
(the delay lets the current answer be posted first).

### Talking to Claude in the group

A message reaches Claude when it:

* starts with `claude` or `@claude` (e.g. `Claude, summarise the discussion above`). You can
  change the pattern with `--trigger REGEX`.
* @mentions the bot's account, or
* replies (swipe → reply) to one of Claude's 🤖 answers.

Use `--all` to make Claude answer every message.
Other group messages are not answered. The last 30 of them (`--context-messages`) are sent along
with the next request, so Claude knows what the group was discussing.

Commands, typed after the trigger: `claude /reset` starts a new Claude session for the group,
and `claude /status` shows the session id and working directory.

To post into an allowed group from this machine (a script, cron job, or another Claude Code
session), use `./wa-link --send <GROUP_JID> "text"`, or `./wa-link --send-file <GROUP_JID> FILE [CAPTION]` for an image (sent as a photo), audio such as mp3 (playable audio message; `--voice` = voice note), video, or any other file (sent as a document; `--as-document` forces this). The message is queued in `state/outbox/` and the
running bridge posts it within a few seconds.

While Claude is working, the group sees "typing…". Long answers are split into several messages.
Answers are sent as a quote block (🤖 on the first line, then `> ` before every line, empty lines
filled with an invisible U+2800 so the quote bar continues); `--no-quote-style` gives the plain
`🤖 text` view. This applies to all messages, including `/status` and `/reset` replies.

## Security: read this

Everyone in an allowed group can make Claude Code work **on this machine**, using your
Claude account.

* By default `claude -p` only runs tools that need no approval, such as reading files and searching.
  Shell commands and file edits are refused. Claude works in `./workspace`; change it with `--workdir`.
* To allow more, pass options on purpose, e.g. `--allowed-tools WebSearch WebFetch "Bash(git log:*)"`
  or `--permission-mode acceptEdits`. Use `--permission-mode bypassPermissions` only in a
  group where you trust every member.
* `--allow-sender 420123456789` (repeatable) limits which phone numbers Claude accepts requests from.
* Groups not passed with `--group` are ignored completely.
* `--group JID=WORKDIR` gives a group its own working directory (own files, Claude sessions and project memory),
  e.g. `--group 1203…@g.us=$HOME/personal-sessions`; all groups are served by the same bridge and number.

WhatsApp does not officially support unofficial clients like this one. Using one can, rarely, get the account banned.
Do not use it for bulk messaging. This is one more reason to use a separate number.

## All options

```
./wa-link --help
```

Most options can also be set through environment variables: `WA_GROUPS` (space-separated),
`WA_TRIGGER`, `WA_ALLOW_SENDERS`, `WA_WORKDIR`, `WA_PERMISSION_MODE`, `WA_ALLOWED_TOOLS`, `WA_MODEL`,
`CLAUDE_BIN`.

## Files

| Path | Purpose |
|---|---|
| `wa_link.py` | the bridge |
| `wa-link` | launcher that uses `./venv` |
| `restart.sh` | background (re)start with the settings from `wa-link.env` |
| `wa-link.env.example` | template for the local, unversioned `wa-link.env` |
| `state/whatsapp.sqlite3` | WhatsApp login and encryption keys (**secret**) |
| `state/sessions.json` | group JID → Claude Code session id |
| `workspace/` | Claude Code's working directory |

## Troubleshooting

* **Logged out / "LoggedOut" in the log:** delete `state/whatsapp.sqlite3` and pair again.
* **Nothing happens on a message:** run with `-v`. Check that the group JID is in `--group` and that
  the message matches the trigger. Messages older than 15 minutes (`--max-age`) are skipped. This
  avoids a flood of answers after downtime.
* **Python 3.10 note:** neonize 0.5 expects Python 3.11. `wa_link.py` fills in the one missing
  piece, `typing.Self`, from `typing_extensions`, so the library itself is not modified.
