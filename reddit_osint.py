#!/usr/bin/env python3
import argparse
import json
import os
import random
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from collections import Counter
from datetime import datetime, timezone

ARCTIC = os.environ.get("REDDIT_OSINT_ARCTIC") or "https://arctic-shift.photon-reddit.com"
PULLPUSH = os.environ.get("REDDIT_OSINT_PULLPUSH") or "https://api.pullpush.io"
UA = {"User-Agent": "Mozilla/5.0 reddit_osint/1.0", "Accept": "application/json"}
DAY = ["Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun"]
BAN_CODES = {401, 403, 407, 422, 429, 500, 502, 503, 504, 520, 521, 522, 523, 524}
BAN_BODY_HINTS = ("slow down", "rate limit", "too many requests", "timeout")
MAX_BAN_SLEEP = 10.0

TECHS = """python javascript typescript java c++ c# go golang rust ruby php swift kotlin
react vue angular svelte nextjs node nodejs deno bun django flask fastapi spring
postgres postgresql mysql sqlite redis mongodb elasticsearch kafka docker kubernetes
aws gcp azure cloudflare nginx apache linux ubuntu debian fedora kali windows macos
android ios terraform ansible jenkins git github gitlab vim neovim vscode emacs
bitcoin ethereum solana monero blockchain web3 defi nvidia amd intel rtx ryzen
tailwind figma lambda dynamodb""".split()


class FetchError(RuntimeError):
    pass


def normalize_proxy(raw):
    raw = raw.strip()
    if not raw or raw.startswith("#"):
        return None
    if "://" not in raw:
        raw = "http://" + raw
    parsed = urllib.parse.urlsplit(raw)
    if not parsed.hostname or not parsed.port:
        return None
    scheme = parsed.scheme.lower()
    if scheme not in ("http", "https", "socks4", "socks5", "socks5h"):
        return None
    auth = ""
    if parsed.username:
        auth = parsed.username
        if parsed.password:
            auth += f":{parsed.password}"
        auth += "@"
    return f"{scheme}://{auth}{parsed.hostname}:{parsed.port}"


def load_proxies(path):
    if not path:
        return []
    out = []
    with open(path, encoding="utf-8") as fh:
        for line in fh:
            proxy = normalize_proxy(line)
            if proxy and proxy not in out:
                out.append(proxy)
    if not out:
        raise FetchError(f"no valid proxies found in {path}")
    return out


class ProxyPool:
    def __init__(self, proxies, cooldown=30.0, direct_fallback=True, shuffle=False, rng=None):
        self.proxies = list(proxies)
        self.cooldown = float(cooldown)
        self.direct_fallback = direct_fallback
        self.rng = rng or random.Random()
        self._order = list(range(len(self.proxies)))
        if shuffle:
            self.rng.shuffle(self._order)
        self._cursor = 0
        self._banned_until = {}
        self._stats = {p: {"ok": 0, "banned": 0, "errors": 0} for p in self.proxies}
        self._lock = threading.Lock()

    def __len__(self):
        return len(self.proxies)

    def available(self):
        now = time.monotonic()
        with self._lock:
            return [p for p in self.proxies if self._banned_until.get(p, 0.0) <= now]

    def _advance(self):
        if not self.proxies:
            return None
        with self._lock:
            idx = self._order[self._cursor % len(self._order)]
            self._cursor += 1
            candidate = self.proxies[idx]
            if self._banned_until.get(candidate, 0.0) > time.monotonic():
                for p in self.proxies:
                    if self._banned_until.get(p, 0.0) <= time.monotonic():
                        return p
                return None
            return candidate

    def pick(self, sleeper=time.sleep):
        while True:
            proxy = self._advance()
            if proxy is not None:
                return proxy
            if not self.proxies:
                if self.direct_fallback:
                    return None
                raise FetchError("no proxy available and direct fallback disabled")
            with self._lock:
                wait = min(self._banned_until.get(p, 0.0) for p in self.proxies)
                wait = max(0.0, wait - time.monotonic())
            if wait > 0:
                if wait >= 0.5:
                    print(f"[proxy] all proxies cooling down, waiting {wait:.1f}s",
                          file=sys.stderr)
                sleeper(min(wait, self.cooldown))
            elif not self.direct_fallback:
                raise FetchError("all proxies are rate limited")

    def report(self, proxy, outcome):
        if proxy is None:
            return
        with self._lock:
            stat = self._stats.setdefault(proxy, {"ok": 0, "banned": 0, "errors": 0})
            if outcome == "ok":
                stat["ok"] += 1
            elif outcome == "banned":
                stat["banned"] += 1
                self._banned_until[proxy] = time.monotonic() + self.cooldown
            else:
                stat["errors"] += 1

    def stats(self):
        with self._lock:
            return {p: dict(v) for p, v in self._stats.items()}


