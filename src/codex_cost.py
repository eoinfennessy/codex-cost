#!/usr/bin/env python3
"""
codex_cost.py - token usage and estimated API cost per Codex session.

Reads the rollout JSONL files under ~/.codex/sessions/. Meaningful only when
Codex is authenticated with an API key (per-token billing); with ChatGPT
sign-in you are billed in credits and these dollar figures do not apply.

    python3 codex_cost.py                  # current month to date (default)
    python3 codex_cost.py --all            # every session on disk
    python3 codex_cost.py --month 2026-08  # one whole calendar month
    python3 codex_cost.py --days 30
    python3 codex_cost.py --since 2026-08-15
    python3 codex_cost.py --start-only     # ignore carry-over from prior months
    python3 codex_cost.py --by-repo
    python3 codex_cost.py --no-auto        # hide auto-review threads
    python3 codex_cost.py --only-auto      # only auto-review threads
    python3 codex_cost.py --grep rfc
    python3 codex_cost.py --session rollout-2026-08-29T20-12-01-*.jsonl
    python3 codex_cost.py --rates rates.json

A session counts toward a window if any of its activity fell inside it, and it
is charged only for the tokens it spent there, so work spanning a month
boundary is split rather than double counted.

Stdlib only. Read-only.
"""

import argparse
import datetime as dt
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import tempfile

# USD per 1M tokens: (uncached input, cached input, output).
# VERIFY against https://platform.openai.com/docs/pricing before trusting.
RATES = {
    "gpt-5.6-sol":   (5.00, 0.50, 30.00),
    "gpt-5.6-terra": (2.00, 0.20, 12.00),
    "gpt-5.6-luna":  (0.20, 0.02,  1.20),
    "gpt-5.5":       (1.25, 0.125, 10.00),
    "gpt-5.4":       (1.25, 0.125, 10.00),
    "gpt-5.4-mini":  (0.25, 0.025,  2.00),
}
DEFAULT_MODEL = "gpt-5.6-sol"

CODEX_HOME = pathlib.Path(os.environ.get("CODEX_HOME", pathlib.Path.home() / ".codex"))
SESSIONS_DIR = CODEX_HOME / "sessions"
TS_RE = re.compile(r"rollout-(\d{4}-\d{2}-\d{2}T\d{2}-\d{2}-\d{2})-(.+)\.jsonl$")

# Codex's approval auto-review harness opens every thread with this. Matching on
# the opening prompt is the only reliable marker: `originator` reads
# "Codex Desktop" for these exactly as it does for threads you started.
AUTO_REVIEW_MARKERS = (
    "the following is the codex agent history whose request action you are assessing",
    "treat the transcript, tool call arguments, tool results, retry reason",
)

INJECTED_PREFIXES = (
    "<environment_context", "<user_instructions", "<project_doc", "<agents",
    "# agents.md", "## my environment", "<user_shell", "<personalization",
    "<memories", "<compaction", "<system_reminder",
)

# Gaps longer than this mean you walked away; they don't count as active time.
IDLE_GAP_MIN = 15


# Title columns/keys, best first. Codex keeps the auto-generated first-utterance
# title in `title` and the name you set yourself in `name` / `display_title` /
# `thread_name`, so preference order decides whether you get your rename or the
# machine's guess.
TITLE_KEYS = [
    "display_title", "thread_name", "custom_title", "user_title",
    "name", "title", "summary",
]
ID_KEYS = ["id", "thread_id", "session_id", "uuid", "rollout_id"]

TITLE_ROOTS = [
    CODEX_HOME,
    pathlib.Path.home() / "Library/Application Support/Codex",
    pathlib.Path.home() / "Library/Application Support/ChatGPT",
    pathlib.Path.home() / ".config/codex",
]
DB_SUFFIXES = (".sqlite", ".sqlite3", ".db")


def _iter_db_files(root):
    """Walk a tree for sqlite files, tolerating unreadable directories."""
    try:
        for dirpath, dirnames, filenames in os.walk(root, onerror=lambda e: None):
            if "sessions" in pathlib.Path(dirpath).parts:
                dirnames[:] = []
                continue
            for fn in filenames:
                if fn.lower().endswith(DB_SUFFIXES) and not fn.startswith("logs"):
                    yield pathlib.Path(dirpath) / fn
    except OSError:
        return


