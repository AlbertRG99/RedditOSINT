#!/usr/bin/env python3
"""MCP server exposing the RedditOSINT archive tools to LLM clients.

Run it with any of:

    python mcp_server.py                        # stdio, the usual local setup
    fastmcp run mcp_server.py:mcp               # same, via the FastMCP CLI
    python mcp_server.py --http                 # streamable HTTP on :8000

Configure the client with, for example:

    {
      "mcpServers": {
        "reddit-osint": {
          "command": "python",
          "args": ["/absolute/path/to/RedditOSINT/mcp_server.py"],
          "env": { "REDDIT_OSINT_SOURCE": "arctic" }
        }
      }
    }
"""

import argparse
import asyncio
import os
import sys
import time

from fastmcp import FastMCP

import reddit_osint as ro

VERSION = "1.1.0"

INSTRUCTIONS = """
Reddit archive OSINT. Query public Reddit archives (Arctic Shift, PullPush) by username to
recover deleted and removed content, reconstruct activity patterns, and extract leaked
identifiers.

How to use these tools efficiently:
  * Start with get_profile_overview. It is cheap and tells you the account size, activity
    window, exposure score and whether anything was deleted at all.
  * Only escalate to the heavier aggregate tools (get_removed_content, find_identifiers,
    get_activity_patterns, get_affiliations) when the overview says there is something to
    find. They all reuse one cached fetch per username, so calling several is cheap.
  * Use list_posts / list_comments for reading content. They do a single targeted request
    instead of paginating the whole account.
  * search_archive searches the whole archive by phrase, optionally scoped to one author.
    Use it for "who talked about X", not for profiling a specific user.

Every tool returns a dict with a "notes" field explaining caveats. Read it before drawing
conclusions: the timezone and exposure score are heuristics, and archive coverage is not the
same as Reddit coverage.
""".strip()

mcp = FastMCP(
    "reddit-osint",
    instructions=INSTRUCTIONS,
    version=VERSION,
)

DEFAULT_SOURCE = os.environ.get("REDDIT_OSINT_SOURCE", "arctic").strip().lower()
CACHE_TTL = float(os.environ.get("REDDIT_OSINT_CACHE_TTL", "600"))
MAX_PAGES = int(os.environ.get("REDDIT_OSINT_MAX_PAGES", "10"))
TEXT_LIMIT = int(os.environ.get("REDDIT_OSINT_TEXT_LIMIT", "600"))
HARD_LIMIT = 100

_session = None
_cache = {}


def get_session():
    global _session
    if _session is None:
        pool = ro.ProxyPool([], direct_fallback=True)
        path = os.environ.get("REDDIT_OSINT_PROXIES", "").strip()
        if path:
            pool = ro.ProxyPool(
                ro.load_proxies(path),
                cooldown=float(os.environ.get("REDDIT_OSINT_COOLDOWN", "30")),
                direct_fallback=os.environ.get("REDDIT_OSINT_NO_DIRECT", "") == "",
            )
        _session = ro.PoolSession(pool=pool, verbose=False)
    return _session


def normalize_username(username):
    name = (username or "").strip().lstrip("@").removeprefix("u/").removeprefix("U/")
    if not name:
        raise ValueError("username is required")
    return name


def resolve_sources(source):
    if source in (None, "", "default"):
        source = DEFAULT_SOURCE
    if source == "both":
        return ["arctic", "pullpush"]
    if source not in ("arctic", "pullpush"):
        raise ValueError("source must be one of: arctic, pullpush, both")
    return [source]


