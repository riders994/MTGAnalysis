"""Lossless background capture of MTG Arena's Player.log.

Arena overwrites Player.log at every launch and keeps exactly one previous
session as Player-prev.log, so anything not captured before a restart is gone
for good. This package tails the live log, archives each session as immutable
gzipped bytes, and snapshots the card database and tracker data alongside it.

Phase 1 is capture only: raw bytes plus enough metadata to interpret them
later. No parsing, no statistics, no schema.
"""

__version__ = "0.1.0"