def _open_ro(path, problems):
    """Open a sqlite file for reading without disturbing it.

    Plain mode=ro fails on a WAL database whose -shm sidecar is absent, because
    read-only connections cannot create one. Fall back to reading a temporary
    copy (database plus its -wal and -shm) so the original is never touched.
    """
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True, timeout=2.0)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con, None
    except sqlite3.Error:
        pass

    tmp = tempfile.mkdtemp(prefix="codexcost-")
    try:
        dest = pathlib.Path(tmp) / path.name
        shutil.copy2(path, dest)
        for side in ("-wal", "-shm"):
            s = path.with_name(path.name + side)
            if s.exists():
                shutil.copy2(s, dest.with_name(dest.name + side))
        con = sqlite3.connect(str(dest), timeout=2.0)
        con.execute("SELECT count(*) FROM sqlite_master").fetchone()
        return con, tmp
    except (sqlite3.Error, OSError, shutil.Error) as exc:
        shutil.rmtree(tmp, ignore_errors=True)
        problems.append(f"{path}: {exc}")
        return None, None


def load_titles(debug=False):
    """id -> best-ranked title per session.

    Scans every local store, not just state_*.sqlite: the rename may live in
    codex-dev.db or session_index.jsonl depending on build. Lower rank wins.
    A store that cannot be read is skipped rather than fatal.
    """
    best = {}
    problems = []

    def offer(sid, title, key, source):
        if not sid or not isinstance(title, str) or not title.strip():
            return
        try:
            rank = TITLE_KEYS.index(key)
        except ValueError:
            return
        sid = str(sid).lower()
        cur = best.get(sid)
        if cur is None or rank < cur[1]:
            best[sid] = (title.strip(), rank, source)

    for root in TITLE_ROOTS:
        if not root.is_dir():
            continue
        for db in sorted(_iter_db_files(root)):
            con, tmp = _open_ro(db, problems)
            if con is None:
                continue
            try:
                cur = con.cursor()
                try:
                    tables = [r[0] for r in cur.execute(
                        "SELECT name FROM sqlite_master WHERE type='table'")]
                except sqlite3.Error as exc:
                    problems.append(f"{db}: {exc}")
                    continue
                for t in tables:
                    try:
                        cols = [r[1] for r in cur.execute(f'PRAGMA table_info("{t}")')]
                    except sqlite3.Error:
                        continue
                    lower = {c.lower(): c for c in cols}
                    icol = next((lower[k] for k in ID_KEYS if k in lower), None)
                    tcols = [lower[k] for k in TITLE_KEYS if k in lower]
                    if not (icol and tcols):
                        continue
                    sel = ", ".join(f'"{c}"' for c in [icol] + tcols)
                    try:
                        for row in cur.execute(f'SELECT {sel} FROM "{t}"'):
                            for col, val in zip(tcols, row[1:]):
                                offer(row[0], val, col.lower(), f"{db.name}:{t}.{col}")
                    except sqlite3.Error:
                        continue
            finally:
                con.close()
                if tmp:
                    shutil.rmtree(tmp, ignore_errors=True)

        idx = root / "session_index.jsonl"
        if idx.is_file():
            try:
                for line in idx.open(encoding="utf-8", errors="replace"):
                    try:
                        rec = json.loads(line)
                    except json.JSONDecodeError:
                        continue
                    sid = next((rec[k] for k in ID_KEYS if isinstance(rec.get(k), str)), None)
                    for k in TITLE_KEYS:
                        if isinstance(rec.get(k), str):
                            offer(sid, rec[k], k, f"{idx.name}:{k}")
            except OSError as exc:
                problems.append(f"{idx}: {exc}")

    if debug:
        by_source = {}
        for _, (_, _, src) in best.items():
            by_source[src] = by_source.get(src, 0) + 1
        print(f"resolved {len(best)} title(s):", file=sys.stderr)
        for src, n in sorted(by_source.items(), key=lambda kv: -kv[1]):
            print(f"  {n:>4}  {src}", file=sys.stderr)
        for p in problems:
            print(f"  unreadable: {p}", file=sys.stderr)
    elif problems and len(problems) == 1:
        print(f"note: skipped an unreadable store ({problems[0]}). "
              f"Titles may be incomplete; run --titles-debug for detail.", file=sys.stderr)
    elif problems:
        print(f"note: skipped {len(problems)} unreadable stores. "
              f"Titles may be incomplete; run --titles-debug for detail.", file=sys.stderr)
    return {k: v[0] for k, v in best.items()}