class PoolSession:
    def __init__(self, pool=None, timeout=20, max_attempts=None, retries=2,
                 headers=None, verbose=True, sleeper=time.sleep):
        self.pool = pool or ProxyPool([], direct_fallback=True)
        self.timeout = timeout
        self.retries = retries
        self.headers = dict(UA)
        if headers:
            self.headers.update(headers)
        default_attempts = max(3, (len(self.pool) + 1) * (retries + 1))
        self.max_attempts = max_attempts or default_attempts
        self.verbose = verbose
        self.sleeper = sleeper
        self.attempts = []
        self.last_error = None
        self.rate_limited = False

    def _open(self, url, proxy):
        handlers = [urllib.request.HTTPSHandler()]
        if proxy:
            handlers.append(urllib.request.ProxyHandler({"http": proxy, "https": proxy}))
        else:
            handlers.append(urllib.request.ProxyHandler({}))
        opener = urllib.request.build_opener(*handlers)
        return opener.open(urllib.request.Request(url, headers=self.headers),
                           timeout=self.timeout)

    def get(self, url):
        errors = []
        transient = 0
        for _ in range(self.max_attempts):
            proxy = self.pool.pick(self.sleeper)
            try:
                with self._open(url, proxy) as resp:
                    payload = json.loads(resp.read().decode("utf-8", "replace"))
                self.pool.report(proxy, "ok")
                self.attempts.append({"proxy": proxy or "DIRECT", "outcome": "ok"})
                return payload
            except urllib.error.HTTPError as exc:
                body = exc.read().decode("utf-8", "replace")[:180]
                msg = f"HTTP {exc.code}: {body}"
                self.last_error = msg
                banned = exc.code in BAN_CODES or (
                    exc.code in (400, 409)
                    and any(h in body.lower() for h in BAN_BODY_HINTS))
                if banned:
                    self.pool.report(proxy, "banned")
                    self.attempts.append({"proxy": proxy or "DIRECT", "outcome": "banned"})
                    errors.append(msg)
                    self.rate_limited = True
                    nap = min(self.pool.cooldown, MAX_BAN_SLEEP)
                    if self.verbose:
                        where = proxy or "direct connection"
                        print(f"[proxy] {where} blocked ({exc.code}), backing off "
                              f"{nap:.0f}s -> rotating", file=sys.stderr)
                    self.sleeper(nap)
                    continue
                self.pool.report(proxy, "errors")
                self.attempts.append({"proxy": proxy or "DIRECT", "outcome": "error"})
                errors.append(msg)
                break
            except (urllib.error.URLError, OSError, ValueError) as exc:
                reason = getattr(exc, "reason", exc)
                msg = f"{type(exc).__name__}: {reason}"
                self.last_error = msg
                self.attempts.append({"proxy": proxy or "DIRECT", "outcome": "error"})
                transient += 1
                if transient > self.retries:
                    self.pool.report(proxy, "errors")
                    errors.append(msg)
                    break
                errors.append(msg)
                self.pool.report(proxy, "errors")
                if self.verbose and proxy:
                    print(f"[proxy] {proxy} failed ({reason}), rotating", file=sys.stderr)
                self.sleeper(1.0)
        raise FetchError(f"{url} -> {' | '.join(dict.fromkeys(errors)) or 'unknown error'}"
                         + (" (rate limited)" if self.rate_limited else ""))


def http_get(url, session=None, **kwargs):
    return (session or PoolSession(**kwargs)).get(url)


