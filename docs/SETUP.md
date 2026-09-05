# Machine Setup — what a `git clone` does NOT give you

Everything in this file lives **outside the repo**. Clone this project onto a fresh
machine and the background systems (heartbeat, reflection, chat) stay dead until
these are in place. None of it is application code — it's credentials, paths, and
the environment, which is exactly why it's easy to miss and worth reading before you
go looking for a bug in the Python.

Ordered by what breaks first.

---

## 1. Claude CLI auth (breaks everything)

The scheduled jobs spawn the `claude` CLI as a subprocess. If that subprocess can't
authenticate, every LLM step fails — the heartbeat, reflection, chat replies. Data
gathering (email, calendar) still runs, so the system looks alive while producing
nothing.

### The failure signature

Your error log fills with something like:

```
Agent SDK error: success; stderr: (no stderr)
```

Confusing, because the reported subtype is `success` while `is_error` is true. The
real message only appears if you capture the assistant's own text:

```
Not logged in · Please run /login
```

### Why a desktop login isn't enough

An interactive desktop login can produce a locally cached credential with no
refresh mechanism. It works for you, sitting at the terminal, and it can't renew
itself and gives nothing to a headless subprocess running on a schedule. A working
interactive session is not proof that scheduled jobs will authenticate.

### The fix — a long-lived token in the credentials file

```bash
claude setup-token
```

Copy the **entire** token — it wraps across lines in a terminal but is one string.
Then add it to the credentials file under `.claude/` (never commit this file):

```
CLAUDE_CODE_OAUTH_TOKEN=sk-ant-oat01-...
```

Verify the length — it should be roughly 100+ characters, not 40–50:

```bash
awk -F= '/^CLAUDE_CODE_OAUTH_TOKEN=/{print length($2)}' .claude/<your-credentials-file>
```

> **Do not pipe `claude setup-token` through `tail -1`.** It truncates the token.
> You'll get a clean-looking `401 OAuth access token is invalid` instead of a
> useful error.

### Why the credentials file, not the launch units

- It's already gitignored, and it's how every other secret in this project works
- The registry's env loader pulls it into the process environment, and the SDK
  subprocess inherits that
- Putting a token in a launchd plist or systemd unit means a plaintext secret
  sitting in a world-readable config path
- The credential-guard hook blocks Read/Write access to this file — an agent
  session can't read or leak it, which is the point

**Every script that spawns the SDK must load the credentials file first**, before
doing anything else. If you add a new SDK-spawning script, give it the same call
your other scripts use to load environment variables from the credentials file
before importing the SDK.

### Smoke test

Run a two-turn query with the SDK directly and confirm it replies. Want the
assistant text to come back and `is_error` to read `False`. If it doesn't, the
auth token is the first thing to check — not your integration code.

---

## 2. Scheduled jobs — `PATH` must resolve to the real `claude`

Launch unit templates live in `.claude/launchd/` (macOS) and `.claude/systemd/`
(Linux). Install or check them with:

```bash
.venv/bin/python3 .claude/scripts/install_units.py list
.venv/bin/python3 .claude/scripts/install_units.py render
.venv/bin/python3 .claude/scripts/install_units.py install
.venv/bin/python3 .claude/scripts/install_units.py diff
```

`diff` should print `same` for every unit; anything else is drift between what's
installed and what's in the templates.

Scheduled jobs get a minimal, non-login environment, so each unit sets its own
`PATH` explicitly rather than inheriting your shell's. If that `PATH` resolves to
an old or wrong install of `claude` — a package-manager version, a stale copy on an
old `PATH` entry — the scheduled job runs against the wrong CLI silently. Symptom:
things work fine when you run a script by hand, but fail (or behave oddly) only on
the schedule. Check what a job actually finds:

```bash
which claude          # what your interactive shell finds
# then check what PATH the rendered unit sets, and whether that PATH would find
# the same binary
```

If your platform's process manager exposes a status/list command for its own
scheduled units (e.g. `launchctl list`, `systemctl status`, `crontab -l`), use it
to confirm the job is actually registered before debugging further.

---

## 3. A fresh server has no `claude` CLI at all

Different failure mode from #2, same root cause. A brand-new VPS or container has
no `claude` on `PATH` at all — no Node.js, nothing installed by your dependency
manager puts it there.