def text_of(content):
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for c in content:
            if isinstance(c, str):
                out.append(c)
            elif isinstance(c, dict):
                t = c.get("text") or c.get("input_text")
                if isinstance(t, str):
                    out.append(t)
        return "\n".join(out)
    return ""


def looks_injected(t):
    return any(t.lstrip()[:200].lower().startswith(p) for p in INJECTED_PREFIXES)


def oneline(text, width=46):
    if not text:
        return None
    for raw in text.splitlines():
        line = raw.strip()
        if line and not line.startswith(("<", "#", "```")):
            line = re.sub(r"\s+", " ", line)
            return line if len(line) <= width else line[: width - 1] + "\u2026"
    flat = re.sub(r"\s+", " ", text).strip()
    return flat[: width - 1] + "\u2026" if len(flat) > width else flat or None


def parse_ts(s):
    """Parse an ISO timestamp and convert to local time.

    Codex writes UTC inside the file but names the file in local time, so
    reporting the raw value puts sessions in the wrong hour and sometimes the
    wrong day."""
    if not isinstance(s, str):
        return None
    try:
        d = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
    except ValueError:
        return None
    if d.tzinfo is None:
        return d
    return d.astimezone().replace(tzinfo=None)


def normalize_model(name):
    if not name:
        return None
    n = name.lower().strip().replace("_", "-").replace(" ", "-")
    if n in RATES:
        return n
    for k in RATES:
        if k.replace("gpt-", "") in n:
            return k
    for k in RATES:
        if n.startswith(k):
            return k
    return None


def human(n):
    if n is None:
        return "-"
    for u, d in (("B", 1e9), ("M", 1e6), ("K", 1e3)):
        if n >= d:
            return f"{n/d:.1f}{u}"
    return str(n)


def money(x):
    if x is None:
        return "-"
    if x and x < 0.10:
        return f"{x:.3f}"
    return f"{x:.2f}"