def build_search_url(source, kind, user=None, limit=100, cursor=None, subreddit=None,
                     keywords=None, over18=None, sort="asc"):
    if not user and not keywords:
        raise ValueError("build_search_url needs a user, keywords, or both")
    params = {"limit": limit, "sort": sort}
    if user:
        params["author"] = user
    if source == "arctic":
        if subreddit:
            params["subreddit"] = subreddit
        if over18 is not None:
            params["over_18"] = "true" if over18 else "false"
        if keywords:
            params["query" if kind == "posts" else "body"] = keywords
        base = f"{ARCTIC}/api/{kind}/search"
    else:
        if subreddit:
            params["subreddit"] = subreddit
        if keywords:
            params["q"] = keywords
        base = f"{PULLPUSH}/reddit/search/{'submission' if kind == 'posts' else 'comment'}/"
    if cursor:
        params["after"] = cursor
    sep = "&" if "?" in base else "?"
    return base + sep + urllib.parse.urlencode(params)


def fetch_all(user, kind, source, session=None, limit=100, max_pages=40,
              subreddit=None, keywords=None, over18=None, delay=0.35, verbose=True,
              rate_limit_retries=2, max_backoff=120.0, sleeper=time.sleep):
    out, seen, cursor, pages, rl_retries = [], set(), None, 0, 0
    while pages < max_pages:
        url = build_search_url(source, kind, user, limit, cursor, subreddit,
                               keywords, over18)
        try:
            data = http_get(url, session=session).get("data") or []
        except FetchError:
            if getattr(session, "rate_limited", False) and rl_retries < rate_limit_retries:
                rl_retries += 1
                nap = min(delay + 30.0 * rl_retries, max_backoff)
                if verbose:
                    print(f"[!] {source}/{kind} rate limited, backing off "
                          f"{nap:.0f}s (retry {rl_retries}/{rate_limit_retries})",
                          file=sys.stderr)
                sleeper(nap)
                if session is not None:
                    session.rate_limited = False
                continue
            raise
        rl_retries = 0
        if not data:
            break
        fresh = [d for d in data if d.get("id") and d["id"] not in seen]
        seen.update(d["id"] for d in fresh)
        out.extend(fresh)
        pages += 1
        if verbose:
            print(f"  [{source}/{kind}] page {pages}: +{len(fresh)} (total {len(out)})",
                  file=sys.stderr)
        if len(data) < limit:
            break
        oldest = int(min((d.get("created_utc") or 0) for d in data))
        nxt = oldest + 1
        if cursor and nxt <= cursor:
            break
        cursor = nxt
        if delay:
            time.sleep(delay)
    return out


def status_of(item, kind):
    text = item.get("selftext") if kind == "posts" else item.get("body")
    author = item.get("author")
    if text in ("[removed]", "[ Removed by Reddit ]") or \
            (kind == "posts" and item.get("removed_by_category")):
        return "removed"
    if text == "[deleted]" or author == "[deleted]":
        return "deleted"
    return "live"


def text_of(item, kind):
    if kind == "posts":
        return f"{item.get('title') or ''} {item.get('selftext') or ''}".strip()
    return (item.get("body") or "").strip()


def summarize_item(item, kind, text_limit=600):
    return {
        "id": item.get("id"),
        "kind": kind,
        "status": status_of(item, kind),
        "subreddit": item.get("subreddit"),
        "score": item.get("score"),
        "created_utc": item.get("created_utc"),
        "utc": datetime.fromtimestamp(item["created_utc"], timezone.utc).isoformat()
               if item.get("created_utc") else None,
        "title": item.get("title"),
        "text": text_of(item, kind)[:text_limit],
        "text_truncated": len(text_of(item, kind)) > text_limit,
        "url": "https://www.reddit.com" + item["permalink"] if item.get("permalink")
               else item.get("url"),
    }


