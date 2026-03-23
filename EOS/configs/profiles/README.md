# Profile configs

These JSON files are release artifacts, not independent configuration systems.

## Required sync rule

`config.base.json` is the authoring baseline for shared keys. Every other profile file in this directory must stay manually synced with any release-surface change to:

- backend host/port layout
- Google / Discord integration keys
- toolpack enablement
- safety defaults
- health probe settings
- WebUI-facing capability flags

Mode-specific differences are allowed only where the profile intentionally changes hardware footprint or optional capabilities.

## Release engineering note

Before tagging a release, update all profile variants together or document why a given key is intentionally different. Treat unsynced profile drift as a release blocker.
