# mail-triage-jev

Local AI email triage for Gmail. A small open model runs **on your own Mac** (no email leaves your machine) and:

- **Filters junk**: cold sales, marketing and spam get a label and leave your inbox. Nothing is ever deleted.
- **Flags what matters**: your VIPs, travel bookings, and anything with a question or task for you get a red label.
- **Tags customers**: one label per customer company (`Customers/acme.example`), learned from your own labels.

It uses [Cloudflare Clef-flash](https://huggingface.co/Cloudflare/clef-flash), an open "decision model" (API-compatible with TypeSafe's Jev) that returns probabilities instead of text.

**Safety:** it only adds and removes Gmail labels. It cannot delete or send mail. Start in `shadow` mode, which tags but never moves anything.

## Requirements

- A Mac with Apple silicon and 32 GB+ of memory (the model uses about 18 GB while it runs)
- About 25 GB of free disk
- A Gmail or Google Workspace account
- [Homebrew](https://brew.sh)

## 1. Create the Google app (one time, about 5 minutes)

You need your own OAuth client so the tool can read and label your mail. Nothing here is shared.

1. Go to <https://console.cloud.google.com> and sign in with the Gmail account you want to triage.
2. **Create a project** (top bar → *New project*), e.g. `mail-triage`.
3. **APIs & Services → Library → Gmail API → Enable.**
4. **Google Auth Platform** (OAuth consent screen):
   - Work or school Google Workspace account: choose **Internal**.
   - Personal Gmail: choose **External**, add your own address under **Test users**, then set the publishing status to **In production** (you'll see an "unverified app" warning when you sign in; it's your own app, so continue). Without this, Google expires the sign-in every 7 days.
5. **Clients → Create client → Application type: Desktop app → Create → Download JSON.**
6. Save that file as **`client_secret.json`** in the project folder. It is git-ignored; never commit it.

The only permission requested is `gmail.modify` (read mail and change labels; it cannot delete or send).

## 2. Install

```bash
brew install uv
git clone https://github.com/tjorourke/mail-triage-jev.git
cd mail-triage-jev
uv sync
uv run hf download Cloudflare/clef-flash --local-dir models/clef-flash   # ~18 GB
cp config.example.toml config.toml
cp allowlist.example.txt allowlist.txt
cp blocklist.example.txt blocklist.txt
```

Edit **`config.toml`**: set `own_domains`, `my_addresses` and the `[profile]` section (a few lines about your job, so the AI has context). Everything else has sensible defaults and is explained in the file.

## 3. Try it

```bash
uv run triage selftest          # checks the model runs on your Mac and sorts sample emails
uv run triage login             # opens your browser once to approve access
uv run triage run --dry-run     # classify your recent mail and print decisions; changes nothing
uv run triage run               # shadow mode: tags AI/WouldFilter, inbox untouched
```

Look at the `AI/WouldFilter` label in Gmail. Remove the label from anything that is real mail and that sender is never filtered again. Add the `AI/Filtered` label to junk it missed and that sender is blocklisted. `uv run triage report` shows the numbers.

The first run takes a few minutes: the model takes about 40 seconds to load and warm up, then it classifies about one email per second.

## 4. Go live

When the shadow results look right (after a few days is sensible), set `mode = "live"` in `config.toml`, then:

```bash
uv run triage promote                       # move everything tagged AI/WouldFilter out of the inbox
uv run triage backfill --days 365 --limit 3000   # also clean up older inbox mail (add --dry-run first)
uv run triage undo --since 24h              # panic button: put the last 24 hours back
```

## 5. Run it automatically

```bash
./service.sh install     # starts at login, checks every hour (when plugged in), restarts if it crashes
./service.sh status
./service.sh logs
./service.sh uninstall
```

macOS may ask once for Keychain access; choose **Always Allow**. The model is loaded only when there is new mail and is unloaded 10 minutes later.

**Pause it** for a demo or a call: `uv run triage pause 2h` and `uv run triage resume`. To tie it to Do Not Disturb, create two Shortcuts automations (Focus → Do Not Disturb turns on/off → *Run Shell Script*) that run `bin/focus-on` and `bin/focus-off`.

## Commands

| Command | What it does |
|---|---|
| `triage login` / `logout` | Sign in to Google (token kept in the macOS Keychain) / remove it |
| `triage run [--dry-run]` | Check new mail now |
| `triage backfill --days N` | Process mail already in the inbox |
| `triage promote` | Move everything tagged `AI/WouldFilter` out of the inbox |
| `triage undo --since 24h` | Put recently filtered mail back in the inbox |
| `triage report` | Counts, precision, what was filtered and flagged |
| `triage flag --days 14` | Mark important mail (VIPs, bookings, questions/tasks) already in the inbox |
| `triage customers discover` | Ask the AI which companies look like customers or prospects |
| `triage customers candidates` / `add <domain>` / `approve` | Review and approve them |
| `triage customers list` / `remove <domain>` | Show / drop customers |
| `triage reeval [--model]` | Re-check tagged mail after you change the rules |
| `triage learn` | Apply your label corrections now |
| `triage pause [2h]` / `resume` / `status` | Stop / restart the background service |

## How it decides

In order, the first match wins:

1. Starred, or from your own company (`own_domains`), allowlisted, or a customer domain: **kept**
2. On your blocklist: **filtered**
3. Someone you have emailed, a calendar invite, a thread you replied in: **kept**
4. Hotel, invoice or travel reservation: **kept**
5. Otherwise the AI gives a probability for each kind of mail (real, transactional, newsletter, marketing, cold sales, spam...) and mail is only filtered when it is confident enough (`threshold`); anything uncertain stays in your inbox.

Mail that fails Gmail's sender-authentication check (someone faking a domain) never gets the benefit of the "trusted domain" rules.

## Customers

Put the **`Customers`** label on any email from a new company. On the next run it learns that company's domain, creates `Customers/<domain>`, and labels every email to or from that domain. Delete a customer's label to drop them. See `config.example.toml` for all the options.

## Your settings stay private

`config.toml`, `allowlist.txt`, `blocklist.txt`, `client_secret.json`, the database and logs are all git-ignored. Only the `*.example` files are in the repo.

## Licence and credits

MIT (see `LICENSE`). The model is [Cloudflare Clef-flash](https://huggingface.co/Cloudflare/clef-flash) (Apache-2.0), downloaded separately and not included here. This project is not affiliated with Cloudflare or TypeSafe.
