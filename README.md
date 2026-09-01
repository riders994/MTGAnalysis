# MTGAnalysis

Generates reports on a player's MTG Arena log. Not meant to replace draftsim or
17lands, but stats the way I want them.

Two phases: a **collector** that captures every Arena session before Arena can
overwrite it, and an **analysis** CLI that reads that archive and writes
Markdown reports. See [USAGE.md](USAGE.md) for the full setup walkthrough of
both.

## Why capture first

Arena keeps exactly two log files. `Player.log` is overwritten at every launch,
and the session it replaces is renamed to `Player-prev.log`. Everything older is
gone. At the time this was written the entire surviving history was about thirty
minutes across two files — not a basis for an annual report, or a monthly one.

So the collector runs in the background on the Windows box and preserves every
session before Arena can destroy it. Parsing and statistics are deliberately out
of scope here: any schema designed against thirty minutes of data would be wrong.
The archive stores raw bytes plus enough metadata to interpret them later.

## Install

**[USAGE.md](USAGE.md) has the full walkthrough** — installing on Windows, and
setting up a Raspberry Pi to hold the archive and run the reports. The short
version:

Needs Python 3.11+ on the Windows machine. No third-party packages.

```
git clone <this repo>
cd MTGAnalysis
python -m collector discover
```

`discover` locates Arena's log, derives the install directory from the log's own
header, hunts for tracker databases under `%APPDATA%` and `%LOCALAPPDATA%`, and
prints a filled-in config block. Review it — especially the tracker candidates —
then save it as `config.toml`. `config.example.toml` documents every option.

Check it works in the foreground first, with Arena running:

```
python -m collector run
```

Then register it to start at logon and leave it alone:

```
python -m collector install
schtasks /Run /TN "MTGA Log Collector"
```

## Daily use

```
python -m collector status      # what has been captured, and is it still alive
python -m collector verify      # re-hash every archive against its metadata
python -m collector push        # send the archive to the storage host over SSH
```

Nothing is sent anywhere automatically. `push` is manual, and idempotent —
archive files are immutable and content-addressed, so re-running after a failed
transfer only resends what did not land.

`--config` works on either side of the subcommand.

### Detailed Logs must be on

Without it Arena writes almost none of the interesting data — no match results,
decks, or events. Turn it on in Arena under **Settings → Account → Detailed
Logs**, then restart Arena. The collector checks the flag on every session and
surfaces it in `status` and `discover`, but it cannot turn it on for you.

## What gets stored

```
archive/
  sessions/2026/08/16/session-20260816T113745-a1b2c3d4.log.gz
  sessions/2026/08/16/session-20260816T113745-a1b2c3d4.meta.json
  snapshots/carddb/Raw_CardDatabase_<name>-<hash>.gz
  snapshots/tracker/<name>-<hash>.gz
  state.json
  collector.log
```

Session captures are byte-for-byte identical to what Arena wrote; the tests
assert this against real logs. They compress 8–13x, which works out to roughly
2–9 MB stored per hour Arena is open — call it 150–500 MB a month at a couple of
hours a day. See [USAGE.md](USAGE.md) for the measurements behind that.

The `.meta.json` sidecar records the session's sha256 and byte count, the startup
timestamp, whether Detailed Logs was on, the log's first and last wall-clock
timestamps, and **the collector's UTC offset at capture time** — Arena's
timestamps carry no timezone, so that has to be recorded while it is still known.

Snapshots are content-addressed: a card database that does not change is stored
once no matter how long the collector runs. SQLite files are copied with the
backup API, so a tracker database that is actively being written yields a valid
snapshot rather than a torn one.

## How it avoids losing data

Three failure modes matter, and each has a specific defense.

**Arena restarts.** Rotation is detected from the immutable byte prefix of the
growing file and the `Startup Timestamp` in its header — never from creation
time, because NTFS *file system tunneling* re-applies the original creation
timestamp to a file recreated at the same path within about fifteen seconds,
which is exactly what an Arena restart looks like.

**The collector was down for a restart.** On every startup and every rotation it
checks `Player-prev.log` and archives it if that session has not been seen. This
is the only thing standing between a crashed collector and permanent loss.

**The collector dies mid-session.** The live session is buffered uncompressed to
a `.part` file and only compressed when the session ends. Appending to a single
gzip stream would mean a crash corrupts the tail — precisely the data most worth
keeping. On restart the buffer is either resumed in place or finalized intact.

When a session ends normally, `Player-prev.log` holds it complete, including the
final bytes Arena wrote on exit that the tail never saw. Since the buffer is by
construction a prefix of that file, prev wins and the buffer is dropped — so each
session is stored once, not twice.

The collector also opens and closes the log on every poll rather than holding a
handle. A long-lived handle without `FILE_SHARE_DELETE` would make Arena's rename
fail and break its own logging.

## Reports

The `analysis` package reads the archive and writes Markdown reports under
`archive/reports/`. Regenerated from scratch on every run — there is no
separate state file to keep in sync with the archive they're derived from, so
it is always safe to just run it again.

```
python -m analysis all-reports    # regenerate every report below in one pass
```

Or run one report at a time:

```
python -m analysis deck-changelog  # per-deck Markdown changelogs of deck-building history
python -m analysis card-stats      # per-deck personal card-performance reports (GP/OH/GD/GIH/GNS win rates)
python -m analysis bracket-stats   # best-effort Brawl bracket-signal report, win-rate-based (no ground-truth data)
python -m analysis reports         # monthly/seasonal/annual rollups of card-stats' metrics, by format group
```

`--config` works the same way it does for the collector. See
[USAGE.md](USAGE.md) for report layout details and the archive path used by
each machine.

## Tests

```
python -m pytest tests/ -q
```

The suite covers append, truncation, same-size replacement (the tunneling case),
restart-and-resume, rotation-while-down, and redundant-capture suppression. When
`Player.log` and `Player-prev.log` are present in the repo root they are replayed
through the collector in irregular chunks and the archive is asserted
byte-identical to the sources; without them those tests skip.

## How matches are parsed

The log's match lifecycle is `EventJoin` → `EventSetDeckV3` →
`EventEnterPairing` → `MatchGameRoomStateChangedEvent`, with `EventSetDeckV3`
tying each match to a deck and decklist, and the room state's
`finalMatchResult` carrying the outcome. Cards appear only as numeric
`grpId`s, which is what the card database snapshots are for.
