# ai-skills-sync

Sync personal `SKILL.md` directories across AI coding assistants:

- **Claude** `~/.claude/skills`
- **Cursor** `~/.cursor/skills`
- **Copilot** `~/.copilot/skills`
- **Codex** `~/.codex/skills`

## Installation

```sh
pip install ai-skills-sync
```

## Usage

```sh
# show what would change
ai-skills-sync --dry-run

# apply changes
ai-skills-sync

# list current inventory
ai-skills-sync --list

# exit non-zero if changes are pending (useful in CI)
ai-skills-sync --check
```

If the same skill name exists in multiple tools with different content, the command stops and prints a conflict. Resolve it by choosing a winner:

```sh
ai-skills-sync --prefer claude
ai-skills-sync --prefer cursor
ai-skills-sync --prefer copilot
ai-skills-sync --prefer codex
```

## Configuration

The default config lives at `~/.config/ai-skills-sync/config.json`. To generate it:

```sh
ai-skills-sync --write-default-config
```

Override the path with the `AI_SKILLS_SYNC_CONFIG` environment variable.

## Automatic sync (Linux systemd)

Install the bundled systemd units:

```sh
# install units only
ai-skills-sync --install-systemd

# install and enable the timer immediately
ai-skills-sync --install-systemd --enable-timer
```

The timer runs every 10 minutes. To manage it manually:

```sh
systemctl --user daemon-reload
systemctl --user enable --now ai-skills-sync.timer

# disable
systemctl --user disable --now ai-skills-sync.timer
```

## State file

The state file at `~/.local/state/ai-skills-sync/state.json` tracks the last-known hash of each skill at each endpoint. It is used to detect which copy changed when a conflict arises.

## License

MIT
