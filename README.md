# RedditOSINT

Reddit OSINT by username. Give it a username and it returns everything the public Reddit
archives have indexed for that account: posts, comments, **what was deleted and what is still
live**, activity patterns, leaked identifiers, and an exposure score.

No dependencies, no API keys, no login. Just `python3`.

```bash
python3 reddit_osint.py someuser
```

---

## What it does and why it exists

Reddit deletes things, but third-party data archives keep the copy that existed *before* the
deletion (or at least the `[removed]` / `[deleted]` marker). This tool queries those archives and
reconstructs the activity profile of an account.

That is the premise behind the `h3kz-reddint` web tool, which is the reference for this script:
it reimplements that pipeline (two sources, merge, PII regexes, graph, score) as a standalone
CLI, with tests and optional proxy support.

### The sources

| Source | What it is | Role |
|---|---|---|
| **Arctic Shift** (`arctic-shift.photon-reddit.com`) | Free Reddit archive, community-run successor to Pushshift. Covers 2022→today and retains the original scrape (`selftext`, `removed_by_category`, `media_metadata`, flair). No API key, per-IP rate limit. | **Primary source.** Provides 100% of the useful data. |
| **PullPush** (`api.pullpush.io`) | Another index of the same Reddit data (historical Pushshift dumps). | Fallback. Queried in parallel and merged by `id`. Its subreddit API returns 404, so it only serves posts/comments. |
| `reddit.com` | — | **Never queried.** Only used to build permalink URLs in the report. The official Reddit API is not used (no login), which is why this works on suspended accounts. |

Measured on a real sample account: Arctic Shift returned 39 posts / 139 comments, PullPush
returned 38 posts with overlapping IDs. Arctic Shift wins; `--source arctic` is faster and
sufficient.

---

## Requirements

- Python 3.9+ (tested on 3.13)
- No `pip install`. Standard library only.
- For SOCKS proxies: `pip install pysocks` (optional, only needed for `socks5://`)

---

## Usage

### Basic

```bash
python3 reddit_osint.py someuser
```

Prints the report as JSON on `stdout` (summary only, without the per-item dump) and logs on
`stderr`.

### Saving the full report

```bash
python3 reddit_osint.py someuser --json report.json
python3 reddit_osint.py someuser --json report.json --quiet > /dev/null
```

`report.json` includes the `all_items` key with **every** post and comment (text, subreddit,
score, date, status, permalink). The on-screen summary omits it so it does not dump 200 KB.

### Filters

```bash
python3 reddit_osint.py someuser --subreddit dotnet       # one subreddit only
python3 reddit_osint.py someuser --keywords "kubernetes"  # posts mentioning X
python3 reddit_osint.py someuser --over18 true            # NSFW only
python3 reddit_osint.py someuser --source arctic          # single source (faster)
python3 reddit_osint.py someuser --max-pages 10           # cap pagination (100 items/page)
python3 reddit_osint.py someuser --delay 1.0             # slower, better with proxies
python3 reddit_osint.py someuser --rate-limit-retries 0   # fail fast when throttled
```

### Proxies

```bash
python3 reddit_osint.py someuser --proxies proxies.txt
```

`proxies.txt`, one proxy per line. Accepted formats:

```
http://1.2.3.4:8080
https://1.2.3.4:8443
socks5://1.2.3.4:1080
socks5://user:pass@1.2.3.4:1080
1.2.3.4:8080              # no scheme -> assumed http://
# comments and blank lines are ignored
```

Duplicates are removed. If the file contains no valid proxy, the script exits with code 2 and
makes no requests.

**Rotation.** Proxies are walked round-robin. When a request comes back with a blocking/rate-limit
code, that proxy is put on cooldown for `--cooldown` seconds and the script moves to the next one:

| Flag | Default | What it does |
|---|---|---|
| `--proxies FILE` | — | Proxy list. Without it: direct connection. |
| `--cooldown SECONDS` | `30` | How long a banned proxy stays sidelined. |
| `--max-attempts N` | `(proxies+1) × 3` | Attempt ceiling per request before giving up. |
| `--no-direct-fallback` | off | Wait instead of connecting directly when all proxies are cooling down. |
| `--shuffle-proxies` | off | Shuffle the list at startup (spreads load from the first request). |
| `--proxy-stats` | off | Adds `proxy_stats` to the report: `ok` / `banned` / `errors` per proxy. |
| `--rate-limit-retries N` | `2` | Retries of the **whole page** with exponential backoff (30 s, 60 s, capped at 120 s) when the API answers 429/422. |