def cached_report(username, sources, subreddit=None, keywords=None, max_pages=None):
    key = (username.lower(), tuple(sources), subreddit, keywords, max_pages or MAX_PAGES)
    now = time.monotonic()
    hit = _cache.get(key)
    if hit and hit[0] > now:
        return hit[1], True

    session = get_session()
    meta = None
    try:
        url = f"{ro.ARCTIC}/api/users/search?author={username}&limit=1"
        meta = (ro.http_get(url, session=session).get("data") or [{}])[0].get("_meta")
    except ro.FetchError:
        meta = None

    items, errors = ro.collect(
        username, sources, session=session, subreddit=subreddit, keywords=keywords,
        max_pages=max_pages or MAX_PAGES, delay=0.4, verbose=False,
    )
    report = ro.analyze(username, items, meta)
    report["errors"] = errors
    report["sources_queried"] = list(sources)
    report["text_truncated_at"] = TEXT_LIMIT

    if len(_cache) > 32:
        _cache.clear()
    _cache[key] = (now + CACHE_TTL, report)
    return report, False


def build_notes(report, extra=""):
    notes = [
        "Archive data, not live Reddit: absence of an item means the archive lacks it, "
        "not that it never existed.",
        f"Items analysed: {report['totals']['items']} "
        f"({report['totals']['posts']} posts, {report['totals']['comments']} comments).",
    ]
    if report.get("errors"):
        notes.append(f"Partial data: {len(report['errors'])} source(s) failed, "
                     f"see the errors field. {report['errors'][0][:160]}")
    if report["totals"]["items"] < 10:
        notes.append("Very small sample: activity and timezone inferences are unreliable.")
    if extra:
        notes.append(extra)
    return notes


def sort_items(items, order):
    reverse = order != "oldest"
    return sorted(items, key=lambda i: i.get("created_utc") or 0, reverse=reverse)


def as_list(value):
    if not value:
        return []
    return [v.strip() for v in value.split(",") if v.strip()]


@mcp.resource("reddit://guide")
def guide() -> str:
    """How to read these tools, and what the heuristics do and do not mean."""
    return f"""RedditOSINT MCP server v{VERSION}

SOURCES
  Arctic Shift  {ro.ARCTIC}
      Free Reddit archive, community-run successor to Pushshift. 2022 onwards, keeps the
      original scrape including removed_by_category. Primary source.
  PullPush      {ro.PULLPUSH}
      Historical index of the same data. Secondary; its subreddit API is unavailable, so it
      only serves posts and comments.
  reddit.com is never queried. The official Reddit API is not used, so suspended accounts
  still resolve.

DELETION SEMANTICS
  removed   a moderator removed it. Detected by text == "[removed]" / "[ Removed by Reddit ]"
            or a non-null removed_by_category on a post. The archive kept the marker, not
            the content.
  deleted   the author deleted it. Detected by text == "[deleted]" or author == "[deleted]".
  live      still present in the archive.
  In both removed and deleted cases the original text is usually gone. You get the metadata,
  the subreddit, the timestamp and the score. Do not invent the missing content.

TIMEZONE HEURISTIC
  Finds the 6 consecutive UTC hours with the lowest activity (assumed to be local night) and
  maps the start hour to a region: 05-09 -> Americas, 22-03 -> EMEA, 14-20 -> Asia/Oceania,
  flat distribution -> Unknown. It is a guess, not evidence. Corroborate with content
  (holiday posts, work hours, local references) before treating it as a finding.

EXPOSURE SCORE (0-100)
  Weighted count of PII in the archived text: email 10, BTC 8, ETH/XMR 6, Telegram or phone 4,
  external domain or u/ mention 2 each, divided by 30, capped at 100.
  0 means no PII was found in what is archived. It does NOT mean the person is anonymous.

CONTEXT BUDGET
  list_posts and list_comments truncate text at {TEXT_LIMIT} chars per item and cap at
  {HARD_LIMIT} items per call. Aggregate tools return at most 30 entries per list. Paginate
  with the returned created_utc values and the "before" argument rather than asking for more.

ENVIRONMENT
  REDDIT_OSINT_SOURCE      arctic | pullpush | both   (default arctic)
  REDDIT_OSINT_PROXIES     path to a proxy list file
  REDDIT_OSINT_COOLDOWN    ban cooldown seconds        (default 30)
  REDDIT_OSINT_NO_DIRECT   set to 1 to forbid direct connections
  REDDIT_OSINT_CACHE_TTL   report cache seconds        (default 600)
  REDDIT_OSINT_MAX_PAGES   pagination cap per account  (default 10)
  REDDIT_OSINT_TEXT_LIMIT  text truncation per item    (default 600)
  REDDIT_OSINT_ARCTIC      override Arctic Shift base URL
  REDDIT_OSINT_PULLPUSH    override PullPush base URL

ETHICAL AND LEGAL
  Deleted content is recoverable because the archive copied it before deletion. This server is
  for forensics, moderation, journalism and authorized security work. Using recovered content
  to harass or dox someone is both a legal and a moral problem, and is out of scope.
"""


