import asyncio
import json
import threading
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit

import pytest

import approve
from conftest import ME_SLACK, URL_RE, FakeGitLab, make_mr, mr_url


class Handler(BaseHTTPRequestHandler):
    def handle_any(self):
        method = urlsplit(self.path).path.lstrip("/")
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length).decode()
        self.server.calls.append({
            "method": method,
            "http": self.command,
            "query": parse_qs(urlsplit(self.path).query),
            "form": parse_qs(body),
            "auth": self.headers.get("Authorization"),
        })
        queue = self.server.script.get(method, [(200, {}, {"ok": True})])
        status, headers, payload = queue.pop(0) if len(queue) > 1 else queue[0]
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        for key, value in headers.items():
            self.send_header(key, value)
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = do_POST = handle_any

    def log_message(self, *args):
        pass


@pytest.fixture
def server(monkeypatch):
    monkeypatch.setattr(approve, "SLACK_PAUSE", 0)
    monkeypatch.setattr(approve.time, "sleep", lambda s: None)
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    httpd.calls, httpd.script = [], {}
    thread = threading.Thread(target=lambda: httpd.serve_forever(poll_interval=0.01), daemon=True)
    thread.start()
    httpd.url = f"http://127.0.0.1:{httpd.server_address[1]}/"
    yield httpd
    httpd.shutdown()
    httpd.server_close()


def web(server, token="xoxp-secret"):
    return approve.SlackWeb(token, base_url=server.url)


def run(coro):
    return asyncio.run(coro)


def ok(payload, headers=None):
    return (200, headers or {}, {"ok": True, **payload})


def err(code, **extra):
    return (200, {}, {"ok": False, "error": code, **extra})


class TestIdentityAndScopes:
    def test_whoami_posts_with_the_bearer_token(self, server):
        server.script["auth.test"] = [ok({"user_id": "U0ALICE001"}, {"x-oauth-scopes": "search:read, reactions:read,reactions:write"})]
        client = web(server)
        assert run(client.whoami()) == "U0ALICE001"
        call = server.calls[0]
        assert call["http"] == "POST" and call["auth"] == "Bearer xoxp-secret"
        assert client.scopes == {"search:read", "reactions:read", "reactions:write"}

    def test_missing_scopes_header_leaves_scopes_unknown(self, server):
        server.script["auth.test"] = [ok({"user_id": "U0ALICE001"})]
        client = web(server)
        run(client.whoami())
        assert client.scopes is None and approve.check_token_scopes(client) == []

    def test_missing_scopes_are_reported(self, server):
        server.script["auth.test"] = [ok({"user_id": "U1"}, {"x-oauth-scopes": "search:read"})]
        client = web(server)
        run(client.whoami())
        assert approve.check_token_scopes(client) == ["reactions:read", "reactions:write"]


class TestSearch:
    def matches(self, *items):
        return ok({"messages": {"matches": list(items)}})

    def human(self, text, ts="1.1", channel="front", cid="C1", **extra):
        return {"channel": {"id": cid, "name": channel}, "ts": ts, "text": text, "user": "U1", **extra}

    def test_phrase_query_and_exact_url_extraction(self, server):
        text = f"review <{mr_url(7)}|MR !7> et <{mr_url(8)}|!8>"
        server.script["search.messages"] = [self.matches(self.human(text))]
        found = run(web(server).search_mr(7, URL_RE))
        assert server.calls[0]["http"] == "GET"
        assert server.calls[0]["query"]["query"] == ['"merge_requests/7"']
        assert found == [{"channel": "front", "channel_id": "C1", "ts": "1.1", "urls": [mr_url(7), mr_url(8)]}]

    def test_bot_messages_are_ignored(self, server):
        server.script["search.messages"] = [self.matches(
            self.human(f"<{mr_url(7)}|x>", bot_id="B1"),
            self.human(f"<{mr_url(7)}|x>", ts="1.2", subtype="bot_message"),
            self.human(f"<{mr_url(7)}|x>", ts="1.3"),
        )]
        assert [m["ts"] for m in run(web(server).search_mr(7, URL_RE))] == ["1.3"]

    def test_html_escaped_text_is_unescaped_before_matching(self, server):
        url = mr_url(7) + "?a=1&b=2"
        server.script["search.messages"] = [self.matches(self.human(f"<{url.replace('&', '&amp;')}|x>"))]
        assert run(web(server).search_mr(7, URL_RE))[0]["urls"] == [mr_url(7)]

    def test_no_result(self, server):
        server.script["search.messages"] = [ok({"messages": {"matches": []}})]
        assert run(web(server).search_mr(7, URL_RE)) == []


class TestReactions:
    def test_reactors_of_the_wanted_emoji_only(self, server):
        server.script["reactions.get"] = [ok({"message": {"reactions": [
            {"name": "check", "users": ["U9"]}, {"name": "lgtm", "users": ["U1", "U2"]}]}})]
        assert run(web(server).reactors("C1", "1.1", "lgtm")) == {"U1", "U2"}
        assert server.calls[0]["query"]["full"] == ["true"]

    def test_message_without_reactions(self, server):
        server.script["reactions.get"] = [ok({"message": {}})]
        assert run(web(server).reactors("C1", "1.1", "lgtm")) == set()

    def test_react_posts_channel_timestamp_and_name(self, server):
        run(web(server).react("C1", "1.1", "lgtm"))
        call = server.calls[0]
        assert call["http"] == "POST" and call["form"] == {"channel": ["C1"], "timestamp": ["1.1"], "name": ["lgtm"]}

    def test_already_reacted_is_not_an_error(self, server):
        server.script["reactions.add"] = [err("already_reacted")]
        run(web(server).react("C1", "1.1", "lgtm"))

    def test_other_reaction_errors_are_raised(self, server):
        server.script["reactions.add"] = [err("channel_not_found")]
        with pytest.raises(approve.SlackError) as exc:
            run(web(server).react("C1", "1.1", "lgtm"))
        assert exc.value.code == "channel_not_found"