Codes treated as a ban (they rotate the proxy): `401 403 407 422 429 500 502 503 504 520 521
522 523 524`, plus any `400`/`409` whose body contains `slow down` / `rate limit` /
`too many requests` / `timeout`. Arctic Shift uses **`422 "Timeout. Maybe slow down a bit"`** for
rate limiting, so it is deliberately handled as a ban.

A `404` does **not** rotate: that is a bad request, not a bad proxy, and retrying elsewhere will
not fix it.

**Backoff.** After a ban it waits `min(cooldown, 10 s)` — this applies to direct connections too,
which is where it hurts most. If a page exhausts its attempts, `fetch_all` retries the page with
exponential backoff (`--rate-limit-retries`, 2 attempts by default = 30 s and 60 s). That is what
stops the script from silently losing comments when it trips a rate limit mid-run.

Sample log with rotation:

```
[proxy] http://1.2.3.4:8080 blocked (429), backing off 10s -> rotating
[proxy] http://5.6.7.8:1080 failed (Connection refused), rotating
[proxy] direct connection blocked (422), backing off 10s -> rotating
[!] arctic/comments rate limited, backing off 30s (retry 1/2)
```

When every proxy is cooling down, the script waits out the longest remaining cooldown and retries
(or uses a direct connection unless `--no-direct-fallback` was passed).

**Warning:** a proxy is the egress path your IP takes to a third party. If you do not trust the
proxy vendor, do not use one with this tool.

### Environment variables

| Variable | Purpose |
|---|---|
| `REDDIT_OSINT_ARCTIC` | Override the Arctic Shift URL (mirrors, tests). |
| `REDDIT_OSINT_PULLPUSH` | Override the PullPush URL. |

---

## What information it extracts

### Raw fields from the APIs

`id`, `title`, `selftext`, `body`, `author`, `subreddit`, `created_utc`, `score`, `permalink`,
`url`, `over_18`, `num_comments`, `link_flair_text`, `preview.images`, `media_metadata`,
`removed_by_category`.

### Status classification

| Status | Detection |
|---|---|
| `removed` | Text = `[removed]` or `[ Removed by Reddit ]`, or `removed_by_category` present (posts) → **a moderator removed it**. |
| `deleted` | Text = `[deleted]` or `author == "[deleted]"` → **the author deleted it**. |
| `live` | Everything else. |

This is the whole point of the tool: the archive captures the *fact* of the removal even when it
has no content, and that alone is useful metadata.

### Profile and metrics

- **Totals** for posts, comments, items, plus a live/removed/deleted breakdown with `deleted_pct`.
- Real **first and last activity** (derived from items, not from archive metadata).
- **Archive metadata**: `total_karma`, `post_karma`, `comment_karma`, `num_posts`,
  `num_comments`, `earliest_post_at`, `last_comment_at` (from `/api/users/search`).

### Temporal patterns

- Histograms of **24 UTC hours**, **7 weekdays**, and a **monthly** series.
- **Estimated timezone**: finds the 6-hour consecutive window with the least activity (the
  *quiet window*, interpretable as local night) and maps it to a region:
  - start 05–09 UTC → `Americas (UTC-8..UTC-5)`
  - start ≥22 or ≤03 UTC → `Europe / Middle East / Africa (UTC+0..UTC+3)`
  - start 14–20 UTC → `Asia / Oceania (UTC+7..UTC+11)`
  - flat or empty distribution → `Unknown / distributed`

### Identifiers (regex over post/comment text)

| Type | Pattern |
|---|---|
| Email | `[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}` (rejects false `.png`/`.jpg` hits) |
| BTC | `bc1…` (bech32), `1…` / `3…` (legacy/base58), 26–59 chars |
| ETH | `0x` + 40 hex |
| XMR | `4` + 94 chars (95 total) |
| Telegram | `t.me/<user>`, `telegram: @<user>` |
| Phone | 10–15 digits with separators |
| Domains | every `https?://host` (filters out `reddit.com`, `redd.it`, `redditmedia.com`, `imgur.com`) |
| Mentions | `u/<username>` (the target account itself is excluded) |

These surface under `identifiers`, `external_domains`, and `mentioned_users`.

### Entities and exposure score