@mcp.tool
async def get_profile_overview(username: str, source: str = "default") -> dict:
    """Cheap first call: account size, karma, activity window, timezone guess, exposure score.

    Use this before anything else. If total items is 0 the account is not in the archive and
    the other tools will return nothing useful.
    """
    name = normalize_username(username)
    sources = resolve_sources(source)
    report, cached = await asyncio.to_thread(cached_report, name, sources)

    return {
        "username": report["username"],
        "in_archive": report["totals"]["items"] > 0,
        "totals": report["totals"],
        "archive_meta": report["archive_meta"],
        "timezone_guess": report["timezone_guess"],
        "exposure_score_0_100": report["score_exposure_0_100"],
        "top_subreddits": report["top_subreddits"][:10],
        "deleted_or_removed": report["totals"]["removed_by_mods"]
                              + report["totals"]["deleted_by_author"],
        "sources_queried": report["sources_queried"],
        "cached": cached,
        "notes": build_notes(report, "Counts include items the archive holds but Reddit no "
                                     "longer serves."),
    }


@mcp.tool
async def list_posts(username: str, limit: int = 25, subreddit: str = "",
                     keywords: str = "", status: str = "all", order: str = "newest",
                     text_limit: int = 0, source: str = "default") -> dict:
    """List a user's posts, newest first. One targeted request, no full pagination.

    status: all | live | removed | deleted. order: newest | oldest.
    """
    name = normalize_username(username)
    sources = resolve_sources(source)
    limit = max(1, min(int(limit or 25), HARD_LIMIT))
    text_limit = int(text_limit) or TEXT_LIMIT
    if order not in ("newest", "oldest"):
        raise ValueError("order must be newest or oldest")

    items = []
    errors = []
    for src in sources:
        try:
            rows = await asyncio.to_thread(
                ro.search_archive, keywords.strip() or None, "posts", src,
                subreddit.strip() or None, name, limit, None, get_session(), text_limit,
                "desc" if order == "newest" else "asc",
            )
        except ro.FetchError as exc:
            errors.append(f"{src}/posts: {exc}"[:200])
            continue
        items.extend(rows)

    seen, unique = set(), []
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    unique = sort_items(unique, order)
    if status != "all":
        if status not in ("live", "removed", "deleted"):
            raise ValueError("status must be all, live, removed or deleted")
        unique = [i for i in unique if i["status"] == status]

    return {
        "username": name,
        "returned": len(unique[:limit]),
        "posts": unique[:limit],
        "errors": errors,
        "notes": [
            f"Text truncated at {text_limit} chars per post.",
            "Removed and deleted posts keep their title, subreddit, score and timestamp; "
            "the body is normally gone.",
        ],
    }


