# Dayflow runbook

What to do when something is wrong, written for the person who installed it six weeks ago
and has forgotten how it works. Start every investigation the same way:

```bash
dayflow doctor          # is everything it needs in place?
dayflow health          # has it been failing, slow or expensive?
tail -50 ~/Library/Logs/dayflow.log
```

## No brief this morning

| What you see | Likely cause | Do this |
|---|---|---|
| No notification, no file in `briefs/` | The Mac was off at the scheduled time, or the schedule is not loaded | `dayflow schedule status`. If "loaded: NO", `dayflow schedule install --at HH:MM`. Then `dayflow run`. |
| Notification "Dayflow failed: Google ..." | The Google sign-in expired (7 days in Testing mode) or was revoked | `dayflow auth login` (add `--drafts` if you use drafts), then `dayflow run` |
| "Dayflow failed: api unavailable ..." | Anthropic API outage or no network at that moment | `dayflow run`. It resumes the failed run and does not pay for finished steps again. |
| "Dayflow failed: token budget exceeded" | An unusually large inbox | `DAYFLOW_INBOX_MAX=15 dayflow run --force`, then look at what arrived |
| "Dayflow failed: no working ANTHROPIC_API_KEY" | Key missing, revoked or out of credit | `dayflow doctor --live`; fix the key in `.env` or `dayflow secret set anthropic-api-key` |
| Log says "skipped: another Dayflow run holds the lock" every day | A run crashed holding the lock | It clears itself after 30 minutes. To clear now: delete `briefs/.dayflow.lock` |

## The brief is wrong

1. `dayflow trace` shows what the run read and did. Start there, not with the prompt.
2. If a tool failed, the trace says which and why. See lesson 91.
3. If the model misjudged, save the case: add it to the eval fixtures, then change the prompt,
   then `dayflow eval -n 10 --compare evals/baseline-sonnet-5-5.json`. Do not ship a prompt
   change on one good-looking run.

## It costs more than it used to

1. `dayflow stats` and compare `$/run` and `in/run` with last week.
2. `dayflow trace <run_id>` on an expensive run: which step, input or output?
3. Usual causes: a bigger inbox (lower `DAYFLOW_INBOX_MAX`), caching turned off
   (`DAYFLOW_CACHE`), a model change, or a price change (`PRICES` in `dayflow/observe.py`).
4. The alert threshold is `DAYFLOW_COST_ALERT` (dollars per run, default 0.15).

## Changing the model or the prompt

```bash
dayflow eval -n 10 --out before.json
# make the change
dayflow eval -n 10 --compare before.json
```

Only merge when no `must` check moved, and you have read the traces for anything that did.

## Secrets

| Task | Command |
|---|---|
| Rotate the Anthropic key | Create a new key, `dayflow secret set anthropic-api-key` (or edit `.env`), `dayflow doctor --live`, then revoke the old key |
| Cut off Google access | `dayflow auth logout`. If that fails, remove Dayflow at myaccount.google.com/permissions |
| Move the Google token to the keychain | `pip install keyring`, `dayflow auth migrate --to keychain`, add `DAYFLOW_TOKEN_STORE=keychain` to `.env` |
| You think a secret leaked | Revoke it at the provider first. Then rotate. Then look for how. |

## The database

`dayflow.db` holds every email body the model read, every brief and every proposal.
Treat it like your mailbox.

| Task | Command |
|---|---|
| Back up now | `dayflow backup` (also runs before every scheduled brief; keeps 7) |
| Restore | Stop the schedule (`dayflow schedule uninstall`), `cp backups/dayflow-YYYYMMDD.db dayflow.db`, `dayflow doctor`, reinstall the schedule |
| Forget old content | `dayflow prune --days 30`: deletes transcripts and brief text for old runs, keeps token counts and cost history |
| "database is locked" | Another Dayflow process is running. `ps aux | grep dayflow`. |

## Turning it off

```bash
dayflow schedule uninstall
dayflow auth logout
dayflow secret delete anthropic-api-key
```

Then delete `dayflow.db`, `briefs/` and `backups/` if you want nothing left behind.