- **Technologies**: ~60 tracked terms (languages, frameworks, clouds, hardware, crypto) with
  occurrence counts.
- **`score_exposure_0_100`**: how likely the content is to identify the person behind the account.
  Weights emails (10), BTC (8), ETH/XMR (6), Telegram/phone (4), and domains/mentions (2 each),
  scaled over 30 and capped at 100. A profile with no PII exposure scores 0.
  Use it as triage: **a high score does not mean "pwned", it means "be careful with this text"**.

---

## How it works internally

```
collect()  -> fetch_all() for each (source, kind)
                 |
                 +-> build_search_url()      GET .../search?limit=100&sort=asc&author=U&after=<ts>
                 +-> PoolSession.get()      PoolSession -> ProxyPool.pick() -> urllib
                 |                             429/403 -> flag proxy, rotate, wait cooldown
                 +-> dedup by id             Set over rows from both sources
                 +-> cursor = min(created_utc) + 1   and page again
                        |
analyze()  ->  status, counters, histograms, regexes, score
```

Details that matter:

- **Cursor pagination, not offset**: `after = min(created_utc) + 1` from the previous page. Stable
  against new rows landing in the index while you page.
- **Failure-tolerant merge**: if one source is down or answers 403, the other carries the report.
  Errors are listed in `errors` instead of aborting the run, and `PoolSession.rate_limited` is
  left `True` so the pagination layer knows to retry with backoff.
- **`--max-pages`** prevents infinite paging if a proxy keeps returning the same page.
- **`sort=asc`** on every request so the cursor stays consistent.
- **Pause between pages** (`--delay`, 0.35 s by default) to stay under the rate limit.

---

## Tests

```bash
python3 -m unittest test_reddit_osint -v
python3 -m unittest test_reddit_osint.ProxyEndToEndTests   # a single class
```

74 tests, ~15 s, no network. They spin up a **fake origin** and **fake proxies** on local
`ThreadingHTTPServer` instances, so rotation is verified against real sockets rather than mocks.
All `sleep` calls are injected (`sleeper=`), which is why the suite does not take minutes.

### Picking the username

The suite ships with **no real usernames in it** — the offline tests use a synthetic
`OFFLINE_USERNAME = "target"` you are free to change at the top of the file.

```python
# test_reddit_osint.py
OFFLINE_USERNAME = "target"
```

`LiveArchiveTests` is the opt-in part: it hits the real archives and runs against **whatever
username you give it**, so nothing about anyone is baked into the repository. It is skipped
unless both environment variables are set:

```bash
REDDIT_OSINT_LIVE=1 REDDIT_OSINT_TEST_USER=someuser python3 -m unittest test_reddit_osint.LiveArchiveTests -v
```

| Variable | Meaning |
|---|---|
| `REDDIT_OSINT_LIVE` | `1` / `true` / `yes` enables the network tests. Anything else (or unset) skips them. |
| `REDDIT_OSINT_TEST_USER` | The Reddit username to run them against. |

The 4 live tests check that the archive contract still holds for that account: `/api/users/search`
returns the username you asked for with a `_meta` block, `collect()` produces no duplicate IDs,
every page is sorted by `created_utc`, every item lands in `live`/`removed`/`deleted`, and the
report's counters are self-consistent. They are deliberately account-agnostic: no fixture data and
no expected numbers, so they pass for any username and fail only if the pipeline or the archive
breaks.

| Suite | Covers |
|---|---|
| `ProxyParsingTests` | Scheme normalization, auth, dedupe, invalid lines, empty file → error |
| `ProxyPoolTests` | Round-robin, ban and exclusion, cooldown expiry, waiting when all are sidelined, counters |
| `ProxyEndToEndTests` | Request **actually goes through** the proxy, real rotation after 429, exhausting `max_attempts`, 404 does **not** rotate, dead proxy → recovery, 422 `slow down` rotates, backoff without proxies, error message flags `rate limited` |
| `UrlBuildingTests` | Exact Arctic Shift and PullPush parameters (`query` vs `body`, `after`, `over_18`, `subreddit`) |
| `FetchAllTests` | Pagination, cross-page dedup, `max_pages`, propagated filters, backoff retry after rate limit, retry exhaustion |
| `ClassificationTests` | `removed` / `deleted` / `live` in all their variants |
| `SignalTests` | Emails, BTC (bech32 and legacy), ETH, XMR, Telegram, mentions, domains, phones, false positives |
| `TimezoneTests` | The 3 regions, midnight wraparound, flat distribution, empty input |
| `AnalyzeTests` | Totals, ordering, URLs, top subs, identifiers, domains, score, empty input |
| `CollectTests` | Cross-source merge, dedup, single source, failure recorded without aborting |
| `CliIntegrationTests` | The CLI as a subprocess against stubs, with and without proxies, `--proxy-stats`, invalid file → exit 2 |
| `LiveArchiveTests` | Opt-in, network. Runs against `REDDIT_OSINT_TEST_USER`: metadata shape, no duplicate IDs, ordering, valid statuses, counter consistency |