@mcp.tool
async def list_comments(username: str, limit: int = 25, subreddit: str = "",
                        keywords: str = "", status: str = "all", order: str = "newest",
                        text_limit: int = 0, source: str = "default") -> dict:
    """List a user's comments, newest first. One targeted request, no full pagination."""
    name = normalize_username(username)
    sources = resolve_sources(source)
    limit = max(1, min(int(limit or 25), HARD_LIMIT))
    text_limit = int(text_limit) or TEXT_LIMIT
    if order not in ("newest", "oldest"):
        raise ValueError("order must be newest or oldest")

    items, errors = [], []
    for src in sources:
        try:
            rows = await asyncio.to_thread(
                ro.search_archive, keywords.strip() or None, "comments", src,
                subreddit.strip() or None, name, limit, None, get_session(), text_limit,
                "desc" if order == "newest" else "asc",
            )
        except ro.FetchError as exc:
            errors.append(f"{src}/comments: {exc}"[:200])
            continue
        items.extend(rows)

    seen, unique = set(), []
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)
    unique = sort_items(unique, order)
    if status != "all":
        if status not in ("live", "removed", "deleted"):
            raise ValueError("status must be all, live, removed or deleted")
        unique = [i for i in unique if i["status"] == status]

    return {
        "username": name,
        "returned": len(unique[:limit]),
        "comments": unique[:limit],
        "errors": errors,
        "notes": [
            f"Text truncated at {text_limit} chars per comment.",
            "Reddit shows [removed] when a moderator removed a comment and [deleted] when "
            "the author did; the archive keeps that distinction.",
        ],
    }


@mcp.tool
async def get_removed_content(username: str, status: str = "all", limit: int = 50,
                              source: str = "default") -> dict:
    """Everything the archive holds for this user that Reddit no longer shows.

    status: all | removed (moderator) | deleted (author). Paginated internally, so this
    covers the whole account rather than the first page.
    """
    name = normalize_username(username)
    sources = resolve_sources(source)
    limit = max(1, min(int(limit or 50), 200))
    if status not in ("all", "removed", "deleted"):
        raise ValueError("status must be all, removed or deleted")

    report, cached = await asyncio.to_thread(cached_report, name, sources)
    flagged = report["deleted_removed_items"]
    if status != "all":
        flagged = [f for f in flagged if f["status"] == status]

    return {
        "username": name,
        "counts": {
            "removed_by_mods": report["totals"]["removed_by_mods"],
            "deleted_by_author": report["totals"]["deleted_by_author"],
            "deleted_pct": report["totals"]["deleted_pct"],
        },
        "total_flagged": len(flagged),
        "returned": min(len(flagged), limit),
        "items": [
            {
                "id": f["id"], "kind": f["kind"], "status": f["status"],
                "subreddit": f["subreddit"], "utc": f["utc"], "score": f["score"],
                "title": f["title"],
                "has_text": f["status"] == "live",
                "permalink": f["permalink"],
            }
            for f in flagged[:limit]
        ],
        "cached": cached,
        "notes": build_notes(report, [
            "The archive usually holds the marker, not the body: the content itself is "
            "typically unrecoverable. Report the metadata, do not speculate about the text.",
        ]),
    }


@mcp.tool
async def find_identifiers(username: str, kinds: str = "", limit: int = 30,
                           source: str = "default") -> dict:
    """PII and handles leaked in this user's archived text.

    kinds is a comma separated filter: email, btc, eth, xmr, telegram, phone. Empty means
    every kind. Also returns external domains and mentioned users.
    """
    name = normalize_username(username)
    sources = resolve_sources(source)
    limit = max(1, min(int(limit or 30), 200))
    wanted = {k.lower() for k in as_list(kinds)}

    report, cached = await asyncio.to_thread(cached_report, name, sources)
    ids = report["identifiers"]
    if wanted:
        ids = [i for i in ids if i.split(":", 1)[0] in wanted]

    grouped = {}
    for entry in ids:
        kind, _, value = entry.partition(":")
        grouped.setdefault(kind, []).append(value)

    return {
        "username": name,
        "exposure_score_0_100": report["score_exposure_0_100"],
        "identifiers": {k: v[:limit] for k, v in grouped.items()},
        "identifier_count": {k: len(v) for k, v in grouped.items()},
        "external_domains": report["external_domains"][:limit],
        "mentioned_users": report["mentioned_users"][:limit],
        "cached": cached,
        "notes": build_notes(report, [
            "Regex hits can be false positives: a bare 10-15 digit number may be an order "
            "id, not a phone number. Verify before reporting anything as PII.",
            "These are values the user posted publicly. Handle them accordingly.",
        ]),
    }


