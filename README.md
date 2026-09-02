# codex-cost

Token usage and estimated API cost for your local [OpenAI Codex](https://github.com/openai/codex) sessions.

Codex writes a rollout log for every thread under `~/.codex/sessions/`. This tool reads those logs and tells you what each session cost, where the money went, and how much of it was cache hits. It runs entirely offline, needs only the Python standard library, and never modifies anything.

```
$ codex-cost --days 3
start        last             session                                        model            act     in   cach    out  hit%    est $
--------------------------------------------------------------------------------------------------------------------------------
08-29 20:12  09-02 14:28      Locate current directory                       5.6-sol          26m 326.7K  17.2M  44.6K  98.1    0.29~
08-29 21:00  08-31 11:22      Find Jira features to watch                    5.6-sol           7m 297.9K   2.0M  13.7K  86.9    1.69~
08-29 21:01  08-31 11:22 AUTO auto-review of team-home                       5.6-luna          1m  84.3K  90.8K    814  51.9   0.013~
09-01 15:48  09-02 13:30      Skill Registry Jira Tickets                    5.6-sol+         45m 356.3K   8.2M  37.2K  95.8     2.54
09-01 15:49  09-02 13:29 AUTO auto-review of team-home                       5.6-luna         26m 159.9K   1.1M   2.8K  87.4    0.058
--------------------------------------------------------------------------------------------------------------------------------
5 sessions in last 3 days, estimated total $4.59
  of which 2 auto-review thread(s): $0.07 (1.5%)
~ 3 session(s) span the window edge; $1.99 of their $14.48 lifetime cost falls inside it
+ session used more than one model; each turn priced at the model then active
Estimate only. Reconcile against platform.openai.com Usage and Costs.
```

## Who this is for

The dollar figures only mean something if Codex is authenticated with an **API key**, so you are billed per token. If you sign in with a ChatGPT account you are billed in credits, and the token counts are still accurate but the prices are not.

## Install

Requires Python 3.9 or newer. No dependencies.

```bash
pipx install git+https://github.com/jwm4/codex-cost
```

Or just run the script directly:

```bash
git clone https://github.com/jwm4/codex-cost
python3 codex-cost/src/codex_cost.py
```

## Usage

With no arguments it reports the current calendar month to date, which matches how most org budgets reset.

```bash
codex-cost                          # current month to date
codex-cost --all                    # every session on disk
codex-cost --month 2026-08          # one whole calendar month
codex-cost --days 30                # rolling window
codex-cost --since 2026-08-15       # activity on or after a date
codex-cost --by-repo                # aggregate by repository
codex-cost --grep rfc               # filter by title or repo (regex)
codex-cost --no-auto                # hide Codex's auto-review threads
codex-cost --only-auto              # show only auto-review threads
codex-cost --session rollout-2026-08-29T20-12-01-*.jsonl
codex-cost --rates rates.json       # override prices
codex-cost --titles-debug           # show where each session title came from
```

Run `codex-cost --help` for the full list.

### Reading the table

| Column | Meaning |
|---|---|
| `start` / `last` | First and last timestamp in the log, in local time. The date is dropped from `last` when it matches `start`. |
| `AUTO` | The thread was opened by Codex's approval auto-review harness, not by you. |
| `session` | Your custom thread name if you set one, otherwise Codex's auto-generated title, otherwise the first prompt. |
| `model` | Model used. `+` means the session switched models; `*` means none was recorded and the `--model` default was assumed. |
| `act` | Active time. Gaps longer than 15 minutes are treated as walking away and not counted. |
| `in` / `cach` / `out` | Uncached input, cached input, and output tokens. |
| `hit%` | Share of input tokens that were served from cache. |
| `est $` | Estimated cost. A trailing `~` means the session straddles the window edge and only the part inside the window is shown. |

## How it works

**Pricing is per turn, not per session.** Codex logs a running `total_token_usage` after each turn. The tool diffs consecutive totals and prices each increment at whatever model was active at that moment, so a session that switches models is charged correctly for each part.

**Sessions that cross a month boundary are split, not double counted.** A session counts toward a window if any of its activity fell inside it, but it is charged only for the tokens spent inside the window. Pass `--start-only` if you would rather select purely by start date.

**Auto-review threads are separated out.** Codex's approval harness opens its own threads to assess tool calls. They look like ordinary sessions in the log, so the tool recognises them by their opening prompt and tags them `AUTO`, then reports their share of spend separately.

**Titles come from Codex's own stores.** Session names live in SQLite databases and index files under `~/.codex` and the Codex desktop app's support directory, and which file holds them varies by build. The tool scans all of them read-only, preferring a name you set yourself over the machine-generated one. A store that cannot be opened is skipped with a note rather than a failure.

**Timestamps are converted to local time.** Codex writes UTC inside the file but names the file in local time, so reporting raw values would put sessions in the wrong hour and sometimes the wrong day.

## Rates

Prices are hard-coded in `RATES` at the top of the script as USD per million tokens, in the order `(uncached input, cached input, output)`. OpenAI changes pricing, so check them against the [official pricing page](https://platform.openai.com/docs/pricing) before trusting a number. To override without editing the script, pass a JSON file:

```json
{
  "gpt-5.6-sol": [5.00, 0.50, 30.00],
  "my-new-model": [1.00, 0.10, 8.00]
}
```

```bash
codex-cost --rates rates.json
```

Models with no rate are still counted for tokens and are listed at the bottom of the report.

## Caveats

- This is an estimate. Reconcile against the Usage and Costs pages on platform.openai.com before relying on it.
- Reasoning tokens are included in the output count that Codex reports, so they are priced at the output rate.
- The tool only sees sessions that are still on disk. If you have cleared `~/.codex/sessions`, that history is gone.
- Point it at a different Codex home with `CODEX_HOME` or a different sessions directory with `--dir`.

## License

Apache License 2.0. See [LICENSE](LICENSE).