class TestErrors:
    def test_invalid_token_message_tells_what_to_do(self, server):
        server.script["auth.test"] = [err("invalid_auth")]
        with pytest.raises(approve.SlackError, match="approve --init"):
            run(web(server).whoami())

    def test_missing_scope_names_the_needed_right(self, server):
        server.script["search.messages"] = [err("missing_scope", needed="search:read")]
        with pytest.raises(approve.SlackError, match="search:read"):
            run(web(server).search_mr(1, URL_RE))

    def test_rate_limit_is_retried(self, server):
        server.script["auth.test"] = [(429, {"Retry-After": "0"}, {"ok": False}), ok({"user_id": "U1"})]
        assert run(web(server).whoami()) == "U1"
        assert len(server.calls) == 2

    def test_server_error_is_retried(self, server):
        server.script["auth.test"] = [(503, {}, {"ok": False}), ok({"user_id": "U1"})]
        assert run(web(server).whoami()) == "U1"

    def test_persistent_server_error_gives_up_with_its_status(self, server):
        server.script["auth.test"] = [(500, {}, {"ok": False})]
        with pytest.raises(approve.SlackError, match="http_500"):
            run(web(server).whoami())
        assert len(server.calls) == approve.SLACK_RETRIES

    def test_unreachable_slack_is_reported_plainly(self, monkeypatch):
        monkeypatch.setattr(approve.time, "sleep", lambda s: None)
        with pytest.raises(RuntimeError, match="injoignable"):
            run(approve.SlackWeb("xoxp-x", base_url="http://127.0.0.1:9/").whoami())


def test_a_full_pass_through_the_real_http_client(server, cfg):
    gl = FakeGitLab([make_mr(1)])
    server.script["search.messages"] = [ok({"messages": {"matches": [{
        "channel": {"id": "C1", "name": "front"}, "ts": "1.1", "user": "U1",
        "text": f"<{mr_url(1)}|!1>"}]}})]
    server.script["reactions.get"] = [ok({"message": {"reactions": []}}), ok({"message": {"reactions": [
        {"name": "lgtm", "users": [ME_SLACK]}]}})]
    stats = asyncio.run(approve.process_person(
        "alice", cfg["people"]["alice"], cfg, gl, web(server), {"mrs": {}}, False))
    assert gl.posts == [(1, "sha1")] and stats["reacted"] == 1
    added = [c for c in server.calls if c["method"] == "reactions.add"]
    assert len(added) == 1 and added[0]["form"]["name"] == ["lgtm"]
    assert all(c["auth"] == "Bearer xoxp-secret" for c in server.calls)


def test_slack_id_is_deduced_from_the_token_when_the_config_has_none(server, cfg, monkeypatch):
    from contextlib import asynccontextmanager

    server.script["auth.test"] = [ok({"user_id": "U0FROMTOKEN"})]

    @asynccontextmanager
    async def session(config):
        yield web(server)

    monkeypatch.setattr(approve, "GitLab", lambda host: FakeGitLab([]))
    monkeypatch.setattr(approve, "slack_session", session)
    cfg["me"].pop("slack_id")
    asyncio.run(approve.run_pass(cfg, ["alice"], True, {"mrs": {}}, 30))
    assert cfg["me"]["slack_id"] == "U0FROMTOKEN"


class TestRecentScan:
    def msg(self, ts, text=None):
        return {"channel": {"id": "C1", "name": "front"}, "ts": ts, "user": "U1", "text": text or f"<{mr_url(7)}|!7>"}

    def page(self, matches, pages=1):
        return ok({"messages": {"matches": matches, "paging": {"pages": pages}}})

    def test_asks_for_the_keyword_since_the_day_before_and_keeps_only_recent_messages(self, server):
        server.script["search.messages"] = [self.page([self.msg("2000.5"), self.msg("10.5")], pages=3)]
        found = run(web(server).search_recent(1000, URL_RE))
        call = server.calls[0]
        assert call["query"]["query"][0].startswith("merge_requests after:1969-12-31")
        assert call["query"]["sort"] == ["timestamp"]
        assert [m["ts"] for m in found] == ["2000.5"] and len(server.calls) == 1

    def test_follows_the_pages_while_everything_is_recent(self, server):
        server.script["search.messages"] = [
            self.page([self.msg("3000.1"), self.msg("2000.2")], pages=2),
            self.page([self.msg("1500.3"), self.msg("5.4")], pages=2),
        ]
        found = run(web(server).search_recent(1000, URL_RE))
        assert [m["ts"] for m in found] == ["3000.1", "2000.2", "1500.3"]
        assert [c["query"]["page"] for c in server.calls] == [["1"], ["2"]]

    def test_stops_at_the_last_page(self, server):
        server.script["search.messages"] = [self.page([self.msg("3000.1")], pages=1)]
        run(web(server).search_recent(1000, URL_RE))
        assert len(server.calls) == 1

    def test_bot_messages_are_ignored(self, server):
        bot = {**self.msg("3000.1"), "bot_id": "B1"}
        server.script["search.messages"] = [self.page([bot, self.msg("3000.2")])]
        assert [m["ts"] for m in run(web(server).search_recent(1000, URL_RE))] == ["3000.2"]