def scan(path, rates, fallback, window=(None, None)):
    """One pass per rollout file. Prices each token_count delta at whatever
    model was active at that point, since a session can switch models.

    `window` is (cutoff, until). Each delta is also attributed to the month it
    actually occurred in, so a session spanning a month boundary reports only
    the spend that fell inside the window rather than being counted whole."""
    win_from, win_to = window
    model_now = None
    models = []
    prev = {"input_tokens": 0, "cached_input_tokens": 0, "output_tokens": 0}
    agg = {"unc": 0, "cache": 0, "out": 0, "reason": 0}
    usd = 0.0
    usd_win = 0.0
    unpriced = set()
    turns = 0
    totals = None
    prompts = []
    n_user = n_tool = n_compact = 0
    peak_ctx = 0
    cwd = repo = branch = sid = effort = None
    first_ts = last_ts = None
    active = dt.timedelta()

    def absorb(b):
        nonlocal model_now, cwd, repo, branch, sid, effort
        if not isinstance(b, dict):
            return
        for k in ("model", "model_slug", "model_name"):
            v = b.get(k)
            if isinstance(v, str):
                model_now = v
                if v not in models:
                    models.append(v)
        if cwd is None and isinstance(b.get("cwd"), str):
            cwd = b["cwd"]
        for k in ("reasoning_effort", "effort"):
            if effort is None and isinstance(b.get(k), str):
                effort = b[k]
        if sid is None:
            for k in ("id", "session_id", "thread_id", "conversation_id"):
                v = b.get(k)
                if isinstance(v, str) and len(v) > 8:
                    sid = v
                    break
        g = b.get("git")
        if isinstance(g, dict):
            if repo is None:
                u = g.get("repository_url") or g.get("remote_url")
                if isinstance(u, str):
                    repo = re.sub(r"\.git$", "", u.rstrip("/")).split("/")[-1]
            if branch is None and isinstance(g.get("branch"), str):
                branch = g["branch"]

    try:
        with path.open(encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line.startswith("{"):
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue

                ts = parse_ts(rec.get("timestamp"))
                if ts:
                    if last_ts:
                        gap = ts - last_ts
                        if gap.total_seconds() <= IDLE_GAP_MIN * 60:
                            active += gap
                    first_ts = first_ts or ts
                    last_ts = ts

                payload = rec.get("payload") if isinstance(rec.get("payload"), dict) else {}
                absorb(rec)
                absorb(payload)
                info = payload.get("info") if isinstance(payload.get("info"), dict) else None
                absorb(info)

                ptype = str(payload.get("type") or rec.get("type") or "")

                if ptype == "token_count" and info:
                    tt = info.get("total_token_usage")
                    if isinstance(tt, dict):
                        turns += 1
                        totals = tt
                        # Price the increment at the model in play right now.
                        di = (tt.get("input_tokens", 0) or 0) - prev["input_tokens"]
                        dc = (tt.get("cached_input_tokens", 0) or 0) - prev["cached_input_tokens"]
                        do = (tt.get("output_tokens", 0) or 0) - prev["output_tokens"]
                        if di < 0 or do < 0:  # counter reset; rebaseline
                            di = dc = do = 0
                        dc = max(min(dc, di), 0)
                        key = normalize_model(model_now) or fallback
                        if key in rates:
                            ui, ci, oc = rates[key]
                            d_usd = (((di - dc) / 1e6) * ui + (dc / 1e6) * ci
                                     + (do / 1e6) * oc)
                            usd += d_usd
                            # Attribute this increment to when it happened, so
                            # a session straddling a month boundary charges each
                            # month only for the work done in it.
                            at = last_ts
                            if at is None or ((win_from is None or at >= win_from)
                                              and (win_to is None or at < win_to)):
                                usd_win += d_usd
                        else:
                            unpriced.add(key)
                        agg["unc"] += max(di - dc, 0)
                        agg["cache"] += dc
                        agg["out"] += do
                        prev = {
                            "input_tokens": tt.get("input_tokens", 0) or 0,
                            "cached_input_tokens": tt.get("cached_input_tokens", 0) or 0,
                            "output_tokens": tt.get("output_tokens", 0) or 0,
                        }
                    lt = info.get("last_token_usage")
                    if isinstance(lt, dict) and isinstance(lt.get("total_tokens"), int):
                        peak_ctx = max(peak_ctx, lt["total_tokens"])
                    continue

                if "compact" in ptype.lower():
                    n_compact += 1
                if ptype in ("function_call", "local_shell_call", "custom_tool_call",
                             "exec_command", "shell_call", "mcp_tool_call"):
                    n_tool += 1

                for blob in (rec, payload):
                    if isinstance(blob, dict) and blob.get("role") == "user":
                        txt = text_of(blob.get("content") or blob.get("message"))
                        if txt and not looks_injected(txt):
                            n_user += 1
                            prompts.append(txt)
                        break
    except OSError as exc:
        print(f"  skipped {path.name}: {exc}", file=sys.stderr)
        return None

    if not totals:
        return None

    head = (prompts[0].lower()[:400] if prompts else "")
    is_auto = any(m in head for m in AUTO_REVIEW_MARKERS)

    mins = int(active.total_seconds() // 60)
    span = int(((last_ts - first_ts).total_seconds() // 60)) if (first_ts and last_ts) else 0

    return {
        "path": path,
        "sid": (sid or "").lower(),
        "kind": "auto" if is_auto else "user",
        "started": first_ts,
        "active_min": mins,
        "span_min": span,
        "prompt": oneline(prompts[0]) if prompts else None,
        "last_prompt": oneline(prompts[-1]) if len(prompts) > 1 else None,
        "cwd": cwd,
        "repo": repo or (pathlib.PurePath(cwd).name if cwd else None),
        "branch": branch,
        "effort": effort,
        "models": models,
        "model": normalize_model(models[0]) if models else None,
        "multi": len({normalize_model(m) for m in models}) > 1,
        "turns": turns,
        "user_msgs": n_user,
        "tool_calls": n_tool,
        "compactions": n_compact,
        "peak_ctx": peak_ctx or None,
        "unc": agg["unc"],
        "cache": agg["cache"],
        "out": agg["out"],
        "total": totals.get("total_tokens", 0) or 0,
        "cache_pct": round(100 * agg["cache"] / (agg["unc"] + agg["cache"]), 1)
                     if (agg["unc"] + agg["cache"]) else None,
        "usd": usd,
        "usd_win": usd_win,
        "ended": last_ts,
        "straddles": abs(usd - usd_win) > 0.0005,
        "unpriced": unpriced,
    }


def file_date(p):
    m = TS_RE.search(p.name)
    if m:
        try:
            return dt.datetime.strptime(m.group(1), "%Y-%m-%dT%H-%M-%S")
        except ValueError:
            pass
    return dt.datetime.fromtimestamp(p.stat().st_mtime)


def dur(m):
    return f"{m//60}h{m%60:02d}m" if m >= 60 else f"{m}m"


def main():
    ap = argparse.ArgumentParser(description="Estimate Codex session cost from local logs.")
    ap.add_argument("--all", action="store_true",
                    help="every session on disk, ignoring the month window")
    ap.add_argument("--month", metavar="YYYY-MM",
                    help="one whole calendar month, e.g. 2026-08")
    ap.add_argument("--days", type=int, default=None,
                    help="look back this many days instead of the current month")
    ap.add_argument("--since", metavar="YYYY-MM-DD",
                    help="sessions with activity on or after this date")
    ap.add_argument("--start-only", action="store_true",
                    help="select by start date only, ignoring later activity")
    ap.add_argument("--session", metavar="PATH_OR_GLOB",
                    help="one rollout file, or a glob matched under --dir")
    ap.add_argument("--by-repo", action="store_true",
                    help="aggregate by repository instead of listing sessions")
    ap.add_argument("--no-auto", action="store_true", help="hide auto-review threads")
    ap.add_argument("--only-auto", action="store_true", help="show only auto-review threads")
    ap.add_argument("--grep", metavar="REGEX",
                    help="only sessions whose title or repo matches (case-insensitive)")
    ap.add_argument("--width", type=int, default=46,
                    help="width of the session/repo column (default 46)")
    ap.add_argument("--rates", metavar="JSON",
                    help='JSON file of {"model": [uncached, cached, output]} USD per 1M tokens')
    ap.add_argument("--model", default=DEFAULT_MODEL,
                    help=f"assumed model when none is recorded (default {DEFAULT_MODEL})")
    ap.add_argument("--max-mb", type=float, default=200.0,
                    help="skip rollout files larger than this (default 200)")
    ap.add_argument("--dir", default=str(SESSIONS_DIR),
                    help=f"sessions directory (default {SESSIONS_DIR})")
    ap.add_argument("--titles-debug", action="store_true",
                    help="report which store each title came from")
    args = ap.parse_args()

    rates = dict(RATES)
    if args.rates:
        with open(args.rates) as fh:
            rates.update({k: tuple(v) for k, v in json.load(fh).items()})

    root = pathlib.Path(args.dir).expanduser()
    if not root.is_dir():
        sys.exit(f"No sessions directory at {root}.")

    window = None
    cutoff = until = None
    if args.session:
        p = pathlib.Path(args.session).expanduser()
        files = [p] if p.is_file() else sorted(root.rglob(args.session))
    else:
        now = dt.datetime.now()
        cutoff = until = None
        if args.all:
            window = None  # no scope qualifier needed on the summary line
        elif args.month:
            try:
                start = dt.datetime.strptime(args.month, "%Y-%m")
            except ValueError:
                sys.exit("--month wants YYYY-MM, e.g. 2026-08")
            cutoff = start
            until = (start.replace(day=28) + dt.timedelta(days=4)).replace(day=1)
            window = start.strftime("%B %Y")
        elif args.since:
            cutoff = dt.datetime.strptime(args.since, "%Y-%m-%d")
            window = f"since {args.since}"
        elif args.days is not None:
            cutoff = now - dt.timedelta(days=args.days)
            window = f"last {args.days} days"
        else:
            # Default: the current billing month, since the org budget resets
            # monthly.
            cutoff = now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
            window = now.strftime("%B %Y") + " (month to date)"

        # Overlap selection: a session counts if any of its activity fell in the
        # window, not just its start. The filename gives the start and the file
        # mtime approximates last activity, both from stat() alone; the exact
        # bounds get rechecked after scanning.
        def overlaps(p):
            start = file_date(p)
            if until is not None and start >= until:
                return False
            if cutoff is None:
                return True
            if start >= cutoff:
                return True
            if args.start_only:
                return False
            try:
                return dt.datetime.fromtimestamp(p.stat().st_mtime) >= cutoff
            except OSError:
                return False

        files = sorted((p for p in root.rglob("rollout-*.jsonl") if overlaps(p)),
                       key=file_date)
    if not files:
        print(f"No sessions in range ({window or 'all sessions'}). "
              f"Use --all to see everything.")
        return

    titles = load_titles(debug=args.titles_debug)
    rows = []
    for p in files:
        if p.stat().st_size / 1e6 > args.max_mb:
            continue
        r = scan(p, rates, args.model, window=(cutoff, until))
        if not r:
            continue
        # mtime is only an approximation of last activity, so confirm against
        # the timestamps actually in the file.
        if not args.start_only and cutoff is not None and r["ended"] is not None:
            if r["ended"] < cutoff:
                continue
        if r["kind"] == "auto":
            # The harness prompt makes a useless label; name it for what it is.
            r["label"] = f"auto-review of {r['repo']}" if r["repo"] else "auto-review"
        else:
            r["label"] = (oneline(titles.get(r["sid"]), args.width)
                          or r["prompt"]
                          or (f"({r['repo']})" if r["repo"] else r["sid"][:12]))
        rows.append(r)

    auto_all = [r for r in rows if r["kind"] == "auto"]
    if args.no_auto:
        rows = [r for r in rows if r["kind"] == "user"]
    elif args.only_auto:
        rows = auto_all
    if args.grep:
        pat = re.compile(args.grep, re.I)
        rows = [r for r in rows if pat.search(r["label"] or "") or pat.search(r["repo"] or "")]
    if not rows:
        print("No matching sessions.")
        return

    w = args.width
    if args.by_repo:
        agg = {}
        for r in rows:
            a = agg.setdefault(r["repo"] or "(none)", {"n": 0, "usd": 0.0, "tok": 0, "auto": 0})
            a["n"] += 1
            a["usd"] += r["usd_win"]
            a["tok"] += r["total"]
            a["auto"] += r["kind"] == "auto"
        print(f"{'repo':<{w}} {'sess':>5} {'auto':>5} {'tokens':>8} {'est $':>8}")
        print("-" * (w + 30))
        for k, a in sorted(agg.items(), key=lambda kv: -kv[1]["usd"]):
            print(f"{k[:w]:<{w}} {a['n']:>5} {a['auto']:>5} "
                  f"{human(a['tok']):>8} {money(a['usd']):>8}")
    else:
        print(f"{'start':<12} {'last':<11} {'':<4} {'session':<{w}} {'model':<13} "
              f"{'act':>6} {'in':>6} {'cach':>6} {'out':>6} {'hit%':>5} {'est $':>8}")
        print("-" * (w + 82))
        for r in rows:
            tag = "AUTO" if r["kind"] == "auto" else ""
            mdl = (r["model"] or args.model).replace("gpt-", "")
            if r["multi"]:
                mdl += "+"
            elif not r["model"]:
                mdl += "*"
            start = r["started"].strftime("%m-%d %H:%M") if r["started"] else "?"
            # Drop the redundant date when the session finished the day it began.
            if not r["ended"]:
                last = "-"
            elif r["started"] and r["ended"].date() == r["started"].date():
                last = r["ended"].strftime("%H:%M")
            else:
                last = r["ended"].strftime("%m-%d %H:%M")
            print(f"{start:<12} {last:<11} "
                  f"{tag:<4} {(r['label'] or '')[:w]:<{w}} {mdl:<13} "
                  f"{dur(r['active_min']):>6} {human(r['unc']):>6} {human(r['cache']):>6} "
                  f"{human(r['out']):>6} "
                  f"{(str(r['cache_pct']) if r['cache_pct'] is not None else '-'):>5} "
                  f"{money(r['usd_win']) + ('~' if r['straddles'] else ''):>8}")

    print("-" * (w + (30 if args.by_repo else 82)))
    user_usd = sum(r["usd_win"] for r in rows if r["kind"] == "user")
    auto_usd = sum(r["usd_win"] for r in rows if r["kind"] == "auto")
    n_auto = sum(1 for r in rows if r["kind"] == "auto")
    scope = f" in {window}" if window else ""
    print(f"{len(rows)} sessions{scope}, estimated total ${user_usd + auto_usd:.2f}")
    if n_auto:
        pct = 100 * auto_usd / (user_usd + auto_usd) if (user_usd + auto_usd) else 0
        print(f"  of which {n_auto} auto-review thread(s): ${auto_usd:.2f} ({pct:.1f}%)")
    strad = [r for r in rows if r["straddles"]]
    if strad:
        full = sum(r["usd"] for r in strad)
        part = sum(r["usd_win"] for r in strad)
        print(f"~ {len(strad)} session(s) span the window edge; ${part:.2f} of their "
              f"${full:.2f} lifetime cost falls inside it")
    if any(r["multi"] for r in rows):
        print("+ session used more than one model; each turn priced at the model then active")
    if any(not r["model"] for r in rows):
        print(f"* model not recorded; priced as {args.model}")
    unp = set().union(*(r["unpriced"] for r in rows)) if rows else set()
    if unp:
        print(f"No rate for: {', '.join(sorted(unp))}. Add to RATES or use --rates.")
    print("Estimate only. Reconcile against platform.openai.com Usage and Costs.")


if __name__ == "__main__":
    main()