Without a discoverable CLI, the Agent SDK falls back to whatever version is bundled
inside the SDK package itself, which may be older than the one you tested locally
and can carry its own known issues (intermittent bodyless API errors under load are
one that's been seen in the wild). Nothing errors at install time, so this hides
until the schedule starts running badly. The giveaway is a line in your service
logs naming the bundled CLI path rather than a system one.

### Install it as the service's own user, not root

If your scheduled jobs run as a dedicated service user (recommended — don't run
this as root), install the CLI as that user, not via a root shell. A root-owned
install lands in root's home directory, where a lower-privilege service process
never looks.

```bash
curl -fsSL https://claude.ai/install.sh | bash
claude --version
```

### Verify

Check what `sdk_cli.best_cli_path()` resolves to (see `.claude/scripts/sdk_cli.py`)
from the working directory and user your service actually runs as. You want an
absolute path back, not `None`. Then restart any long-running service (the chat
server) — the CLI path is cached for the life of a process, so a running service
holds its first answer until restarted. One-shot scheduled jobs (heartbeat,
reflection) spawn a fresh process each run and pick up a newly installed CLI
automatically, no restart needed.

---

## 4. Integration OAuth connections (Gmail / Calendar via Composio)

Composio-routed connections live in Composio's own cloud, keyed by a user id — they
survive a machine rebuild but die the moment the underlying account revokes the
grant.

### The failure signature

Every heartbeat run floods stderr with something like:

```
404 ActionExecute_ConnectedAccountNotFound
"No connected account found for user ID <id> for toolkit gmail"
```

### Check real status

```bash
.venv/bin/python3 .claude/scripts/composio_setup.py --list
```

This filters to `ACTIVE` connections only — anything `INITIATED`, `EXPIRED`, or
revoked reads as simply missing. If you need to see *why* a connection isn't
active, query the underlying records without a status filter rather than trusting
`--list` alone.

### Reconnect

```bash
.venv/bin/python3 .claude/scripts/composio_setup.py --urls --account <account-name>
```

Three traps worth knowing before you hit them:

1. **Links expire quickly (minutes, not hours).** Generate one and use it
   immediately. A half-finished attempt sits at `INITIATED` forever and silently
   counts as not-connected.
2. **Finish the whole consent screen** — pick the account, approve every scope,
   land on the success page. Stopping early leaves it `INITIATED`.
3. **Never use a provider dashboard's own "Connect" button** if one exists outside
   this project's own setup script — it can file the connection under a different
   internal user id than the one this project expects, and every call then 404s
   even though the dashboard shows it as connected. Only use the URL your own
   setup script generates.

---

## 5. Machine-local data (gitignored — must be rebuilt or seeded on each machine)

| Path | How to restore |
|---|---|
| `.venv/` | `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` |
| Credentials file under `.claude/` | By hand, from `master.env.example` — no committed template holds real values |
| Search index database | `.venv/bin/python3 .claude/scripts/memory_index.py` (full reindex) |
| Chat session store | Recreated on first chat-server run |
| Heartbeat/reflection state files | Regenerated by the next scheduled run |

None of this syncs via Git on purpose — it's either a secret, a large regenerable
index, or genuinely per-machine operational state. If you run on two machines, the
vault (memory) syncs via Git; the search index and local state do not, and each
machine needs its own scheduled indexer.

---

## Full health check

Run whatever status command this project exposes (see the main `CLAUDE.md`) and
confirm integrations report real connected status, not just "a key is present." A
status check that only checks for the presence of an API key, rather than
confirming the connection is actually live, can report healthy while every real
call 404s — trust the check only as far as it verifies the thing it claims to.

Then confirm the loop is actually closing end to end: run the heartbeat directly
and grep its output for the connection-not-found error above. Zero hits is the
answer you want. Then check nothing new landed in the error log.

---

## Rollback: the memory-index migration

If this project's vault memory has been migrated from a single flat `MEMORY.md`
into an index-plus-pages structure (index file ≤5 KB pointing at detail pages under
`vault/entities/`, `vault/topics/`, `vault/decisions/`), the pre-migration flat file
is kept as a backup. The migration script's `backup`, `rollback`, and `verify`
commands all take a `--vault PATH` so you can rehearse against a scratch copy
before touching real data:

```bash
.venv/bin/python3 .claude/scripts/memory_migrate.py backup --vault /path/to/scratch-vault
.venv/bin/python3 .claude/scripts/memory_migrate.py rollback --vault /path/to/scratch-vault
.venv/bin/python3 .claude/scripts/memory_migrate.py verify --vault /path/to/scratch-vault
```

**What rollback actually does:** it restores the flat memory file from its backup —
nothing else. Any entity/topic/decision pages that were created stay on disk
exactly as they are; rollback doesn't delete or move them. They simply become
inert once the index no longer points at them — nothing links to them, but the
files themselves are untouched.

---

## Troubleshooting

**Bot receives messages but never replies.** This almost always means the CLI
OAuth token has expired — see §1. A `claude -p` command run manually in a bare
shell can give a false negative here (it may not load the same environment as the
service), so check the service's actual process environment, not just whether a
manual command succeeds.

**`install_units.py diff` reports drift.** Something installed on the machine
doesn't match the templates in the repo — re-render and reinstall, or investigate
what changed it out from under you (a manual edit, a different install script).

**Everything looks configured but nothing runs.** Work through this file in order
— auth, then scheduling, then CLI presence, then integration connections. Most
"nothing is working" reports trace back to one of the first three sections, not to
application code.