---

## Report structure

```jsonc
{
  "username": "someuser",
  "archive_meta": { "total_karma": 1234, "num_posts": 480, "num_comments": 2100 },
  "totals": {
    "posts": 39, "comments": 139, "items": 178,
    "live": 164, "removed_by_mods": 14, "deleted_by_author": 0,
    "deleted_pct": 7.9, "items_last_12m": 20,
    "first_activity": "2021-03-04T11:22:33+00:00",
    "last_activity": "2025-09-22T09:29:20+00:00"
  },
  "score_exposure_0_100": 13,
  "timezone_guess": {
    "quiet_window_utc": "06:00-12:00",
    "estimated_region": "Americas (UTC-8..UTC-5)",
    "busiest_hour_utc": "03:00"
  },
  "activity_by_hour_utc": [0, 8, 10, 18, 4, ...],
  "activity_by_weekday": { "Mon": 33, "Tue": 14, "Wed": 23, ... },
  "activity_by_month": { "2025-01": 20, "2025-02": 7, ... },
  "top_subreddits": [{ "subreddit": "example_sub", "items": 20 }],
  "identifiers": ["email:me@example.com"],
  "external_domains": [{ "domain": "arxiv.org", "times": 5 }],
  "mentioned_users": [{ "user": "u/friend", "times": 3 }],
  "technologies": [{ "tech": "rtx", "times": 8 }],
  "deleted_removed_items": [
    { "id": "abc123", "kind": "posts", "status": "removed", "subreddit": "Frontend",
      "utc": "2025-08-09T05:40:48+00:00", "score": 1,
      "title": "Any suggestion for the frontend?", "permalink": "https://www.reddit.com/..." }
  ],
  "errors": [],
  "proxy_stats": { "http://1.2.3.4:8080": { "ok": 8, "banned": 2, "errors": 0 } },
  "all_items": [ /* only with --json */ ]
}
```

---

## Use as a library

```python
import reddit_osint as ro

session = ro.PoolSession(pool=ro.ProxyPool(ro.load_proxies("proxies.txt"), cooldown=45))
items, errors = ro.collect("someuser", ["arctic"], session=session, delay=1.0)
report = ro.analyze("someuser", items, meta=None)

print(report["score_exposure_0_100"])
print(report["deleted_removed_items"][:5])
print(session.attempts)          # [{'proxy': ..., 'outcome': 'banned'}, ...]
print(session.pool.stats())
```

The chain is: `load_proxies` → `ProxyPool` → `PoolSession` → `fetch_all` / `collect` → `analyze`.

---

## Limitations and caveats

- **Archive coverage, not Reddit coverage.** If the index does not have it, it will not show up.
  Pre-2022 data is spotty on PullPush; Arctic Shift starts in 2022.
- **`score_exposure_0_100` is a heuristic**, not a risk assessment. A 0 does not mean "anonymous",
  only that no PII was found in what is archived.
- **The timezone guess is a statistical inference.** A user with two or three items can land in
  any region; with little data, read `Unknown / distributed`.
- **Post content leaves your machine** toward Arctic Shift / PullPush (and toward the proxy if you
  use one). If you are analysing a sensitive account, prefer `--source arctic` without proxies.
- **An investigation tool, not a harassment tool.** Content deleted by its author is recoverable
  because the archive copied it before the deletion; using it to target someone is both a legal
  and a moral problem. Intended use: forensics, moderation, journalism, and authorized security
  work.

## Credits

- [Arctic Shift](https://github.com/ArthurHeitmann/arctic_shift) — the data archive.
- [PullPush](https://pullpush.io/) — historical index.
- [rosint.dev](https://rosint.dev) — the original open-source Reddit OSINT research.
