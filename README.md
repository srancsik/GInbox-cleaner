# GInbox Cleaner

A desktop app that lists the senders in your Gmail Inbox ranked by
message count, lets you mark each one **Keep / Delete / Undecided**, and
then removes the unwanted ones in bulk — moved to Trash or permanently
deleted, your choice.

English UI | Hungarian UI — switchable with the flag buttons in the app.

---

## ⚠️ Read this first — warnings & assumptions

- **This app acts on your real mailbox.** Deletion is not reversible
  unless you use *Move to Trash* mode. Prefer Trash — mail there stays
  recoverable for ~30 days via Gmail. **Permanently delete cannot be
  undone.**
- The app only ever touches your **Inbox**: nothing in Sent, Spam,
  Drafts, or other folders is searched or modified.
- Only senders you explicitly mark **Delete** are affected, and only by
  *exact* address match — `ann@x.com` will never delete `joann@x.com`.
- You need a **Gmail app password** (not your normal password) — see
  setup below. This requires 2-Step Verification on your account.
- Binaries are self-contained (~56–72 MB — Python and Qt are bundled).
- **Windows builds are currently self-signed** — SmartScreen may warn.
  Click *More info → Run anyway*, or verify the SHA256 checksum first.
- Not affiliated with Google. Gmail is a trademark of Google LLC.

## Downloads

| You want | File | Where |
|---|---|---|
| Linux Mint / Ubuntu / Debian | `ginbox-cleaner_*_amd64.deb` | [Releases](https://github.com/srancsik/GInbox-cleaner/releases) |
| Windows (no install) | `ginbox-cleaner_*_portable_windows.zip` | [Releases](https://github.com/srancsik/GInbox-cleaner/releases) |
| Linux (no install) | `ginbox-cleaner_*_portable_linux.zip` | [Releases](https://github.com/srancsik/GInbox-cleaner/releases) |
| Run the Python source | `dist-source/` folder in this repo | repo tree |

Always verify `SHA256SUMS` before running:
`sha256sum -c SHA256SUMS` (Linux/macOS) or
`certutil -hashfile <file> SHA256` (Windows).

## Gmail setup (required once)

1. Enable **2-Step Verification** on your Google account.
2. Create an app password at <https://myaccount.google.com/apppasswords>. The format is a 16-character string, for example `abcd efgh ijkl mnop`.
3. Make sure **IMAP is enabled**: Gmail → Settings → See all settings →
   Forwarding and POP/IMAP.

## Run it

**Linux (.deb):**

```bash
sudo dpkg -i ginbox-cleaner_*_amd64.deb
```

Then launch "GInbox Cleaner" from your menu, or run `ginbox-cleaner`.

**Portable (Windows/Linux):** unzip anywhere, run `ginbox-cleaner.exe`
or `./ginbox-cleaner`. The included `portable.flag` makes the app keep
all its data inside its own folder — delete it if you'd rather use the
per-user location.

**From source:**

```bash
cd dist-source
pip install -r requirements.txt
python ginbox-cleaner.py
```

## How it works

1. **Connect** — enter your Gmail address + app password.
2. **Fetch senders** — the app reads Inbox *headers only* (sender and
   count; message bodies are never downloaded) and ranks senders.
3. **Mark** — buttons or right-click: Keep → Delete → Undecided.
4. **Delete marked senders** — choose Trash or permanent; a progress
   window shows live status and lets you cancel.

| | Move to Trash | Permanently delete |
|---|---|---|
| Recoverable | Yes, ~30 days | **No** |
| What happens | Copied to Trash, removed from Inbox | Inbox and Trash copies both expunged — nothing remains |

## Your data & privacy

- Decisions are saved **per Gmail account** and **encrypted at rest**
  (`decisions.enc`); the key lives in your OS credential manager
  (Windows Credential Manager / GNOME Keyring / KWallet).
- The app password goes to the same OS credential store — never into a
  readable config file. On systems with no keyring it falls back to an
  owner-only `secrets.json` (and tells you it did).
- Data folder: `~/.local/share/ginbox-cleaner` (Linux),
  `%APPDATA%\ginbox-cleaner` (Windows) — created `0700`, files `0600`.
- **Export/Import CSV** lets you back up or move decisions between
  machines and fresh installs.

## License

MIT — see [LICENSE](LICENSE). Not affiliated with Google.
