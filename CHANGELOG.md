# Changelog

All notable user-visible changes. The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/); versions follow [Semantic Versioning](https://semver.org/) (before 1.0, minor versions may break things).

Entries marked **reseat** need `agx reseat <name>` on existing agents to take effect.

## [Unreleased]

## [0.2.0] - 2026-10-13

### Changed
- The CLI is now called `agx` (package `agx-cli`, config in `~/.config/agx/`). Live state keeps its names: the VM, the data dir and the crontab entries still say `agents`.
- `agx create` starts new agents on the newest upstream release instead of the configured default, and skips releases that have no published image.
- Hermes containers use `/opt/data` as `HOME`, so files written by `agx exec` sessions survive container recreates. **reseat**
- `agx rollback` exports the current state to `backups/pre-rollback/` before overwriting it.
- Toolkit writes to an agent's `config.yaml` refuse to overwrite changes the agent made in the meantime.

### Fixed
- OpenClaw 2.0: agents can write their state (the image runs as non-root `node`), image tags without the `v` prefix, and the new `.config` / `.local` state directories are persisted. **reseat**
- libvirt backend: `agx mount add` / `remove` work (virtiofs tag length, share detach).
- Host-side writes are only acted on once the VM sees them, which fixes `mount remove --restart` occasionally keeping the old mount.
- bare-metal backend: `agx mount` binds the host folder, every `docker` call uses the rootless socket, and `agx doctor` checks container-log rotation.
- Export pruning for an agent no longer counts or deletes archives of another agent whose name starts with the same prefix (`foo` vs `foo-bar`).

### Removed
- Retired commands `agx codex refresh` and `agx access grant/revoke` (replaced by `agx access mount`).
- Compatibility code for pre-release installs: the `.tar.gz` archive format, the `/home/hermes` runtime home, the old Codex bridge mount and the leftover-ACL hints.

## [0.1.1] and earlier

Initial development; see the git history up to tag `v0.1.1` (2026-05-12).

[Unreleased]: https://github.com/ebner/agx/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/ebner/agx/compare/v0.1.1...v0.2.0
[0.1.1]: https://github.com/ebner/agx/releases/tag/v0.1.1
