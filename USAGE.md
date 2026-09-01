# Usage

Two machines, two jobs:

- **Windows** runs the collector. It captures every Arena session before Arena
  can overwrite it, into a local archive that is the source of truth.
- **Raspberry Pi** stores the archive long-term and runs the reports (Part C).

Nothing moves between them automatically. `push` is a command you run.

---

# Part A — Install on Windows

## A1. Prerequisites

**Python 3.11 or newer.** The collector uses `tomllib`, which arrived in 3.11.

```powershell
python --version
```

If it is missing or older, install from [python.org](https://www.python.org/downloads/windows/)
and **tick "Add python.exe to PATH"** on the first screen of the installer.

**OpenSSH client** — needed for `push` in Part B, not for capture. Windows 10
(1809+) and 11 include it. Check:

```powershell
ssh -V
```

If that fails: *Settings → System → Optional features → Add a feature → OpenSSH
Client*.

## A2. Turn on Detailed Logs in Arena

**Do this first — without it the capture is worthless.** Arena writes match
results, decklists and event data only when this is on.

In Arena: **Settings → Account → Detailed Logs (Plugin Support)**, then
**restart Arena**. The setting only takes effect on a fresh launch.

The collector checks the flag on every session and reports it in `status` and
`discover`, but it cannot turn it on for you.

## A3. Get the code onto the Windows machine

```powershell
cd $env:USERPROFILE
git clone <this repo> MTGAnalysis
cd MTGAnalysis
```

No dependencies to install — it is standard library only.

## A4. Generate the config

```powershell
python -m collector discover
```

This finds Arena's log, derives the install directory from the log's own header
(so Steam and standalone installs both work), and scans `%APPDATA%` and
`%LOCALAPPDATA%` for tracker databases. It prints a ready-to-paste config block.

**Read the output before using it.** In particular the tracker candidates are
guesses ranked by size — keep the ones that are actually yours and delete the
rest. Then save your edited version:

```powershell
notepad config.toml
```

`config.example.toml` documents every option.

## A5. Test in the foreground

With Arena running, watch it work:

```powershell
python -m collector run
```

You should see it identify a session and begin capturing. Leave it for a minute,
press `Ctrl+C`, and check:

```powershell
python -m collector status
```

`buffered` should be non-zero and `detailed` should say `enabled`. If it says
`DISABLED`, go back to step A2.

Now prove the rotation path, which is the part that matters: **quit Arena and
start it again**, with the collector running. `status` should show one archived
session and a new one in progress.

## A6. Install as a Scheduled Task

```powershell
python -m collector install
```

This registers a task that starts at logon, restarts up to three times on
failure, and has **no execution time limit** (Task Scheduler's 72-hour default
would otherwise kill it silently after three days). It runs under `pythonw.exe`,
so there is no console window.

Start it now without logging out:

```powershell
schtasks /Run /TN "MTGA Log Collector"
python -m collector status
```

`collector` should read `RUNNING`.

To remove it later: `python -m collector uninstall`.

## A7. Confirm it survives a reboot

Reboot, log in, wait a minute, then:

```powershell
cd $env:USERPROFILE\MTGAnalysis
python -m collector status
```

If `collector` says `RUNNING` and `last tick` is recent, you are done. Play
normally for a few weeks.

## Checking in on it

```powershell
python -m collector status              # is it alive, what has it captured
python -m collector status --sessions   # every session, one per line
python -m collector verify              # re-hash every archive against metadata
```

`status` is what catches silent failure: a stale `last tick`, a `DISABLED`
detailed-logs flag, or a buffer that never finalizes.

## Updating after a patch

A pull does not re-register anything. The Scheduled Task runs `pythonw -m
collector run` from the repo working directory, so it loads whatever is on disk
the next time it starts. What it will *not* do is notice a change while it is
running — the old code stays loaded until the task restarts.

Best done with **Arena closed**, so no capture is in flight:

```powershell
cd $env:USERPROFILE\MTGAnalysis
schtasks /End /TN "MTGA Log Collector"
git pull
schtasks /Run /TN "MTGA Log Collector"
python -m collector status
```

`collector` should read `RUNNING` again, with a fresh `last tick`.

Stopping mid-session is safe if you cannot close Arena first: on restart the
collector resumes from the `.part` buffer when the live log is unchanged, and
if Arena rotated the log while it was down it recovers that session from
`Player-prev.log`. The one case that genuinely loses data is leaving it stopped
across *two* Arena restarts — by the second, `Player-prev.log` has been
overwritten. So restart it before you play again, not tomorrow.

Your `config.toml` is never touched by a pull; it is gitignored. New options do
appear in `config.example.toml`, which is worth a glance after each update:

```powershell
git diff HEAD@{1} -- config.example.toml
```

### When you also need to re-run `install`

The task XML bakes in absolute paths to the interpreter and to `config.toml`, so
re-register whenever one of those moves:

- you installed or upgraded Python, so `pythonw.exe` lives somewhere new
- you moved the repo or the config
- the change notes say the task definition itself changed

```powershell
python -m collector install
schtasks /Run /TN "MTGA Log Collector"
python -m collector status
```

`install` overwrites the existing task in place, so re-running it costs nothing
if you are unsure. Note that registering does not start it — hence the
`schtasks /Run`.

### After a patch that touches capture or the archive

Verify before you push, so a bad build is caught while the source of truth is
still on Windows and the Pi's copy is still known-good:

```powershell
python -m collector verify
python -m collector push
```

If an update changes the state file format, the collector starts from a fresh
`state.json` rather than reading the old one. That costs at most one re-archived
session, which the content-addressed filenames then dedupe — nothing to do.

The Pi's clone (B5) is only used for `verify` and `status`, so it can be updated
whenever you next SSH in: `cd ~/MTGAnalysis && git pull`.

## Troubleshooting

| Symptom | Cause and fix |
|---|---|
| `collector   not running` after logon | Task did not start. `schtasks /Query /TN "MTGA Log Collector" /V /FO LIST` and check *Last Result*. |
| `detailed  DISABLED` | Detailed Logs is off in Arena. See A2, and restart Arena. |
| `watching ... (not present)` | `player_log` path is wrong. Re-run `discover`. |
| `Another collector already holds ...` | It is already running. That is the single-instance guard doing its job. |
| A fix from a patch is not taking effect | The task is still running the old code. `schtasks /End` then `/Run` it — see *Updating after a patch*. |
| Sessions missing for a day you played | The collector was down across an Arena restart *and* a second restart happened before it came back, so `Player-prev.log` had already been overwritten. Check `collector.log` in the archive directory. |

Detailed logs of the collector's own activity live in `archive\collector.log`.

---

# Part B — Storage on the Raspberry Pi

## B1. Prepare the Pi

SSH into the Pi and make a directory for the archive:

```bash
sudo mkdir -p /srv/mtga-archive
sudo chown "$USER:$USER" /srv/mtga-archive
```

**Consider putting it on a USB drive rather than the SD card.** This archive is
meant to accumulate for years, and SD cards are the least durable thing on a Pi.
If you have a drive mounted at, say, `/mnt/data`, use `/mnt/data/mtga-archive`
and substitute that path throughout.

Make sure SSH is enabled:

```bash
sudo systemctl enable --now ssh
```

**Pin the Pi's address.** `raspberrypi.local` works via mDNS on modern Windows,
but a DHCP reservation on your router (or a static IP) is worth the five minutes
— a push that fails because the Pi moved is a push you will not notice failing.

## B2. Set up key-based SSH from Windows

Passwords will not do, because you want this to be a one-command operation.

On **Windows**, generate a key if you do not have one:

```powershell
ssh-keygen -t ed25519
```

Accept the defaults. Now copy the public key to the Pi. Windows has no
`ssh-copy-id`, so do it by hand:

```powershell
type $env:USERPROFILE\.ssh\id_ed25519.pub | ssh pi@raspberrypi.local "mkdir -p ~/.ssh && cat >> ~/.ssh/authorized_keys && chmod 700 ~/.ssh && chmod 600 ~/.ssh/authorized_keys"
```

That prompts for the Pi's password once. Confirm it now works without one:

```powershell
ssh pi@raspberrypi.local "echo connected"
```

That must print `connected` with no prompt before you go on.

## B3. Point the collector at the Pi

Add to `config.toml` on Windows:

```toml
[push]
host = "pi@raspberrypi.local"
remote_dir = "/srv/mtga-archive"
# identity_file = "C:/Users/weezy/.ssh/id_ed25519"   # only if not the default
# ssh_port = 22
```

## B4. Push

```powershell
python -m collector push --dry-run    # list what would be sent
python -m collector push              # send it
```

Push works by listing what is already on the Pi and sending only what is
missing. Because archive files are immutable and content-addressed, this is
**idempotent** — a failed or interrupted transfer is fixed by running it again,
and running it twice in a row is a no-op:

```
$ python -m collector push
Nothing to send; remote is up to date.
```

The live capture buffer, the lock file, and machine-local state are never sent.

`--rsync` is available if you have rsync on the Windows box, but plain ssh is
the default on purpose: an rsync on a Windows PATH is usually WSL's, which
resolves `C:/...` against the Linux filesystem and silently sends nothing. And
rsync's advantage is efficiently re-sending files that changed, which these
never do.

### If it asks for your key passphrase

A push authenticates once, not once per file: everything missing goes up in a
single `tar` stream over one ssh command, and on Linux/macOS the listing call
shares that connection too. So the most you should ever see is one prompt per
push — two on Windows, whose OpenSSH build cannot share connections.

To get to zero, hand the key to the ssh agent once per login. On Windows,
enable the agent service (as Administrator, once ever):

```powershell
Set-Service ssh-agent -StartupType Automatic
Start-Service ssh-agent
```

then add the key (once per key, it is remembered across reboots):

```powershell
ssh-add $env:USERPROFILE\.ssh\id_ed25519
```

On Linux/macOS the agent is usually already running, so `ssh-add ~/.ssh/id_ed25519`
is all it takes. After that `push` runs without prompting at all.

## B5. Verify the copy on the Pi

Put a minimal config on the Pi — no `player_log`, because there is no Arena
here:

```bash
cat > ~/mtga.toml <<'EOF'
[paths]
archive_dir = "/srv/mtga-archive"
EOF
```

Then, if the Pi has Python 3.11+ (Raspberry Pi OS *Bookworm* does; *Bullseye*
ships 3.9 and will not run this):

```bash
git clone <this repo> ~/MTGAnalysis
cd ~/MTGAnalysis
python3 -m collector --config ~/mtga.toml verify
python3 -m collector --config ~/mtga.toml status
```

`verify` decompresses every session and re-hashes it against the sha256 recorded
at capture time, so a clean result means the data survived the trip intact.

On an older Pi OS you can still spot-check integrity without Python 3.11:

```bash
find /srv/mtga-archive -name '*.gz' -exec gzip -t {} +
```

Silence means every archive decompresses cleanly.

## B6. A routine that works

Roughly weekly, or whenever you think of it:

```powershell
python -m collector status    # sanity check the Windows side
python -m collector push      # ship it
```

Then occasionally on the Pi, `verify`.

The one rule worth internalising: **the Windows archive is the source of truth
until it has been pushed.** Do not clear it out before the push lands.

### If you would rather not remember

The Pi is always on, so a scheduled push is easy. Rather than fight
`schtasks` quoting, put the command in a small wrapper — save this as
`push.cmd` in the repo:

```bat
@echo off
cd /d "%~dp0"
python -m collector push >> "%~dp0push.log" 2>&1
```

Then register it to run daily:

```powershell
schtasks /Create /TN "MTGA Archive Push" /SC DAILY /ST 03:00 /F /TR "%USERPROFILE%\MTGAnalysis\push.cmd"
```

The wrapper handles the working directory and keeps a log, which matters
because a scheduled push failing is exactly the kind of thing you would not
otherwise notice. This is left out of `install` deliberately — check
`push.log` and the Pi's copy every so often if you go this route.

---

## How much space this needs

Measured from the two real sample sessions:

| Situation | Raw | Stored (gzip) |
|---|---|---|
| Playing matches | 29 MB/hour | **2.2 MB/hour** |
| Sitting in menus | 69 MB/hour | **8.5 MB/hour** |

So budget by how long Arena is *open*, not how much you play:

| Habit | Per month, stored |
|---|---|
| 1 h/day | 65–255 MB |
| 2 h/day | 130–510 MB |
| 4 h/day | 265 MB–1 GB |

A few gigabytes a year, worst case. Trivial for a USB drive, and fine on an SD
card too — but see the durability note in B1.

The Windows side needs room for the archive plus one uncompressed session in
progress (tens of MB, released when the session ends).

## What is in the archive

```
/srv/mtga-archive/
  sessions/2026/08/16/session-20260816T113745-a1b2c3d4.log.gz
  sessions/2026/08/16/session-20260816T113745-a1b2c3d4.meta.json
  snapshots/carddb/Raw_CardDatabase_<name>-<hash>.gz
  snapshots/tracker/<name>-<hash>.gz
  collector.log
```

Session files are byte-for-byte what Arena wrote — `gunzip -c` any of them and
you have the original log back. The `.meta.json` beside each one records its
sha256 and size, the startup timestamp, whether Detailed Logs was on, the first
and last wall-clock timestamps in the log, and the collector's UTC offset at
capture time (Arena's own timestamps carry no timezone, so it has to be recorded
while it is still known).

Snapshots are stored by content hash, so an unchanged card database is kept once
no matter how many times it is seen.

Reports are built against this directory, which is why it lives on the Pi
rather than only on the gaming machine — see Part C.

---

# Part C — Running the reports

Run this on the Pi, against the same `~/mtga.toml` from B5 — reports read the
archive, they don't touch Arena or the collector.

## C1. Regenerate everything

```bash
cd ~/MTGAnalysis
python3 -m analysis --config ~/mtga.toml all-reports
```

This runs all four report types in one pass — `deck-changelog`, `card-stats`,
`bracket-stats`, then `reports` — and prints each one's own summary as it
goes. Every report is regenerated from scratch on every run: there is no
separate state file to keep in sync with the archive, so re-running after
pushing new sessions is always safe and just picks up whatever is new.

## C2. Or run one report at a time

```bash
python3 -m analysis --config ~/mtga.toml deck-changelog  # per-deck Markdown changelogs of deck-building history
python3 -m analysis --config ~/mtga.toml card-stats       # per-deck personal card-performance reports (GP/OH/GD/GIH/GNS win rates)
python3 -m analysis --config ~/mtga.toml bracket-stats    # best-effort Brawl bracket-signal report, win-rate-based (no ground-truth data)
python3 -m analysis --config ~/mtga.toml reports          # monthly/seasonal/annual rollups of card-stats' metrics, by format group
```

`--config` works on either side of the subcommand, same as the collector.

## C3. Where reports land

```
/srv/mtga-archive/reports/
  changelogs/<deck>.md
  card_stats/<deck>.md
  bracket_stats/overview.md
  annual/summary/<year>-<format>.md
  annual/decks/<year>-<deck>.md
  monthly/summary/<year>-<month>-<format>.md
  monthly/decks/<year>-<month>-<deck>.md
  seasonal/summary/<season>-<format>.md
  seasonal/decks/<season>-<deck>.md
```

Deck names are slugified, and a name collision (two decks that legitimately
share a display name) gets a short hash suffix appended so neither report
silently overwrites the other.

## C4. A routine that works

Right after B6's push, so reports reflect what just landed:

```bash
python3 -m analysis --config ~/mtga.toml all-reports
```