def search_archive(keywords=None, kind="posts", source="arctic", subreddit=None,
                   author=None, limit=50, before=None, session=None, text_limit=600,
                   sort="desc"):
    url = build_search_url(source, kind, user=author, limit=limit, cursor=before,
                           subreddit=subreddit, keywords=keywords, sort=sort)
    rows = http_get(url, session=session).get("data") or []
    reverse = sort != "asc"
    rows.sort(key=lambda r: r.get("created_utc") or 0, reverse=reverse)
    return [summarize_item(r, kind, text_limit) for r in rows[:limit]]


def scrape_signals(text):
    out = []
    for m in re.finditer(r"\b(bc1[a-zA-HJ-NP-Z0-9]{25,59}|[13][a-km-zA-HJ-NP-Z1-9]{25,34})\b",
                         text):
        value = m.group(1)
        if not re.fullmatch(r"[a-z]+", value) and len(value) > 25:
            out.append(("btc", value))
    for m in re.finditer(r"\b(0x[a-fA-F0-9]{40})\b", text):
        out.append(("eth", m.group(1)))
    for m in re.finditer(r"\b(4[0-9AB][1-9A-HJ-NP-Za-km-z]{93})\b", text):
        out.append(("xmr", m.group(1)))
    for m in re.finditer(r"\b([a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,})\b", text):
        value = m.group(1).lower()
        if not value.endswith((".png", ".jpg", ".jpeg", ".gif", ".webp")):
            out.append(("email", value))
    for m in re.finditer(r"(?:t\.me/|telegram:\s*@)([a-zA-Z0-9_]{5,32})\b", text, re.I):
        out.append(("telegram", "@" + m.group(1)))
    for m in re.finditer(r"(?:^|[\s/])u/([a-zA-Z0-9_-]{3,30})", text):
        out.append(("mention", "u/" + m.group(1)))
    for m in re.finditer(r"https?://([^\s<>\"')]+)", text):
        host = m.group(1).split("/")[0].lower().removeprefix("www.")
        out.append(("domain", host))
    for m in re.finditer(r"\+?\d[\d\s().-]{8,17}\d", text):
        raw = re.sub(r"[^\d+]", "", m.group(0))
        if 10 <= len(raw.lstrip("+")) <= 15:
            out.append(("phone", raw))
    return out


def guess_timezone(hours):
    scores = [sum(hours[(start + i) % 24] for i in range(6)) for start in range(24)]
    best = min(range(24), key=lambda s: scores[s])
    if not sum(hours) or max(scores) - min(scores) <= 1:
        return best, "Unknown / distributed"
    if 5 <= best <= 9:
        return best, "Americas (UTC-8..UTC-5)"
    if best >= 22 or best <= 3:
        return best, "Europe / Middle East / Africa (UTC+0..UTC+3)"
    if 14 <= best <= 20:
        return best, "Asia / Oceania (UTC+7..UTC+11)"
    return best, "Unknown / distributed"


