# Discord documentation sync

Updates the existing Bizarre Arena channels after relevant changes reach `main`:

- `changelog`: version notes, with no visible GitHub links.
- `characters-and-skills`: English character portraits and short skill descriptions.
- `balance-admin`: original/current values, a change summary, and yellow `*` markers for changed skill fields.

The existing message IDs and pinned navigation are recorded in `sync.json`. Changes edit these messages; new characters or versions receive new posts. Removed source entries are never automatically deleted from Discord. Large version notes and indexes are split into stable numbered parts.

## Requirements

- A Linux self-hosted GitHub Actions runner made available to this repository, running Actions Runner 2.327.1 or newer.
- Repository variable `DISCORD_RUNNER_LABELS`: a JSON array that must include `self-hosted`, `linux` and `nory-discord`, for example `["self-hosted", "linux", "nory-discord"]`. Assign the dedicated `nory-discord` label to only the one worker that owns this sync's persistent state. Additional labels such as `x64` are allowed.
- Repository Actions secret `DISCORD_BOT_TOKEN`: the existing NoRy Games bot token. Do not put it in files, workflow text, command arguments, or logs.
- A persistent runner home directory. Bindings are stored under `~/.local/state/nory-discord/ba-v9qhxb2ct6/state.json`, outside the checkout. Keep that directory across runner-container replacements and include it in backups. When replacing the worker, transfer this state and move its dedicated label before resuming the workflow.

The workflow runs on trusted `main` pushes or a manual run on `main`. Pull request checks use GitHub-hosted runners and do not receive the Discord credential or execute on the homelab worker. Actions are pinned to full commit IDs, and `GITHUB_TOKEN` only has `contents: read`.

## Source changes

Edit `balance.json`, `wiki/original-values.json`, the English wiki character pages, or the version files under `changelog/`. Original time-stop entries may have a `note` explaining that their durations are starting values which can grow during a match; the admin reference preserves this distinction.

Write prose in English. The renderer is deterministic and does not call a translation or AI service. It rejects a non-English language setting and clearly Portuguese copy instead of publishing it. Names, skill tags, numeric values and portraits remain source-driven. Explicit GitHub links are removed or rejected; portrait image URLs are still used internally by Discord.

## Manual checks

```sh
python3 -m unittest discover -s tests -v
python3 -m scripts.discord_sync.sync --check
```

`--check` requires no credential, state file or network access. `--dry-run` uses `DISCORD_BOT_TOKEN` from the environment for read-only identity, permissions and message checks. Only `--apply` publishes; it also requires an absolute `DISCORD_SYNC_STATE_PATH` outside the checkout. Never print the token or use shell tracing around credentials.

## Recovery and access

The bot verifies the guild, channel IDs, message authors, effective permissions and the private admin channel before publishing. It does not create channels, change role permissions, delete messages or grant access. Allowed mentions are empty, and new messages suppress notifications.

The worker locks and atomically saves its bindings. New posts use a stable nonce and a pending record saved before the request. After a lost response, it searches recent bot messages to recover the ID. If recovery is ambiguous or a known message is missing, it stops instead of creating duplicates. Review the failing job and repair the stored binding deliberately; do not erase the state file to bypass a recovery error.

Use **Actions → Sync Discord documentation → Run workflow** to retry after correcting a source or configuration error. A failure leaves existing posts available. The job reports counts and safe error descriptions, without credentials or full private message contents.
