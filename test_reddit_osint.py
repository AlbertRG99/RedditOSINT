#!/usr/bin/env python3
import json
import os
import socket
import sys
import threading
import unittest
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

import reddit_osint as ro

UTC = 1700000000

OFFLINE_USERNAME = "target"

LIVE_USERNAME = os.environ.get("REDDIT_OSINT_TEST_USER", "").strip()
RUN_LIVE = os.environ.get("REDDIT_OSINT_LIVE", "").strip().lower() in ("1", "true", "yes")


class FakeAPI:
    def __init__(self):
        self.routes = {}
        self.hits = []

    def add(self, path, handler):
        self.routes[path] = handler

    def dispatch(self, target):
        parts = urllib.parse.urlsplit(target)
        self.hits.append(target)
        for route, handler in self.routes.items():
            if parts.path.startswith(route):
                return handler(parts.path, urllib.parse.parse_qs(parts.query))
        return 404, {"error": "not found"}


class OriginHandler(BaseHTTPRequestHandler):
    api = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        status, payload = self.api.dispatch(self.path)
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


class ProxyHandler(BaseHTTPRequestHandler):
    log = None
    origin = None

    def log_message(self, *args):
        pass

    def do_GET(self):
        type(self).log.append(self.path)
        status, payload = self.origin.dispatch(self.path)
        body = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


def start_server(handler_cls, attr=None, value=None):
    cls = type(f"{handler_cls.__name__}_bound", (handler_cls,), {attr: value} if attr else {})
    srv = ThreadingHTTPServer(("127.0.0.1", 0), cls)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    return srv, f"http://127.0.0.1:{srv.server_address[1]}"


def post(pid, ts, sub="test", selftext="hello", **extra):
    base = {"id": pid, "author": OFFLINE_USERNAME, "subreddit": sub, "created_utc": ts,
            "title": f"title {pid}", "selftext": selftext, "score": 1,
            "permalink": f"/r/{sub}/comments/{pid}/x/"}
    base.update(extra)
    return base


def comment(cid, ts, sub="test", body="hi", **extra):
    base = {"id": cid, "author": OFFLINE_USERNAME, "subreddit": sub, "created_utc": ts,
            "body": body, "score": 2, "permalink": f"/r/{sub}/comments/abc/{cid}/x/"}
    base.update(extra)
    return base