def analyze(user, items_by_kind, meta=None):
    posts = items_by_kind.get("posts") or []
    comments = items_by_kind.get("comments") or []
    everything = [("posts", p) for p in posts] + [("comments", c) for c in comments]
    everything.sort(key=lambda kv: kv[1].get("created_utc") or 0)

    subs, hours, weekdays = Counter(), [0] * 24, [0] * 7
    statuses, signals = Counter(), Counter()
    domains, mentions, techs, per_month = Counter(), Counter(), Counter(), Counter()
    flagged = []

    for kind, item in everything:
        text = text_of(item, kind)
        sub = (item.get("subreddit") or "").lower()
        if sub:
            subs[sub] += 1
        ts = item.get("created_utc")
        if ts:
            stamp = datetime.fromtimestamp(ts, timezone.utc)
            hours[stamp.hour] += 1
            weekdays[stamp.weekday()] += 1
            per_month[stamp.strftime("%Y-%m")] += 1
        status = status_of(item, kind)
        statuses[status] += 1
        if status != "live":
            flagged.append({
                "id": item.get("id"), "kind": kind, "status": status,
                "subreddit": item.get("subreddit"),
                "utc": datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None,
                "score": item.get("score"),
                "title": (item.get("title") or "")[:120],
                "permalink": "https://www.reddit.com" + item["permalink"]
                             if item.get("permalink") else item.get("url"),
            })
        low = text.lower()
        for tech in TECHS:
            if re.search(rf"\b{re.escape(tech)}\b", low):
                techs[tech] += 1
        for tag, value in scrape_signals(text):
            if tag == "domain":
                if value.endswith(("reddit.com", "redd.it", "redditmedia.com", "imgur.com")):
                    continue
                domains[value] += 1
            elif tag == "mention":
                if value.lower() == f"u/{user}".lower():
                    continue
                mentions[value] += 1
            elif tag == "phone":
                signals["phone"] += 1
            else:
                signals[f"{tag}:{value}"] += 1

    tz_start, tz_name = guess_timezone(hours)
    total = len(everything)
    cutoff = time.time() - 365 * 24 * 3600
    recent = sum(1 for _, i in everything if (i.get("created_utc") or 0) > cutoff)
    exposure = min(100, round(
        (len([s for s in signals if s.startswith("email")]) * 10
         + len([s for s in signals if s.startswith("btc")]) * 8
         + len([s for s in signals if s.startswith("eth")]) * 6
         + len([s for s in signals if s.startswith("xmr")]) * 6
         + len([s for s in signals if s.startswith("telegram")]) * 4
         + signals.get("phone", 0) * 4
         + len(domains) * 2 + len(mentions) * 2) / 30 * 100))

    def iso(ts):
        return datetime.fromtimestamp(ts, timezone.utc).isoformat() if ts else None

    return {
        "username": user,
        "archive_meta": meta,
        "totals": {
            "posts": len(posts), "comments": len(comments), "items": total,
            "live": statuses["live"], "removed_by_mods": statuses["removed"],
            "deleted_by_author": statuses["deleted"],
            "deleted_pct": round((total - statuses["live"]) / total * 100, 1) if total else 0.0,
            "items_last_12m": recent,
            "first_activity": iso(everything[0][1].get("created_utc")) if everything else None,
            "last_activity": iso(everything[-1][1].get("created_utc")) if everything else None,
        },
        "score_exposure_0_100": exposure,
        "timezone_guess": {
            "quiet_window_utc": f"{tz_start:02d}:00-{(tz_start + 6) % 24:02d}:00",
            "estimated_region": tz_name,
            "busiest_hour_utc": f"{hours.index(max(hours)):02d}:00" if total else None,
        },
        "activity_by_hour_utc": hours,
        "activity_by_weekday": {DAY[i]: weekdays[i] for i in range(7)},
        "activity_by_month": dict(sorted(per_month.items())),
        "top_subreddits": [{"subreddit": s, "items": c} for s, c in subs.most_common(20)],
        "identifiers": sorted(signals),
        "external_domains": [{"domain": d, "times": c} for d, c in domains.most_common(30)],
        "mentioned_users": [{"user": m, "times": c} for m, c in mentions.most_common(30)],
        "technologies": [{"tech": t, "times": c} for t, c in techs.most_common(30)],
        "deleted_removed_items": sorted(flagged, key=lambda d: d["utc"] or "", reverse=True),
        "all_items": [summarize_item(i, k, 2000) for k, i in everything],
    }


def collect(user, sources, session=None, subreddit=None, keywords=None,
            over18=None, max_pages=40, delay=0.35, verbose=True,
            rate_limit_retries=2, sleeper=time.sleep):
    merged = {"posts": {}, "comments": {}}
    errors = []
    for source in sources:
        for kind in ("posts", "comments"):
            try:
                rows = fetch_all(user, kind, source, session=session,
                                 subreddit=subreddit, keywords=keywords, over18=over18,
                                 max_pages=max_pages, delay=delay, verbose=verbose,
                                 rate_limit_retries=rate_limit_retries, sleeper=sleeper)
            except FetchError as exc:
                errors.append(f"{source}/{kind}: {exc}")
                if verbose:
                    print(f"[!] {source}/{kind}: {exc}", file=sys.stderr)
                continue
            for row in rows:
                merged[kind].setdefault(row["id"], row)
    items = {kind: sorted(rows.values(), key=lambda x: x.get("created_utc") or 0)
             for kind, rows in merged.items()}
    return items, errors