@mcp.tool
async def get_activity_patterns(username: str, source: str = "default") -> dict:
    """Temporal fingerprint: 24h histogram, weekdays, monthly series, timezone guess."""
    name = normalize_username(username)
    sources = resolve_sources(source)
    report, cached = await asyncio.to_thread(cached_report, name, sources)

    hours = report["activity_by_hour_utc"]
    total = sum(hours) or 1
    return {
        "username": name,
        "timezone_guess": report["timezone_guess"],
        "activity_by_hour_utc": hours,
        "hour_share_pct": [round(h * 100 / total, 1) for h in hours],
        "activity_by_weekday": report["activity_by_weekday"],
        "activity_by_month": report["activity_by_month"],
        "first_activity": report["totals"]["first_activity"],
        "last_activity": report["totals"]["last_activity"],
        "items_last_12m": report["totals"]["items_last_12m"],
        "cached": cached,
        "notes": build_notes(report, [
            "The timezone is derived from the quietest 6 hour UTC block, which is a "
            "heuristic. Corroborate with post content before treating it as a finding.",
            "hour_share_pct sums to 100 and is easier to reason about than raw counts.",
        ]),
    }


@mcp.tool
async def get_affiliations(username: str, limit: int = 20,
                           source: str = "default") -> dict:
    """Communities and interests: top subreddits, detected technologies, mentioned users."""
    name = normalize_username(username)
    sources = resolve_sources(source)
    limit = max(1, min(int(limit or 20), 100))

    report, cached = await asyncio.to_thread(cached_report, name, sources)
    return {
        "username": name,
        "top_subreddits": report["top_subreddits"][:limit],
        "technologies": report["technologies"][:limit],
        "mentioned_users": report["mentioned_users"][:limit],
        "external_domains": report["external_domains"][:limit],
        "cached": cached,
        "notes": build_notes(report, [
            "Technologies come from a fixed keyword list matched against post text, so a "
            "mention in passing counts the same as a project. Not evidence of expertise.",
        ]),
    }


@mcp.tool
async def search_archive(keywords: str, kind: str = "posts", author: str = "",
                         subreddit: str = "", limit: int = 25, source: str = "default") -> dict:
    """Search the whole archive by phrase, optionally scoped to one author or subreddit.

    This is the tool for "who talked about X". For profiling a known user prefer
    get_profile_overview plus list_posts / list_comments.
    """
    term = (keywords or "").strip()
    if not term:
        raise ValueError("keywords is required")
    if kind not in ("posts", "comments"):
        raise ValueError("kind must be posts or comments")
    sources = resolve_sources(source)
    limit = max(1, min(int(limit or 25), HARD_LIMIT))
    author_name = normalize_username(author) if author.strip() else None

    items, errors = [], []
    for src in sources:
        try:
            rows = await asyncio.to_thread(
                ro.search_archive, term, kind, src, subreddit.strip() or None,
                author_name, limit, None, get_session(), TEXT_LIMIT, "desc",
            )
        except ro.FetchError as exc:
            errors.append(f"{src}/{kind}: {exc}"[:200])
            continue
        items.extend(rows)

    seen, unique = set(), []
    for item in items:
        if item["id"] not in seen:
            seen.add(item["id"])
            unique.append(item)

    return {
        "keywords": term,
        "kind": kind,
        "author": author_name,
        "returned": len(unique[:limit]),
        "results": sort_items(unique, "newest")[:limit],
        "errors": errors,
        "notes": [
            f"Text truncated at {TEXT_LIMIT} chars. Results are newest first.",
            "Archive-wide search only sees what the archives indexed. Absence of results "
            "does not prove absence of posts.",
        ],
    }


def main():
    ap = argparse.ArgumentParser(description="RedditOSINT MCP server")
    ap.add_argument("--http", action="store_true",
                    help="serve over streamable HTTP instead of stdio")
    ap.add_argument("--host", default="127.0.0.1")
    ap.add_argument("--port", type=int, default=8000)
    args = ap.parse_args()

    if args.http:
        mcp.run(transport="http", host=args.host, port=args.port)
    else:
        mcp.run()


if __name__ == "__main__":
    main()