class ProxyParsingTests(unittest.TestCase):
    def test_normalize_adds_scheme(self):
        self.assertEqual(ro.normalize_proxy("1.2.3.4:8080"), "http://1.2.3.4:8080")

    def test_normalize_keeps_scheme_and_auth(self):
        self.assertEqual(ro.normalize_proxy("socks5://user:pw@1.2.3.4:1080"),
                         "socks5://user:pw@1.2.3.4:1080")

    def test_normalize_rejects_garbage(self):
        for bad in ["", "   ", "# comment", "http://", "ftp://1.2.3.4:21", "not a proxy"]:
            self.assertIsNone(ro.normalize_proxy(bad), bad)

    def test_load_proxies_dedupes_and_skips_comments(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("# pool\n1.2.3.4:8080\n1.2.3.4:8080\n\nsocks5://5.6.7.8:1080\nbad\n")
            path = fh.name
        proxies = ro.load_proxies(path)
        self.assertEqual(proxies, ["http://1.2.3.4:8080", "socks5://5.6.7.8:1080"])

    def test_load_proxies_empty_file_raises(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("# nothing here\n")
            path = fh.name
        with self.assertRaises(ro.FetchError):
            ro.load_proxies(path)


class ProxyPoolTests(unittest.TestCase):
    def test_rotation_is_round_robin(self):
        pool = ro.ProxyPool(["http://a:1", "http://b:2", "http://c:3"], cooldown=0)
        picks = [pool.pick(sleeper=lambda _: None) for _ in range(6)]
        self.assertEqual(picks, ["http://a:1", "http://b:2", "http://c:3"] * 2)

    def test_ban_marks_and_excludes(self):
        pool = ro.ProxyPool(["http://a:1", "http://b:2"], cooldown=60)
        pool.report("http://a:1", "banned")
        self.assertEqual(pool.available(), ["http://b:2"])
        self.assertNotIn("http://a:1", [pool.pick(sleeper=lambda _: None) for _ in range(3)])

    def test_cooldown_expires(self):
        pool = ro.ProxyPool(["http://a:1"], cooldown=0.01)
        pool.report("http://a:1", "banned")
        self.assertEqual(pool.available(), [])
        import time
        time.sleep(0.02)
        self.assertEqual(pool.available(), ["http://a:1"])

    def test_all_banned_waits_then_recovers(self):
        pool = ro.ProxyPool(["http://a:1"], cooldown=0.01)
        pool.report("http://a:1", "banned")
        slept = []
        self.assertEqual(pool.pick(sleeper=slept.append), "http://a:1")
        self.assertTrue(slept)

    def test_direct_when_no_proxies(self):
        self.assertIsNone(ro.ProxyPool([]).pick(sleeper=lambda _: None))

    def test_stats_counted(self):
        pool = ro.ProxyPool(["http://a:1"], cooldown=0)
        pool.report("http://a:1", "ok")
        pool.report("http://a:1", "banned")
        pool.report("http://a:1", "errors")
        self.assertEqual(pool.stats()["http://a:1"],
                         {"ok": 1, "banned": 1, "errors": 1})


class ProxyEndToEndTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.srv, self.origin_url = start_server(OriginHandler, "api", self.api)
        self.api.add("/ok", lambda p, q: (200, {"data": []}))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def _proxies(self, count=1):
        ProxyHandler.log = []
        ProxyHandler.origin = self.api
        urls = []
        for _ in range(count):
            srv, url = start_server(ProxyHandler)
            self.addCleanup(srv.server_close)
            self.addCleanup(srv.shutdown)
            urls.append(url)
        return urls

    def test_request_goes_through_proxy(self):
        proxies = self._proxies()
        pool = ro.ProxyPool(proxies, cooldown=0)
        session = ro.PoolSession(pool=pool, verbose=False)
        self.assertEqual(session.get(self.origin_url + "/ok"), {"data": []})
        self.assertEqual(len(ProxyHandler.log), 1)
        self.assertTrue(ProxyHandler.log[0].startswith(self.origin_url))

    def test_rotates_on_429_and_succeeds(self):
        api = self.api
        calls = {"n": 0}

        def flaky(path, query):
            calls["n"] += 1
            if calls["n"] == 1:
                return 429, {"error": "rate limited"}
            return 200, {"data": [{"ok": True}]}

        api.add("/flaky", flaky)
        proxies = self._proxies(2)
        pool = ro.ProxyPool(proxies, cooldown=0)
        session = ro.PoolSession(pool=pool, verbose=False, sleeper=lambda _: None)
        result = session.get(self.origin_url + "/flaky")
        self.assertEqual(result, {"data": [{"ok": True}]})
        self.assertEqual([a["outcome"] for a in session.attempts],
                         ["banned", "ok"])
        self.assertNotEqual(session.attempts[0]["proxy"], session.attempts[1]["proxy"])
        self.assertEqual(pool.stats()[session.attempts[0]["proxy"]]["banned"], 1)

    def test_gives_up_after_max_attempts(self):
        self.api.add("/blocked", lambda p, q: (403, {"error": "forbidden"}))
        proxies = self._proxies(2)
        pool = ro.ProxyPool(proxies, cooldown=0)
        session = ro.PoolSession(pool=pool, verbose=False, max_attempts=3,
                                 sleeper=lambda _: None)
        with self.assertRaises(ro.FetchError):
            session.get(self.origin_url + "/blocked")
        self.assertEqual(len(session.attempts), 3)

    def test_404_does_not_rotate(self):
        self.api.add("/missing", lambda p, q: (404, {"error": "nope"}))
        proxies = self._proxies(2)
        pool = ro.ProxyPool(proxies, cooldown=0)
        session = ro.PoolSession(pool=pool, verbose=False, sleeper=lambda _: None)
        with self.assertRaises(ro.FetchError):
            session.get(self.origin_url + "/missing")
        self.assertEqual([a["outcome"] for a in session.attempts], ["error"])

    def test_dead_proxy_recovers_to_working_one(self):
        dead_port = socket.socket()
        dead_port.bind(("127.0.0.1", 0))
        dead_url = f"http://127.0.0.1:{dead_port.getsockname()[1]}"
        dead_port.close()
        proxies = self._proxies(1)
        pool = ro.ProxyPool([dead_url, proxies[0]], cooldown=0)
        session = ro.PoolSession(pool=pool, verbose=False, retries=1,
                                 sleeper=lambda _: None)
        self.assertEqual(session.get(self.origin_url + "/ok"), {"data": []})
        self.assertEqual(session.attempts[-1]["outcome"], "ok")

    def test_422_slow_down_rotates(self):
        self.api.add("/soft", lambda p, q: (422, {"error": "Timeout. Maybe slow down"}))
        proxies = self._proxies(2)
        pool = ro.ProxyPool(proxies, cooldown=0)
        session = ro.PoolSession(pool=pool, verbose=False, max_attempts=1,
                                 sleeper=lambda _: None)
        with self.assertRaises(ro.FetchError):
            session.get(self.origin_url + "/soft")
        self.assertEqual(session.attempts[0]["outcome"], "banned")
        self.assertTrue(session.rate_limited)

    def test_429_backs_off_even_without_proxies(self):
        self.api.add("/limited", lambda p, q: (429, {"error": "Rate limit exceeded"}))
        pool = ro.ProxyPool([], direct_fallback=True, cooldown=7)
        slept = []
        session = ro.PoolSession(pool=pool, verbose=False, max_attempts=2,
                                 sleeper=slept.append)
        with self.assertRaises(ro.FetchError):
            session.get(self.origin_url + "/limited")
        self.assertEqual(len(slept), 2)
        self.assertTrue(all(0 < s <= ro.MAX_BAN_SLEEP for s in slept))
        self.assertEqual([a["proxy"] for a in session.attempts], ["DIRECT", "DIRECT"])

    def test_error_message_flags_rate_limit(self):
        self.api.add("/limited2", lambda p, q: (429, {"error": "slow down"}))
        session = ro.PoolSession(pool=ro.ProxyPool([]), verbose=False, max_attempts=1,
                                 sleeper=lambda _: None)
        with self.assertRaises(ro.FetchError) as ctx:
            session.get(self.origin_url + "/limited2")
        self.assertIn("rate limited", str(ctx.exception))


class UrlBuildingTests(unittest.TestCase):
    def test_arctic_posts(self):
        url = ro.build_search_url("arctic", "posts", OFFLINE_USERNAME, limit=100, cursor=500,
                                  subreddit="dotnet", keywords="aws", over18=False)
        self.assertTrue(url.startswith(f"{ro.ARCTIC}/api/posts/search?"))
        q = urllib.parse.parse_qs(urllib.parse.urlsplit(url).query)
        self.assertEqual(q["author"], [OFFLINE_USERNAME])
        self.assertEqual(q["limit"], ["100"])
        self.assertEqual(q["sort"], ["asc"])
        self.assertEqual(q["subreddit"], ["dotnet"])
        self.assertEqual(q["query"], ["aws"])
        self.assertEqual(q["over_18"], ["false"])
        self.assertEqual(q["after"], ["500"])

    def test_arctic_comments_uses_body_key(self):
        url = ro.build_search_url("arctic", "comments", OFFLINE_USERNAME, keywords="aws")
        self.assertIn("/api/comments/search?", url)
        self.assertIn("body=aws", url)
        self.assertNotIn("query=", url)

    def test_pullpush_paths(self):
        self.assertIn("/reddit/search/submission/?", ro.build_search_url("pullpush", "posts", OFFLINE_USERNAME))
        self.assertIn("/reddit/search/comment/?", ro.build_search_url("pullpush", "comments", OFFLINE_USERNAME))

    def test_no_cursor_no_after(self):
        self.assertNotIn("after=", ro.build_search_url("arctic", "posts", OFFLINE_USERNAME))


class FetchAllTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.srv, self.url = start_server(OriginHandler, "api", self.api)
        self._old = ro.ARCTIC

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        ro.ARCTIC = self._old

    def test_pagination_and_dedup(self):
        ro.ARCTIC = self.url

        def page(path, query):
            after = int(query.get("after", [0])[0])
            if after == 0:
                return 200, {"data": [post(f"p{i}", 1000 + i) for i in range(3)]}
            if after == 1001:
                return 200, {"data": [post("p0", 1000), post("p3", 1003)]}
            return 200, {"data": []}

        self.api.add("/api/posts/search", page)
        rows = ro.fetch_all(OFFLINE_USERNAME, "posts", "arctic", session=ro.PoolSession(verbose=False, sleeper=lambda _: None),
                            limit=3, delay=0, verbose=False, rate_limit_retries=0)
        self.assertEqual([r["id"] for r in rows], ["p0", "p1", "p2", "p3"])
        self.assertEqual(len(self.api.hits), 2)
        self.assertIn("after=1001", self.api.hits[1])

    def test_max_pages_respected(self):
        ro.ARCTIC = self.url

        def page(path, query):
            after = int(query.get("after", [0])[0])
            return 200, {"data": [post(f"p{after + i}", 1000 + after + i) for i in range(2)]}

        self.api.add("/api/posts/search", page)
        rows = ro.fetch_all(OFFLINE_USERNAME, "posts", "arctic", session=ro.PoolSession(verbose=False, sleeper=lambda _: None),
                            limit=2, max_pages=3, delay=0, verbose=False, rate_limit_retries=0)
        self.assertEqual(len(rows), 6)
        self.assertEqual(len(self.api.hits), 3)

    def test_subreddit_filter_passed(self):
        ro.ARCTIC = self.url
        self.api.add("/api/posts/search", lambda p, q: (200, {"data": []}))
        ro.fetch_all(OFFLINE_USERNAME, "posts", "arctic", session=ro.PoolSession(verbose=False, sleeper=lambda _: None),
                     subreddit="dotnet", delay=0, verbose=False, rate_limit_retries=0)
        self.assertIn("subreddit=dotnet", self.api.hits[0])

    def test_rate_limit_is_retried_with_backoff(self):
        ro.ARCTIC = self.url
        calls = {"n": 0}

        def page(path, query):
            calls["n"] += 1
            if calls["n"] == 1:
                return 429, {"error": "Rate limit exceeded"}
            return 200, {"data": [post("a", 1000)]}

        self.api.add("/api/posts/search", page)
        session = ro.PoolSession(pool=ro.ProxyPool([]), verbose=False,
                                 max_attempts=1, sleeper=lambda _: None)
        slept = []
        rows = ro.fetch_all(OFFLINE_USERNAME, "posts", "arctic", session=session, limit=100,
                            delay=0, verbose=False, sleeper=slept.append)
        self.assertEqual([r["id"] for r in rows], ["a"])
        self.assertEqual(calls["n"], 2)
        self.assertEqual(len(slept), 1)

    def test_rate_limit_gives_up_after_retries(self):
        ro.ARCTIC = self.url
        self.api.add("/api/posts/search", lambda p, q: (429, {"error": "slow down"}))
        session = ro.PoolSession(pool=ro.ProxyPool([]), verbose=False,
                                 max_attempts=1, sleeper=lambda _: None)
        with self.assertRaises(ro.FetchError):
            ro.fetch_all(OFFLINE_USERNAME, "posts", "arctic", session=session, delay=0,
                         verbose=False, rate_limit_retries=1,
                         sleeper=lambda _: None)
        self.assertEqual(len(self.api.hits), 2)


class ClassificationTests(unittest.TestCase):
    def test_removed_by_category(self):
        self.assertEqual(ro.status_of(post("a", 1, selftext="[removed]"), "posts"), "removed")

    def test_automod_flag(self):
        item = post("a", 1, selftext="real text", removed_by_category="automod_filtered")
        self.assertEqual(ro.status_of(item, "posts"), "removed")

    def test_reddit_removal_notice(self):
        self.assertEqual(ro.status_of(post("a", 1, selftext="[ Removed by Reddit ]"),
                                      "posts"), "removed")

    def test_deleted_by_author(self):
        self.assertEqual(ro.status_of(comment("a", 1, body="[deleted]"), "comments"),
                         "deleted")

    def test_deleted_author_marker(self):
        self.assertEqual(ro.status_of(comment("a", 1, author="[deleted]"), "comments"),
                         "deleted")

    def test_live(self):
        self.assertEqual(ro.status_of(post("a", 1), "posts"), "live")
        self.assertEqual(ro.status_of(comment("a", 1), "comments"), "live")

    def test_text_extraction(self):
        self.assertEqual(ro.text_of(post("a", 1, title="T", selftext="S"), "posts"), "T S")
        self.assertEqual(ro.text_of(comment("a", 1, body="B"), "comments"), "B")


class SignalTests(unittest.TestCase):
    def kinds(self, text):
        return [t for t, _ in ro.scrape_signals(text)]

    def values(self, text, tag):
        return [v for t, v in ro.scrape_signals(text) if t == tag]

    def test_email(self):
        self.assertEqual(self.values("write me at Foo.Bar+x@example.co.uk", "email"),
                         ["foo.bar+x@example.co.uk"])

    def test_image_extensions_not_emails(self):
        self.assertEqual(self.values("shot.png and pic.jpg", "email"), [])

    def test_btc(self):
        addr = "1A1zP1eP5QGefi2DMPTfTL5SLmv7DivfNa"
        self.assertEqual(self.values(f"send to {addr}", "btc"), [addr])
        bech = "bc1qar0srrr7xfkvy5l643lydnw9re59gtzzwf5mdq"
        self.assertEqual(self.values(bech, "btc"), [bech])

    def test_eth(self):
        addr = "0x" + "a1b2c3d4" * 5
        self.assertEqual(self.values(addr, "eth"), [addr])

    def test_xmr(self):
        addr = "4" + "A" * 94
        self.assertEqual(self.values(addr, "xmr"), [addr])

    def test_telegram(self):
        self.assertEqual(self.values("t.me/mychannel", "telegram"), ["@mychannel"])
        self.assertEqual(self.values("telegram: @other", "telegram"), ["@other"])

    def test_mentions(self):
        self.assertEqual(self.values("cc u/someone and r/x u/another", "mention"),
                         ["u/someone", "u/another"])

    def test_domains(self):
        self.assertEqual(self.values("see https://www.Arxiv.org/abs/1 and http://x.io", "domain"),
                         ["arxiv.org", "x.io"])

    def test_phone(self):
        self.assertEqual(self.values("call +34 600 11 22 33", "phone"), ["+34600112233"])

    def test_clean_text_no_signals(self):
        self.assertEqual(ro.scrape_signals("just a normal comment about python"), [])


class TimezoneTests(unittest.TestCase):
    def hours_with_gap(self, gap_start, gap_len=6):
        hours = [1] * 24
        for i in range(gap_len):
            hours[(gap_start + i) % 24] = 0
        return hours

    def test_americas(self):
        start, region = ro.guess_timezone(self.hours_with_gap(6))
        self.assertEqual(start, 6)
        self.assertEqual(region, "Americas (UTC-8..UTC-5)")

    def test_emea(self):
        start, region = ro.guess_timezone(self.hours_with_gap(22))
        self.assertEqual(start, 22)
        self.assertEqual(region, "Europe / Middle East / Africa (UTC+0..UTC+3)")

    def test_emea_wraps_midnight(self):
        start, region = ro.guess_timezone(self.hours_with_gap(0))
        self.assertEqual(start, 0)
        self.assertEqual(region, "Europe / Middle East / Africa (UTC+0..UTC+3)")

    def test_asia(self):
        start, region = ro.guess_timezone(self.hours_with_gap(14))
        self.assertEqual(start, 14)
        self.assertEqual(region, "Asia / Oceania (UTC+7..UTC+11)")

    def test_empty_is_unknown(self):
        start, region = ro.guess_timezone([0] * 24)
        self.assertEqual(region, "Unknown / distributed")
        self.assertEqual(start, 0)

    def test_flat_activity_is_unknown(self):
        _, region = ro.guess_timezone([5] * 24)
        self.assertEqual(region, "Unknown / distributed")


class AnalyzeTests(unittest.TestCase):
    def build(self):
        posts = [
            post("p1", UTC, sub="dotnet", selftext="i code in python daily"),
            post("p2", UTC + 60, sub="dotnet", selftext="[removed]",
                 removed_by_category="automod_filtered"),
            post("p3", UTC + 120, sub="machinelearning", selftext="mail me at me@example.com"),
        ]
        comments = [
            comment("c1", UTC + 180, sub="dotnet", body="see https://arxiv.org/abs/1"),
            comment("c2", UTC + 240, sub="dotnet", body="[deleted]", author="[deleted]"),
            comment("c3", UTC + 300, sub="smallbusiness", body="cc u/friend and t.me/channel"),
        ]
        return {"posts": posts, "comments": comments}

    def test_totals(self):
        r = ro.analyze(OFFLINE_USERNAME, self.build())
        self.assertEqual(r["totals"]["posts"], 3)
        self.assertEqual(r["totals"]["comments"], 3)
        self.assertEqual(r["totals"]["items"], 6)
        self.assertEqual(r["totals"]["live"], 4)
        self.assertEqual(r["totals"]["removed_by_mods"], 1)
        self.assertEqual(r["totals"]["deleted_by_author"], 1)
        self.assertEqual(r["totals"]["deleted_pct"], 33.3)

    def test_sorted_and_urls(self):
        r = ro.analyze(OFFLINE_USERNAME, self.build())
        stamps = [i["utc"] for i in r["all_items"]]
        self.assertEqual(stamps, sorted(stamps))
        self.assertTrue(r["all_items"][0]["url"].startswith("https://www.reddit.com/r/"))

    def test_top_subreddits(self):
        r = ro.analyze(OFFLINE_USERNAME, self.build())
        self.assertEqual(r["top_subreddits"][0], {"subreddit": "dotnet", "items": 4})

    def test_identifiers(self):
        r = ro.analyze(OFFLINE_USERNAME, self.build())
        self.assertIn("email:me@example.com", r["identifiers"])
        self.assertIn("telegram:@channel", r["identifiers"])
        self.assertNotIn("domain:arxiv.org", r["identifiers"])

    def test_domains_and_mentions(self):
        r = ro.analyze(OFFLINE_USERNAME, self.build())
        self.assertIn({"domain": "arxiv.org", "times": 1}, r["external_domains"])
        self.assertIn({"user": "u/friend", "times": 1}, r["mentioned_users"])

    def test_technologies(self):
        r = ro.analyze(OFFLINE_USERNAME, self.build())
        self.assertIn({"tech": "python", "times": 1}, r["technologies"])

    def test_reddit_domains_filtered(self):
        r = ro.analyze(OFFLINE_USERNAME, {"posts": [post("p", UTC, selftext="x https://i.redd.it/a.png")],
                                  "comments": []})
        self.assertEqual(r["external_domains"], [])

    def test_self_mention_excluded(self):
        r = ro.analyze(OFFLINE_USERNAME, {"posts": [], "comments": [comment("c", UTC, body="u/target")]})
        self.assertEqual(r["mentioned_users"], [])

    def test_flagged_items_sorted_desc(self):
        r = ro.analyze(OFFLINE_USERNAME, self.build())
        flagged = r["deleted_removed_items"]
        self.assertEqual(len(flagged), 2)
        self.assertEqual({f["status"] for f in flagged}, {"removed", "deleted"})

    def test_exposure_grows_with_identifiers(self):
        clean = ro.analyze(OFFLINE_USERNAME, {"posts": [post("p", UTC, selftext="hello")],
                                      "comments": []})
        dirty = ro.analyze(OFFLINE_USERNAME, {"posts": [post("p", UTC, selftext="a@b.com")],
                                      "comments": []})
        self.assertEqual(clean["score_exposure_0_100"], 0)
        self.assertGreater(dirty["score_exposure_0_100"], clean["score_exposure_0_100"])

    def test_empty_input(self):
        r = ro.analyze("ghostaccount", {"posts": [], "comments": []})
        self.assertEqual(r["totals"]["items"], 0)
        self.assertIsNone(r["totals"]["first_activity"])
        self.assertIsNone(r["timezone_guess"]["busiest_hour_utc"])

    def test_meta_passthrough(self):
        r = ro.analyze(OFFLINE_USERNAME, self.build(), meta={"total_karma": 41})
        self.assertEqual(r["archive_meta"]["total_karma"], 41)


class CollectTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.srv, self.url = start_server(OriginHandler, "api", self.api)
        self._arctic, self._pull = ro.ARCTIC, ro.PULLPUSH
        ro.ARCTIC, ro.PULLPUSH = self.url, self.url
        self.api.add("/api/posts/search",
                     lambda p, q: (200, {"data": [post("shared", 10), post("only_arctic", 20)]}))
        self.api.add("/api/comments/search",
                     lambda p, q: (200, {"data": [comment("c1", 30)]}))
        self.api.add("/reddit/search/submission/",
                     lambda p, q: (200, {"data": [post("shared", 10), post("only_pull", 40)]}))
        self.api.add("/reddit/search/comment/", lambda p, q: (200, {"data": []}))

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()
        ro.ARCTIC, ro.PULLPUSH = self._arctic, self._pull

    def test_merge_dedup_across_sources(self):
        items, errors = ro.collect(OFFLINE_USERNAME, ["arctic", "pullpush"],
                                   session=ro.PoolSession(verbose=False, sleeper=lambda _: None), delay=0,
                                   verbose=False, rate_limit_retries=0)
        self.assertEqual(errors, [])
        self.assertEqual({p["id"] for p in items["posts"]},
                         {"shared", "only_arctic", "only_pull"})
        self.assertEqual(len(items["posts"]), 3)
        self.assertEqual([c["id"] for c in items["comments"]], ["c1"])

    def test_single_source(self):
        items, _ = ro.collect(OFFLINE_USERNAME, ["arctic"], session=ro.PoolSession(verbose=False, sleeper=lambda _: None),
                              delay=0, verbose=False, rate_limit_retries=0)
        self.assertEqual({p["id"] for p in items["posts"]}, {"shared", "only_arctic"})

    def test_failure_is_recorded_not_raised(self):
        self.api.add("/api/comments/search", lambda p, q: (500, {"error": "boom"}))
        items, errors = ro.collect(OFFLINE_USERNAME, ["arctic"], session=ro.PoolSession(verbose=False, sleeper=lambda _: None),
                                   delay=0, verbose=False, rate_limit_retries=0)
        self.assertEqual(len(errors), 1)
        self.assertIn("arctic/comments", errors[0])
        self.assertEqual(items["comments"], [])


class CliIntegrationTests(unittest.TestCase):
    def setUp(self):
        self.api = FakeAPI()
        self.origin_srv, self.origin_url = start_server(OriginHandler, "api", self.api)
        self.api.add("/api/users/search",
                     lambda p, q: (200, {"data": [{"_meta": {"total_karma": 7}}]}))
        self.api.add("/api/comments/search", lambda p, q: (200, {"data": []}))
        self.api.add("/reddit/search/submission/", lambda p, q: (200, {"data": []}))
        self.api.add("/reddit/search/comment/", lambda p, q: (200, {"data": []}))

        ProxyHandler.log = []
        ProxyHandler.origin = self.api
        self.proxy_srvs, self.proxy_urls = [], []
        for fail_first in (2, 0):
            srv, url = start_server(ProxyHandler)
            self.proxy_srvs.append(srv)
            self.proxy_urls.append(url)
        self.fail_first = {}

        import tempfile
        self.proxies_file = tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False)
        for url in self.proxy_urls:
            self.proxies_file.write(url + "\n")
        self.proxies_file.close()

    def tearDown(self):
        self.origin_srv.shutdown()
        self.origin_srv.server_close()
        for srv in self.proxy_srvs:
            srv.shutdown()

    def run_cli(self, *extra, proxies=True):
        import os
        import subprocess
        env = dict(os.environ)
        env["REDDIT_OSINT_ARCTIC"] = self.origin_url
        env["REDDIT_OSINT_PULLPUSH"] = self.origin_url
        cmd = [sys.executable, os.path.join(os.path.dirname(__file__) or ".",
                                           "reddit_osint.py"), OFFLINE_USERNAME, "--quiet"]
        if proxies:
            cmd += ["--proxies", self.proxies_file.name, "--cooldown", "0", "--delay", "0"]
        cmd += list(extra)
        return subprocess.run(cmd, capture_output=True, text=True, env=env, timeout=90)

    def test_cli_end_to_end_with_proxies(self):
        import tempfile
        self.api.add("/api/posts/search", lambda p, q: (200, {"data": [
            post("a", 1000), post("b", 1001, selftext="[removed]",
                                  removed_by_category="automod_filtered")]}))
        out = os.path.join(tempfile.mkdtemp(), "report.json")
        proc = self.run_cli("--proxy-stats", "--json", out)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        with open(out, encoding="utf-8") as fh:
            full = json.load(fh)
        report = json.loads(proc.stdout)
        self.assertEqual(report["totals"], full["totals"])
        self.assertEqual(len(full["all_items"]), 2)
        self.assertEqual(report["totals"]["posts"], 2)
        self.assertEqual(report["totals"]["removed_by_mods"], 1)
        self.assertEqual(report["archive_meta"]["total_karma"], 7)
        self.assertEqual(len(report["proxy_stats"]), 2)
        self.assertTrue(any(s["ok"] for s in report["proxy_stats"].values()))
        self.assertTrue(ProxyHandler.log)

    def test_cli_without_proxies(self):
        self.api.add("/api/posts/search", lambda p, q: (200, {"data": [post("a", 1000)]}))
        proc = self.run_cli(proxies=False)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertEqual(json.loads(proc.stdout)["totals"]["posts"], 1)
        self.assertEqual(ProxyHandler.log, [])

    def test_cli_bad_proxy_file_exits_2(self):
        import tempfile
        with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as fh:
            fh.write("# empty\n")
            path = fh.name
        proc = self.run_cli("--proxies", path)
        self.assertEqual(proc.returncode, 2)
        self.assertIn("no valid proxies", proc.stderr)

    def test_cli_subreddit_and_keywords(self):
        self.api.add("/api/posts/search", lambda p, q: (200, {"data": []}))
        self.run_cli("--subreddit", "dotnet", "--keywords", "aws")
        self.assertTrue(any("subreddit=dotnet" in h and "query=aws" in h
                            for h in self.api.hits))


@unittest.skipUnless(RUN_LIVE and LIVE_USERNAME,
                     "set REDDIT_OSINT_LIVE=1 and REDDIT_OSINT_TEST_USER=<username>")
class LiveArchiveTests(unittest.TestCase):
    maxDiff = None

    def setUp(self):
        self.session = ro.PoolSession(pool=ro.ProxyPool([]), verbose=False)

    def test_profile_metadata_shape(self):
        url = f"{ro.ARCTIC}/api/users/search?author={urllib.parse.quote(LIVE_USERNAME)}&limit=1"
        data = ro.http_get(url, session=self.session).get("data") or []
        for row in data:
            self.assertEqual(row.get("author", "").lower(), LIVE_USERNAME.lower())
            if "_meta" in row:
                for key in ("num_posts", "num_comments"):
                    self.assertIn(key, row["_meta"])

    def test_collect_and_analyze(self):
        items, _ = ro.collect(LIVE_USERNAME, ["arctic"], session=self.session,
                              max_pages=2, delay=0.5, verbose=False)
        report = ro.analyze(LIVE_USERNAME, items)
        self.assertEqual(report["username"], LIVE_USERNAME)
        self.assertEqual(len(report["activity_by_hour_utc"]), 24)
        self.assertEqual(len(report["activity_by_weekday"]), 7)
        self.assertEqual(report["totals"]["items"],
                         report["totals"]["posts"] + report["totals"]["comments"])
        self.assertGreaterEqual(report["score_exposure_0_100"], 0)
        self.assertLessEqual(report["score_exposure_0_100"], 100)
        if report["totals"]["items"]:
            self.assertIsNotNone(report["totals"]["first_activity"])
            self.assertIsNotNone(report["totals"]["last_activity"])

    def test_ids_are_unique_and_sorted(self):
        items, _ = ro.collect(LIVE_USERNAME, ["arctic"], session=self.session,
                              max_pages=2, delay=0.5, verbose=False)
        for kind, rows in items.items():
            ids = [r["id"] for r in rows]
            self.assertEqual(len(ids), len(set(ids)), f"duplicate ids in {kind}")
            stamps = [r.get("created_utc") or 0 for r in rows]
            self.assertEqual(stamps, sorted(stamps), f"{kind} not sorted by created_utc")

    def test_every_item_has_a_known_status(self):
        items, _ = ro.collect(LIVE_USERNAME, ["arctic"], session=self.session,
                              max_pages=1, delay=0.5, verbose=False)
        for kind, rows in items.items():
            for row in rows:
                self.assertIn(ro.status_of(row, kind), ("live", "removed", "deleted"))


if __name__ == "__main__":
    unittest.main(verbosity=2)