def main():
    ap = argparse.ArgumentParser(
        description="Reddit OSINT by username, backed by Arctic Shift and PullPush archives")
    ap.add_argument("username", help="Reddit username, without the u/ prefix")
    ap.add_argument("--subreddit", help="only return items from this subreddit")
    ap.add_argument("--keywords", help="only return items whose text matches this term")
    ap.add_argument("--over18", choices=["true", "false"], help="filter by NSFW flag")
    ap.add_argument("--source", choices=["arctic", "pullpush", "both"], default="both",
                    help="which archive(s) to query (default: both)")
    ap.add_argument("--max-pages", type=int, default=40,
                    help="pagination cap at 100 items per page (default: 40)")
    ap.add_argument("--delay", type=float, default=0.35,
                    help="pause between pages in seconds (default: 0.35)")
    ap.add_argument("--rate-limit-retries", type=int, default=2,
                    help="page retries with backoff when the API answers 429/422 (default: 2)")
    ap.add_argument("--timeout", type=float, default=20, help="per-request HTTP timeout (s)")
    ap.add_argument("--json", metavar="FILE", help="write the full report to this JSON file")
    ap.add_argument("--quiet", action="store_true", help="suppress stderr logs")

    proxy = ap.add_argument_group("proxies")
    proxy.add_argument("--proxies", metavar="FILE",
                       help="file with one proxy per line (http/https/socks5://host:port)")
    proxy.add_argument("--cooldown", type=float, default=30.0,
                       help="seconds a banned proxy stays sidelined (default: 30)")
    proxy.add_argument("--max-attempts", type=int, default=None,
                       help="attempt ceiling per request (default: (proxies+1)*3)")
    proxy.add_argument("--no-direct-fallback", action="store_true",
                       help="never fall back to a direct connection when all proxies are cooling down")
    proxy.add_argument("--shuffle-proxies", action="store_true",
                       help="shuffle the proxy list at startup")
    proxy.add_argument("--proxy-stats", action="store_true",
                       help="include per-proxy usage statistics in the report")

    args = ap.parse_args()
    verbose = not args.quiet
    user = args.username.strip().removeprefix("u/").removeprefix("U/")
    sources = ["arctic", "pullpush"] if args.source == "both" else [args.source]
    over18 = None if args.over18 is None else args.over18 == "true"

    try:
        pool = ProxyPool(load_proxies(args.proxies), cooldown=args.cooldown,
                         direct_fallback=not args.no_direct_fallback,
                         shuffle=args.shuffle_proxies)
    except (FetchError, OSError) as exc:
        print(f"[!] {exc}", file=sys.stderr)
        return 2

    session = PoolSession(pool=pool, timeout=args.timeout,
                          max_attempts=args.max_attempts, verbose=verbose)

    if verbose:
        print(f"[*] u/{user} | sources: {', '.join(sources)} | "
              f"proxies: {len(pool) or 'direct'}", file=sys.stderr)

    meta = None
    try:
        url = f"{ARCTIC}/api/users/search?author={urllib.parse.quote(user)}&limit=1"
        meta = (http_get(url, session=session).get("data") or [{}])[0].get("_meta")
    except FetchError as exc:
        if verbose:
            print(f"[!] profile metadata unavailable: {exc}", file=sys.stderr)

    items, errors = collect(user, sources, session=session, subreddit=args.subreddit,
                            keywords=args.keywords, over18=over18,
                            max_pages=args.max_pages, delay=args.delay, verbose=verbose,
                            rate_limit_retries=args.rate_limit_retries)

    report = analyze(user, items, meta)
    report["sources_merged"] = sources
    report["errors"] = errors
    if args.proxy_stats:
        report["proxy_stats"] = pool.stats()

    if args.json:
        out = args.json
        if not os.path.isabs(out) and os.path.dirname(out) == "":
            out = os.path.abspath(out)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump(report, fh, ensure_ascii=False, indent=2)
        if verbose:
            print(f"[*] full JSON report -> {out}", file=sys.stderr)

    print(json.dumps({k: v for k, v in report.items() if k != "all_items"},
                     ensure_ascii=False, indent=2))
    return 0


if __name__ == "__main__":
    sys.exit(main())