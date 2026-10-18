# Daily CortenDesk release sync

The VM job checks the latest stable `rustdesk/rustdesk` tag, builds only
configured platforms that do not yet have a matching release in
`Ryanrobertolson/rdgen`, publishes installer files and SHA-256 metadata as
GitHub Release assets, then syncs new assets to CortenDesk. Builds are
serialized. Successful outputs are published even when another platform
build fails.

The systemd timer is scheduled for 10:00 UTC daily. The unit expects its two
secret JSON files through encrypted systemd credentials; it does not read
credentials from the repository or write them to logs. Build settings and
icons are supplied in a private VM-side config directory, not committed here.

Run `python3 sync.py --check-only` to inspect release state without starting a
build or logging in to CortenDesk.
