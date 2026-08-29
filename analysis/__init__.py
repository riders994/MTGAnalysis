"""Reports generated from archived Player.log sessions.

Reads the collector's archive (sessions plus card database snapshots) and
renders Markdown reports, regenerated from scratch on every run — there is
no separate state file to keep in sync with the archive they're derived
from. Covers deck-building changelogs, and personal card-performance stats
(GP/OH/GD/GIH/GNS win rates) reformulated from 17lands' draft-only metrics
for this account's own Brawl/Standard games.
"""

__version__ = "0.1.0"
