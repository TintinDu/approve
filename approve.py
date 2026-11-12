import argparse
import asyncio
import copy
import fcntl
import getpass
import html
import json
import os
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import zlib
import urllib.error
import urllib.parse
import urllib.request
from collections import namedtuple
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path


def resolve_paths(env, home):
    app = Path(env.get("MR_APPROVER_HOME") or home / ".mr-approver")
    return {
        "app": app,
        "hermes": Path(env.get("HERMES_HOME") or home / ".hermes"),
        "config": Path(env["APPROVE_CONFIG"]) if env.get("APPROVE_CONFIG") else app / "config.json",
    }


_PATHS = resolve_paths(os.environ, Path.home())
APP_DIR = _PATHS["app"]
HERMES = _PATHS["hermes"]
CONFIG_PATH = _PATHS["config"]
STATE_DIR = APP_DIR / "state"
STATE_PATH = STATE_DIR / "state.json"
LOCK_PATH = STATE_DIR / "approve.lock"
PAUSE_PATH = STATE_DIR / "PAUSE"
WATCH_PATH = APP_DIR / "watch.json"
JOURNAL_PATH = APP_DIR / "journal.jsonl"
TOKEN_PATH = APP_DIR / "slack_token"
TOKEN_ENV = "MR_APPROVER_SLACK_TOKEN"
FAILURES_BEFORE_NOTIFY = 3
DEFAULT_MAX_APPROVALS = 30
DEFAULT_REVERIFY_MINUTES = 5
PAUSE_POLL_SECONDS = 5
STATUS_WINDOW_HOURS = 24
EXIT_CAP_REACHED = 3
EXIT_INTERRUPTED = 130
STOP_WAIT_SECONDS = 40
SUBCOMMANDS = ("add", "remove", "list")
INTERRUPTED = threading.Event()
DO_NOT_MERGE = re.compile(r"do[\s_-]*not[\s_-]*merge", re.IGNORECASE)
SLACK_RETRIES = 3
SLACK_PAUSE = 0.1
GITLAB_CONCURRENCY = 6
SCAN_MARGIN_SECONDS = 120
SCAN_MAX_PAGES = 5
RECHECK_SECONDS = 900
BLOCKED_OUTCOMES = ("not_allowed", "skipped", "do_not_merge")
RECENT_KEYWORD = "merge_requests"
MIN_PYTHON = (3, 11)
SLACK_API = "https://slack.com/api/"
SLACK_TIMEOUT = 30
RETRY_AFTER_CAP = 30
REQUIRED_SCOPES = ("search:read", "reactions:read", "reactions:write")
SLACK_HINTS = {
    "invalid_auth": "jeton refusé ou révoqué : relance approve --init pour en saisir un nouveau",
    "not_authed": "aucun jeton envoyé : relance approve --init",
    "token_revoked": "jeton révoqué : relance approve --init pour en saisir un nouveau",
    "account_inactive": "compte Slack désactivé",
    "missing_scope": "le jeton n'a pas tous les droits (" + ", ".join(REQUIRED_SCOPES) + ")",
    "ratelimited": "Slack limite le débit : réessaie plus tard ou allonge --interval",
}
GITLAB_ONLY_NOTE = "Slack non configuré : mode GitLab seul (aucune réaction, pas de filtre « annoncée sur Slack »)"
PLACEHOLDERS = {"gitlab.example.com", "UXXXXXXXXXX"}
DEFAULT_CONFIG = {
    "emoji": "lgtm",
    "exclude_channels": [],
    "interval_seconds": 300,
    "rediscovery_minutes": 60,
    "merged_grace_hours": 48,
    "reverify_minutes": DEFAULT_REVERIFY_MINUTES,
    "max_approvals_per_pass": DEFAULT_MAX_APPROVALS,
    "slack_backend": "auto",
    "slack_mcp": {
        "url": "https://mcp.slack.com/mcp",
        "oauth": {"redirect_port": 3118, "redirect_host": "localhost"},
    },
    "people": {},
}
Check = namedtuple("Check", "ok label detail hint")

sys.path.insert(0, str(HERMES / "hermes-agent"))


ANSI = {"bold": "1", "dim": "2", "red": "91", "green": "92", "yellow": "93",
        "blue": "94", "magenta": "95", "cyan": "96"}
LOG_MARKS = {"ok": ("■", "green"), "again": ("▣", "cyan"), "err": ("▚", "red"),
             "warn": ("▒", "yellow"), "pass": ("▓▒░", "magenta")}
BANNER_TEXT = "APPROVE"
BANNER_TAGLINE = "▀▄▀▄▀▄  NO LLM · NO MERCY · LGTM  ▄▀▄▀▄▀"
BANNER_ROW_COLORS = ["magenta", "magenta", "blue", "cyan", "cyan"]
BANNER_PIXEL = "██"
BANNER_FONT = {
    "A": [".##.", "#..#", "####", "#..#", "#..#"],
    "P": ["###.", "#..#", "###.", "#...", "#..."],
    "R": ["###.", "#..#", "###.", "#.#.", "#..#"],
    "O": [".##.", "#..#", "#..#", "#..#", ".##."],
    "V": ["#..#", "#..#", "#..#", "#..#", ".##."],
    "E": ["####", "#...", "###.", "#...", "####"],
}


def color_enabled(stream=None):
    stream = stream or sys.stdout
    if os.environ.get("NO_COLOR") or os.environ.get("TERM") == "dumb":
        return False
    return stream.isatty()


def paint(text, *styles):
    if not styles or not color_enabled():
        return text
    return f"\x1b[{';'.join(ANSI[style] for style in styles)}m{text}\x1b[0m"


def log(msg, kind=None):
    stamp = paint(f"[{datetime.now():%H:%M:%S}]", "dim")
    if kind in LOG_MARKS and color_enabled():
        mark, color = LOG_MARKS[kind]
        print(f"{stamp} {paint(mark, color, 'bold')} {paint(msg.lstrip(), color)}", flush=True)
    else:
        print(f"{stamp} {msg}", flush=True)


def banner_rows():
    glyphs = [BANNER_FONT[letter] for letter in BANNER_TEXT]
    rows = []
    for y in range(len(glyphs[0])):
        cells = ("".join(BANNER_PIXEL if bit == "#" else " " * len(BANNER_PIXEL) for bit in glyph[y])
                 for glyph in glyphs)
        rows.append("  ".join(cells).rstrip())
    return rows


def print_banner():
    if not color_enabled():
        return
    print()
    for row, color in zip(banner_rows(), BANNER_ROW_COLORS):
        print(paint(row, color, "bold"))
    print(paint(BANNER_TAGLINE, "yellow") + "\n", flush=True)


def heading(text, plain=None):
    if color_enabled():
        print(paint(f"▓▒░ {text.upper()} ░▒▓", "magenta", "bold"), flush=True)
    elif plain is not None:
        print(plain, flush=True)


def check_mark(ok):
    if not color_enabled():
        return "✓" if ok else "✗"
    return paint("■", "green", "bold") if ok else paint("▚", "red", "bold")


def journal(event, **data):
    JOURNAL_PATH.parent.mkdir(parents=True, exist_ok=True)
    row = {"at": datetime.now().isoformat(timespec="seconds"), "event": event, **data}
    with JOURNAL_PATH.open("a") as f:
        f.write(json.dumps(row, ensure_ascii=False) + "\n")


def notify(title, message):
    if not shutil.which("osascript"):
        return
    script = f'display notification {json.dumps(message)} with title {json.dumps(title)}'
    subprocess.run(["osascript", "-e", script], check=False, capture_output=True)


class ApprovalCapReached(Exception):
    pass


class Budget:
    def __init__(self, cap):
        self.cap = cap
        self.used = 0
        self.lock = threading.Lock()

    def take(self):
        with self.lock:
            if self.used >= self.cap:
                raise ApprovalCapReached(self.cap)
            self.used += 1


class GitLab:
    def __init__(self, host):
        self.host = host

    def api(self, path, method="GET", fields=None, paginate=False):
        cmd = ["glab", "api", "--hostname", self.host, "-X", method]
        if paginate:
            cmd.append("--paginate")
        for k, v in (fields or {}).items():
            cmd += ["-f", f"{k}={v}"]
        cmd.append(path)
        proc = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
        if proc.returncode != 0:
            raise RuntimeError((proc.stdout + proc.stderr).strip()[:300])
        return self._decode(proc.stdout, paginate)

    @staticmethod
    def _decode(raw, paginate):
        if not paginate:
            return json.loads(raw) if raw.strip() else None
        decoder, pos, items = json.JSONDecoder(), 0, []
        raw = raw.strip()
        while pos < len(raw):
            chunk, pos = decoder.raw_decode(raw, pos)
            items += chunk
            while pos < len(raw) and raw[pos].isspace():
                pos += 1
        return items

    def open_mrs(self, author_id):
        path = f"merge_requests?state=opened&scope=all&author_id={author_id}&per_page=100"
        return self.api(path, paginate=True)

    def mr(self, project, iid):
        return self.api(f"projects/{project}/merge_requests/{iid}")

    def approval_info(self, project, iid):
        data = self.api(f"projects/{project}/merge_requests/{iid}/approvals")
        approvers = {a["user"]["id"] for a in data["approved_by"]}
        return approvers, bool(data.get("user_can_approve", True))

    def approvers(self, project, iid):
        return self.approval_info(project, iid)[0]

    def approve(self, project, iid, sha):
        return self.api(
            f"projects/{project}/merge_requests/{iid}/approve", "POST", {"sha": sha}
        )


class HermesSlack:
    def __init__(self, session):
        self.session = session
        self.lock = asyncio.Lock()

    async def call(self, tool, args):
        async with self.lock:
            last = None
            for attempt in range(SLACK_RETRIES):
                try:
                    result = await self.session.call_tool(tool, args)
                    text = result.content[0].text if result.content else ""
                    if result.is_error:
                        raise RuntimeError(text[:300])
                    await asyncio.sleep(SLACK_PAUSE)
                    return json.loads(text)
                except Exception as exc:
                    last = exc
                    await asyncio.sleep(2 ** attempt)
            raise RuntimeError(f"{tool}: {last}")

    async def search_recent(self, since, mr_url_re):
        found, cursor = [], None
        for _ in range(SCAN_MAX_PAGES):
            args = {"keywords": [RECENT_KEYWORD], "after": str(int(since)), "sort": "timestamp",
                    "sort_dir": "desc", "include_context": False, "limit": 20}
            if cursor:
                args["cursor"] = cursor
            data = await self.call("slack_search_public_and_private", args)
            found += parse_search(data.get("results", ""), mr_url_re)
            match = re.search(r"cursor `([^`]+)`", data.get("pagination_info", ""))
            if not match:
                break
            cursor = match.group(1)
        return found

    async def whoami(self):
        data = await self.call("slack_read_user_profile", {"response_format": "detailed"})
        found = re.search(r"User ID: ([UW][A-Z0-9]+)", data.get("result", ""))
        if not found:
            raise RuntimeError("réponse Slack sans ID de membre")
        return found.group(1)

    async def search_mr(self, iid, mr_url_re):
        data = await self.call(
            "slack_search_public_and_private",
            {"keywords": [f'"merge_requests/{iid}"'], "include_context": False, "limit": 20},
        )
        return parse_search(data.get("results", ""), mr_url_re)

    async def reactors(self, channel_id, ts, emoji):
        data = await self.call(
            "slack_get_reactions", {"channel_id": channel_id, "message_ts": ts}
        )
        for line in data.get("result", "").splitlines():
            if line.startswith(f":{emoji}:"):
                return set(re.findall(r"\((U[A-Z0-9]+)\)", line))
        return set()

    async def react(self, channel_id, ts, emoji):
        await self.call(
            "slack_add_reaction", {"channel_id": channel_id, "message_ts": ts, "emoji": emoji}
        )


class NoSlack:
    async def whoami(self):
        return None

    async def search_recent(self, since, mr_url_re):
        return []

    async def search_mr(self, iid, mr_url_re):
        return []

    async def reactors(self, channel_id, ts, emoji):
        return set()

    async def react(self, channel_id, ts, emoji):
        return None


class SlackError(RuntimeError):
    def __init__(self, code, detail=""):
        hint = SLACK_HINTS.get(code)
        text = f"Slack : {code}" + (f" ({detail})" if detail else "") + (f" — {hint}" if hint else "")
        super().__init__(text)
        self.code = code


class SlackWeb:
    def __init__(self, token, base_url=SLACK_API):
        self.token = token
        self.base_url = base_url
        self.scopes = None
        self.lock = asyncio.Lock()

    def request(self, method, params=None, post=False):
        body = urllib.parse.urlencode(params or {})
        for attempt in range(SLACK_RETRIES):
            last = attempt == SLACK_RETRIES - 1
            url = self.base_url + method
            if post:
                req = urllib.request.Request(url, data=body.encode(), method="POST")
            else:
                req = urllib.request.Request(f"{url}?{body}" if body else url)
            req.add_header("Authorization", f"Bearer {self.token}")
            try:
                with urllib.request.urlopen(req, timeout=SLACK_TIMEOUT) as resp:
                    payload = json.loads(resp.read())
                    scopes = resp.headers.get("x-oauth-scopes")
            except urllib.error.HTTPError as exc:
                if exc.code == 429 and not last:
                    time.sleep(min(int(exc.headers.get("Retry-After") or 1), RETRY_AFTER_CAP))
                    continue
                if exc.code >= 500 and not last:
                    time.sleep(2 ** attempt)
                    continue
                raise SlackError(f"http_{exc.code}") from exc
            except (urllib.error.URLError, TimeoutError, ConnectionError) as exc:
                if not last:
                    time.sleep(2 ** attempt)
                    continue
                raise RuntimeError(f"Slack injoignable : {exc}") from exc
            if scopes is not None:
                self.scopes = {name.strip() for name in scopes.split(",") if name.strip()}
            if not payload.get("ok"):
                needed = payload.get("needed")
                raise SlackError(payload.get("error", "unknown"), f"droit manquant : {needed}" if needed else "")
            return payload
        raise RuntimeError("Slack : plus de tentatives")

    async def call(self, method, params=None, post=False):
        async with self.lock:
            data = await asyncio.to_thread(self.request, method, params, post)
            await asyncio.sleep(SLACK_PAUSE)
            return data

    async def search_recent(self, since, mr_url_re):
        day = time.strftime("%Y-%m-%d", time.gmtime(since - 86400))
        found = []
        for page in range(1, SCAN_MAX_PAGES + 1):
            data = await self.call("search.messages", {
                "query": f"{RECENT_KEYWORD} after:{day}", "count": 100, "sort": "timestamp",
                "sort_dir": "desc", "page": page})
            block = data.get("messages", {})
            matches = block.get("matches", [])
            fresh = [m for m in matches if float(m.get("ts", 0)) >= since]
            found += parse_web_matches(fresh, mr_url_re)
            if len(fresh) < len(matches) or page >= block.get("paging", {}).get("pages", 1):
                break
        return found

    async def whoami(self):
        return (await self.call("auth.test", post=True))["user_id"]

    async def search_mr(self, iid, mr_url_re):
        data = await self.call(
            "search.messages", {"query": f'"merge_requests/{iid}"', "count": 20, "sort": "timestamp"}
        )
        return parse_web_matches(data.get("messages", {}).get("matches", []), mr_url_re)

    async def reactors(self, channel_id, ts, emoji):
        data = await self.call("reactions.get", {"channel": channel_id, "timestamp": ts, "full": "true"})
        for reaction in data.get("message", {}).get("reactions", []):
            if reaction.get("name") == emoji:
                return set(reaction.get("users", []))
        return set()

    async def react(self, channel_id, ts, emoji):
        try:
            await self.call("reactions.add", {"channel": channel_id, "timestamp": ts, "name": emoji}, post=True)
        except SlackError as exc:
            if exc.code != "already_reacted":
                raise


def parse_web_matches(matches, mr_url_re):
    messages = []
    for match in matches:
        channel = match.get("channel") or {}
        if match.get("bot_id") or match.get("subtype") == "bot_message":
            continue
        if not channel.get("id") or not match.get("ts"):
            continue
        text = html.unescape(match.get("text", ""))
        messages.append(
            {
                "channel": channel.get("name", ""),
                "channel_id": channel["id"],
                "ts": match["ts"],
                "urls": sorted(set(mr_url_re.findall(text))),
            }
        )
    return messages


def parse_search(text, mr_url_re):
    messages = []
    for block in re.split(r"### Result \d+ of \d+\n", text)[1:]:
        channel = re.search(r"Channel: (.+?) \(ID: (\w+)\)", block)
        ts = re.search(r"Message_ts: (\d+\.\d+)", block)
        sender = re.search(r"From: (.*)", block)
        if not channel or not ts or (sender and "[BOT]" in sender.group(1)):
            continue
        messages.append(
            {
                "channel": channel.group(1).lstrip("#"),
                "channel_id": channel.group(2),
                "ts": ts.group(1),
                "urls": sorted(set(mr_url_re.findall(block))),
            }
        )
    return messages


def load_state():
    try:
        return json.loads(STATE_PATH.read_text())
    except (OSError, ValueError):
        return {"mrs": {}}


def save_state(state):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    tmp = STATE_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(state, indent=1))
    tmp.replace(STATE_PATH)


def search_due(key, now, cfg):
    spread = 1 + (zlib.crc32(key.encode()) % 50) / 100
    return now + cfg.get("rediscovery_minutes", 60) * 60 * spread


async def discover(slack, mr, entry, mr_url_re, now, due=None):
    found = await slack.search_mr(mr["iid"], mr_url_re)
    known = {(m["channel_id"], m["ts"]): m for m in entry.get("messages", [])}
    for m in found:
        if mr["web_url"] in m["urls"]:
            known[(m["channel_id"], m["ts"])] = m
    entry["messages"] = list(known.values())
    entry["last_search"] = now
    if due is not None:
        entry["search_due"] = due


def apply_recent(entry, mr_url, recent):
    known = {(m["channel_id"], m["ts"]): m for m in entry.get("messages", [])}
    changed = False
    for m in recent:
        if mr_url in m["urls"] and (m["channel_id"], m["ts"]) not in known:
            known[(m["channel_id"], m["ts"])] = m
            changed = True
    if changed:
        entry["messages"] = list(known.values())
    return changed


def needs_discovery(entry, person, cfg, now, scan_ok=False):
    if "last_search" not in entry:
        return True
    due = entry.get("search_due", entry["last_search"] + cfg["rediscovery_minutes"] * 60)
    if now >= due:
        return True
    return not scan_ok and not entry.get("messages")


def settled(entry, cfg, now):
    if not entry.get("messages"):
        return False
    ttl = cfg["rediscovery_minutes"] * 60
    if now - entry.get("last_search", 0) > ttl:
        return False
    reactions = entry.get("reactions", {})
    for msg in entry["messages"]:
        if msg["channel"] in cfg["exclude_channels"] or msg["channel_id"] in cfg["exclude_channels"]:
            continue
        done = reactions.get(f"{msg['channel_id']}:{msg['ts']}")
        if not done or now - done["at"] >= ttl:
            return False
    return True


def mr_key(mr):
    return f"{mr['project_id']}!{mr['iid']}"


def approve_mr(gl, mr, me_id, dry, budget=None):
    approvers, can_approve = gl.approval_info(mr["project_id"], mr["iid"])
    if me_id in approvers:
        return "already", mr["sha"]
    fresh = gl.mr(mr["project_id"], mr["iid"])
    if fresh["state"] != "opened" or fresh.get("draft"):
        return "skipped", fresh["sha"]
    if DO_NOT_MERGE.search(fresh.get("title", "")):
        return "do_not_merge", fresh["sha"]
    if not can_approve:
        return "not_allowed", fresh["sha"]
    if budget is not None:
        budget.take()
    if dry:
        return "would_approve", fresh["sha"]
    gl.approve(mr["project_id"], mr["iid"], fresh["sha"])
    if me_id not in gl.approvers(mr["project_id"], mr["iid"]):
        raise RuntimeError("approval not visible after POST")
    return "approved", fresh["sha"]


def bundle_fully_approved(gl, urls, me_id, mr_url_re, cache):
    for url in urls:
        if url not in cache:
            m = re.match(r"https://[^/]+/(.+)/-/merge_requests/(\d+)$", url)
            project = m.group(1).replace("/", "%2F")
            try:
                cache[url] = me_id in gl.approvers(project, m.group(2))
            except RuntimeError:
                cache[url] = False
        if not cache[url]:
            return False
    return True


async def reconcile_reactions(slack, gl, entry, cfg, me, mr_url_re, dry, now, cache, stats):
    emoji = cfg["emoji"]
    reactions = entry.setdefault("reactions", {})
    ttl = cfg["rediscovery_minutes"] * 60
    for msg in list(entry.get("messages", [])):
        key = f"{msg['channel_id']}:{msg['ts']}"
        if msg["channel"] in cfg["exclude_channels"] or msg["channel_id"] in cfg["exclude_channels"]:
            continue
        done = reactions.get(key)
        if done and now - done["at"] < ttl:
            continue
        lock = cache.setdefault(("lock", key), asyncio.Lock())
        async with lock:
            if ("msg", key) in cache:
                reactions[key] = cache[("msg", key)]
                continue
            approved = await asyncio.to_thread(
                bundle_fully_approved, gl, msg["urls"], me["gitlab_id"], mr_url_re, cache
            )
            if not approved:
                continue
            present = me["slack_id"] in await slack.reactors(msg["channel_id"], msg["ts"], emoji)
            if not present:
                if dry:
                    stats["would_react"] += 1
                    log(f"  DRY réaction :{emoji}: sur {msg['channel']} {msg['ts']}", "warn")
                    continue
                await slack.react(msg["channel_id"], msg["ts"], emoji)
                if me["slack_id"] not in await slack.reactors(msg["channel_id"], msg["ts"], emoji):
                    raise RuntimeError(f"reaction not visible on {key}")
                stats["reacted"] += 1
                journal("reaction_added", channel=msg["channel"], ts=msg["ts"], urls=msg["urls"])
                log(f"  réaction :{emoji}: ajoutée sur {msg['channel']} {msg['ts']}", "ok")
            reactions[key] = cache[("msg", key)] = {"at": now}


def make_mr_url_re(host):
    return re.compile(r"(https://" + re.escape(host) + r"/[\w.\-/]+?/-/merge_requests/\d+)")


def leaves(group):
    for exc in group.exceptions:
        if isinstance(exc, BaseExceptionGroup):
            yield from leaves(exc)
        else:
            yield exc


def unwrap_group(group):
    found = list(leaves(group))
    for kind in (KeyboardInterrupt, ApprovalCapReached):
        for exc in found:
            if isinstance(exc, kind):
                return exc
    return found[0]


async def run_all(coros):
    try:
        async with asyncio.TaskGroup() as group:
            for coro in coros:
                group.create_task(coro)
    except BaseExceptionGroup as caught:
        raise unwrap_group(caught) from None


STAT_KEYS = ["open", "approved", "reapproved", "already", "would_approve", "skipped", "gated", "reacted",
             "would_react", "errors", "not_allowed", "do_not_merge"]


async def process_person(name, person, cfg, gl, slack, state, dry, budget=None, recent=None):
    me = cfg["me"]
    mr_url_re = make_mr_url_re(cfg["gitlab_host"])
    stats = dict.fromkeys(STAT_KEYS, 0)
    now = time.time()
    scan_ok = recent is not None
    recent = recent or []
    reverify = cfg.get("reverify_minutes", DEFAULT_REVERIFY_MINUTES) * 60
    mrs = [m for m in await asyncio.to_thread(gl.open_mrs, person["gitlab_id"]) if not m.get("draft")]
    stats["open"] = len(mrs)
    cache = {}
    gate = asyncio.Semaphore(GITLAB_CONCURRENCY)

    async def handle(mr):
        async with gate:
            key = mr_key(mr)
            if DO_NOT_MERGE.search(mr.get("title", "")):
                stats["do_not_merge"] += 1
                return
            entry = state["mrs"].setdefault(key, {})
            entry.update(author_id=person["gitlab_id"], last_seen=now, web_url=mr["web_url"])
            apply_recent(entry, mr["web_url"], recent)
            try:
                if needs_discovery(entry, person, cfg, now, scan_ok):
                    await discover(slack, mr, entry, mr_url_re, now, search_due(key, now, cfg))
            except RuntimeError as exc:
                stats["errors"] += 1
                log(f"  recherche Slack en échec pour {key}: {exc}", "warn")
                if person["require_announcement"]:
                    return
            if person["require_announcement"] and not entry.get("messages"):
                stats["gated"] += 1
                return
            try:
                previously_approved = bool(entry.get("approved_sha"))
                blocked = entry.get("blocked", {})
                unchanged = entry.get("approved_sha") == mr.get("sha") and now - entry.get("verified_at", 0) < reverify
                still_blocked = blocked.get("sha") == mr.get("sha") and now - blocked.get("at", 0) < reverify
                if unchanged:
                    outcome, sha = "already", mr["sha"]
                elif still_blocked:
                    outcome, sha = blocked["outcome"], mr["sha"]
                else:
                    outcome, sha = await asyncio.to_thread(approve_mr, gl, mr, me["gitlab_id"], dry, budget)
                    entry.pop("blocked", None)
                    if outcome in BLOCKED_OUTCOMES:
                        entry["blocked"] = {"sha": sha, "outcome": outcome, "at": now}
                stats[outcome] += 1
                entry["sha"] = sha
                if outcome in ("approved", "already"):
                    entry.update(approved_sha=sha, verified_at=now if not unchanged else entry["verified_at"])
                    cache[mr["web_url"]] = True
                if outcome in ("approved", "would_approve"):
                    again = previously_approved
                    stats["reapproved"] += int(again and outcome == "approved")
                    journal("approval", mr=key, sha=sha, url=mr["web_url"], dry=dry, reapproved=again)
                    verb = ("DRY ré-approuverait" if again else "DRY approuverait") if dry else (
                        "ré-approuvée" if again else "approuvée")
                    log(f"  {verb} {key} {mr['title'][:60]}",
                        "warn" if dry else "again" if again else "ok")
                if outcome in ("approved", "already", "would_approve"):
                    await reconcile_reactions(
                        slack, gl, entry, cfg, me, mr_url_re, dry or outcome == "would_approve",
                        now, cache, stats,
                    )
            except RuntimeError as exc:
                stats["errors"] += 1
                journal("error", mr=key, error=str(exc))
                log(f"  erreur sur {key}: {exc}", "err")

    await run_all(handle(mr) for mr in mrs)
    await process_merged(person, cfg, gl, slack, state, dry, mr_url_re, cache, stats, now,
                         {mr_key(m) for m in mrs}, recent, scan_ok)
    return stats


async def process_merged(person, cfg, gl, slack, state, dry, mr_url_re, cache, stats, now, open_keys,
                         recent=None, scan_ok=False):
    me = cfg["me"]
    grace = cfg.get("merged_grace_hours", 48) * 3600
    candidates = []
    for key, entry in state["mrs"].items():
        if key in open_keys or "sha" not in entry:
            continue
        entry.setdefault("last_seen", now)
        if now - entry["last_seen"] > grace or entry.get("author_id", person["gitlab_id"]) != person["gitlab_id"]:
            continue
        if entry.get("web_url") and recent:
            apply_recent(entry, entry["web_url"], recent)
        if settled(entry, cfg, now) or entry.get("recheck_at", 0) > now:
            continue
        if not entry.get("messages") and not needs_discovery(entry, person, cfg, now, scan_ok):
            continue
        candidates.append((key, entry))
    gate = asyncio.Semaphore(GITLAB_CONCURRENCY)

    async def handle(key, entry):
        async with gate:
            project, iid = key.split("!")
            try:
                fresh = await asyncio.to_thread(gl.mr, project, iid)
                if fresh["author"]["id"] != person["gitlab_id"]:
                    entry["author_id"] = fresh["author"]["id"]
                    return
                entry["author_id"] = person["gitlab_id"]
                entry["web_url"] = fresh["web_url"]
                approvers = await asyncio.to_thread(gl.approvers, project, iid)
                if fresh["state"] != "merged" or me["gitlab_id"] not in approvers:
                    return
                if needs_discovery(entry, person, cfg, now, scan_ok):
                    await discover(slack, fresh, entry, mr_url_re, now, search_due(key, now, cfg))
                await reconcile_reactions(slack, gl, entry, cfg, me, mr_url_re, dry, now, cache, stats)
                if not settled(entry, cfg, now):
                    entry["recheck_at"] = now + RECHECK_SECONDS
            except RuntimeError as exc:
                stats["errors"] += 1
                journal("error", mr=key, error=str(exc))
                log(f"  erreur sur MR mergée {key}: {exc}", "err")

    await run_all(handle(key, entry) for key, entry in candidates)


def load_env():
    try:
        from dotenv import load_dotenv
    except ImportError:
        return
    load_dotenv(HERMES / ".env")


def load_slack_token():
    token = os.environ.get(TOKEN_ENV, "").strip()
    if token:
        return token
    try:
        return TOKEN_PATH.read_text().strip() or None
    except OSError:
        return None


def save_slack_token(token):
    TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
    TOKEN_PATH.write_text(token.strip() + "\n")
    TOKEN_PATH.chmod(0o600)


def hermes_available():
    return (HERMES / "hermes-agent").is_dir() and (HERMES / "mcp-tokens" / "slack.json").is_file()


def resolve_backend(cfg):
    choice = cfg.get("slack_backend", "auto")
    if choice in ("token", "hermes"):
        return choice
    if load_slack_token():
        return "token"
    if hermes_available():
        return "hermes"
    return None


def ensure_hermes_python():
    try:
        import mcp  # noqa: F401
        from tools import mcp_oauth  # noqa: F401
        return
    except ImportError:
        pass
    if os.environ.get("MR_APPROVER_REEXEC"):
        return
    pythons = sorted(HERMES.glob("installs/*/environments/*/venv/bin/python"),
                     key=lambda path: path.stat().st_mtime, reverse=True)
    if pythons:
        os.environ["MR_APPROVER_REEXEC"] = "1"
        os.execv(str(pythons[0]), [str(pythons[0]), *sys.argv])


@asynccontextmanager
async def hermes_session(cfg):
    load_env()
    try:
        from mcp import ClientSession
        from mcp.client.streamable_http import create_mcp_http_client, streamable_http_client
        from tools.mcp_oauth import build_oauth_auth, suppress_interactive_oauth
    except ImportError as exc:
        raise RuntimeError(
            f"Hermes introuvable ou incomplet ({exc}). Lance : approve --doctor"
        ) from exc
    if "SLACK_MCP_CLIENT_ID" not in os.environ:
        raise RuntimeError("SLACK_MCP_CLIENT_ID absent de l'environnement. Lance : approve --doctor")
    slack_cfg = cfg["slack_mcp"]
    oauth = {**slack_cfg["oauth"], "client_id": os.environ["SLACK_MCP_CLIENT_ID"]}
    with suppress_interactive_oauth():
        auth = build_oauth_auth("slack", slack_cfg["url"], oauth)
        async with create_mcp_http_client(auth=auth) as http:
            async with streamable_http_client(slack_cfg["url"], http_client=http) as streams:
                async with ClientSession(streams[0], streams[1]) as session:
                    await session.initialize()
                    yield HermesSlack(session)


@asynccontextmanager
async def slack_session(cfg):
    backend = resolve_backend(cfg)
    if backend == "token":
        token = load_slack_token()
        if not token:
            raise RuntimeError("Jeton Slack introuvable. Lance : approve --init")
        yield SlackWeb(token)
    elif backend == "hermes":
        async with hermes_session(cfg) as slack:
            yield slack
    else:
        yield NoSlack()


async def run_pass(cfg, names, dry, state, max_approvals):
    gl = GitLab(cfg["gitlab_host"])
    errors = 0
    budget = Budget(max_approvals)
    mr_url_re = make_mr_url_re(cfg["gitlab_host"])
    async with slack_session(cfg) as slack:
        if not cfg["me"].get("slack_id"):
            cfg["me"]["slack_id"] = await slack.whoami()
        started = time.time()
        since = state.get("recent_scan")
        recent = None
        if since:
            try:
                recent = await slack.search_recent(since - SCAN_MARGIN_SECONDS, mr_url_re)
            except Exception as exc:
                log(f"  balayage Slack récent en échec ({describe(exc)}) : recherche MR par MR", "warn")
        results = {}

        async def one(name):
            results[name] = await process_person(
                name, cfg["people"][name], cfg, gl, slack, state, dry, budget, recent)

        await run_all(one(name) for name in names)
        parts = []
        for name in names:
            s = results[name]
            errors += s["errors"]
            parts.append(
                f"{name}: {s['open']} ouvertes, {s['approved'] + s['would_approve']} "
                f"approuvées, {s['already']} déjà, {s['gated']} sans annonce, "
                f"{s['reacted'] + s['would_react']} réactions"
                + (f" dont {s['reapproved']} ré-approuvées" if s["reapproved"] else "")
                + (f", {s['do_not_merge']} DO NOT MERGE" if s["do_not_merge"] else "")
                + (f", {s['not_allowed']} non approuvables" if s["not_allowed"] else "")
                + (f", {s['errors']} erreurs" if s["errors"] else "")
            )
        if recent is not None or not since:
            state["recent_scan"] = started
        log(" | ".join(parts) + (" [simulation]" if dry else ""), "pass")
    return errors


def mode_text(person):
    return "seulement les MR annoncées sur Slack" if person["require_announcement"] else "toutes ses MR ouvertes"


def resolve_people(tokens, cfg):
    gl = GitLab(cfg["gitlab_host"])
    resolved = {}
    known = cfg.get("people", {})
    by_id = {p["gitlab_id"]: p for p in known.values() if "gitlab_id" in p}
    for token in tokens:
        name, _, flag = token.partition(":")
        if flag not in ("", "all", "announced"):
            sys.exit(f"{token}: suffixe inconnu (':all' ou ':announced')")
        if name in known and not flag:
            resolved[name] = known[name]
            continue
        users = [u for u in gl.api(f"users?search={name}&active=true&per_page=20") if u["state"] == "active"]
        exact = [u for u in users if u["username"] == name]
        users = exact or users
        if len(users) != 1:
            lines = "\n".join(f"  {u['username']} ({u['name']})" for u in users) or "  aucun"
            sys.exit(f"{name}: {len(users)} correspondances, précise le username :\n{lines}")
        inherited = by_id.get(users[0]["id"])
        require = inherited["require_announcement"] if inherited else True
        if flag == "all":
            require = False
        elif flag == "announced":
            require = True
        resolved[name] = {"gitlab_id": users[0]["id"], "require_announcement": require, "label": users[0]["name"]}
    return resolved


def contains(exc, kind):
    if isinstance(exc, kind):
        return True
    return isinstance(exc, BaseExceptionGroup) and any(contains(e, kind) for e in exc.exceptions)


def is_interrupt(exc):
    return contains(exc, KeyboardInterrupt)


def stop_requested(exc):
    return INTERRUPTED.is_set() or is_interrupt(exc)


def request_stop(signum, frame):
    INTERRUPTED.set()
    raise KeyboardInterrupt


def install_stop_handlers():
    signal.signal(signal.SIGINT, request_stop)
    signal.signal(signal.SIGTERM, request_stop)


def describe(exc):
    if isinstance(exc, BaseExceptionGroup):
        return "; ".join(describe(e) for e in exc.exceptions)
    return f"{type(exc).__name__}: {exc}"


def lock_held():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    with LOCK_PATH.open("a+") as handle:
        try:
            fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return True
        return False


def read_lock_pid():
    try:
        return int(LOCK_PATH.read_text().strip())
    except (OSError, ValueError):
        return None


def pid_is_approve(pid):
    out = subprocess.run(["ps", "-ww", "-p", str(pid), "-o", "command="], capture_output=True, text=True).stdout
    return "approve.py" in out


def running_pid():
    pid = read_lock_pid()
    if pid and pid_is_approve(pid):
        return pid
    listing = subprocess.run(["ps", "-wwaxo", "pid=,command="], capture_output=True, text=True).stdout
    skip = ("--stop", "--status", "--why", "--doctor", "--init", "--pause", "--resume")
    others = [int(line.split(None, 1)[0]) for line in listing.splitlines()
              if "approve.py" in line and not any(flag in line for flag in skip)]
    others = [other for other in others if other != os.getpid()]
    return others[0] if len(others) == 1 else None


def cmd_stop(wait_seconds=STOP_WAIT_SECONDS):
    if not lock_held():
        print("Aucune instance en cours.")
        return 0
    pid = running_pid()
    if pid is None:
        print("Une instance tourne mais son numéro de processus est introuvable.")
        print("Arrête-la avec Ctrl-C dans le terminal où elle a été lancée.")
        return 1
    print(f"Arrêt de l'instance {pid}…")
    try:
        os.kill(pid, signal.SIGTERM)
    except ProcessLookupError:
        pass
    deadline = time.time() + wait_seconds
    while time.time() < deadline:
        if not lock_held():
            print("Instance arrêtée.")
            return 0
        time.sleep(0.2)
    print(f"L'instance {pid} ne s'est pas arrêtée en {wait_seconds} s. Réessaie, ou : kill -9 {pid}")
    return 1


def set_pause(on):
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    if on:
        PAUSE_PATH.touch()
    else:
        PAUSE_PATH.unlink(missing_ok=True)


def read_journal():
    try:
        lines = JOURNAL_PATH.read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            rows.append(json.loads(line))
        except ValueError:
            continue
    return rows


def status_report(now=None):
    now = now or datetime.now()
    since = now.timestamp() - STATUS_WINDOW_HOURS * 3600
    recent = [r for r in read_journal() if datetime.fromisoformat(r["at"]).timestamp() >= since]
    live = [r for r in recent if not r.get("dry")]
    count = lambda event, rows=live: sum(1 for r in rows if r["event"] == event)
    passes = [r for r in read_journal() if r["event"] in ("pass", "pass_failed")]
    last = passes[-1] if passes else None
    lines = [
        f"approve : {'en cours' + (f' (PID {read_lock_pid()})' if read_lock_pid() else '') if lock_held() else 'arrêté'}",
        f"pause : {'oui' if PAUSE_PATH.exists() else 'non'}",
        f"dernière passe : {last['at'] if last else 'aucune'}"
        + ("" if not last else " (échec)" if last["event"] == "pass_failed"
           else f" ({last['errors']} erreur(s))"),
        f"{STATUS_WINDOW_HOURS} dernières heures : {count('approval')} approbations, "
        f"{count('reaction_added')} réactions, {count('error', recent) + count('pass_failed', recent)} erreurs",
    ]
    if count("cap_reached", recent):
        lines.append("plafond d'approbations atteint : relancer avec --max-approvals N")
    return "\n".join(lines)


def acquire_lock():
    STATE_DIR.mkdir(parents=True, exist_ok=True)
    handle = LOCK_PATH.open("a+")
    try:
        fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        handle.close()
        sys.exit("approve tourne déjà (verrou pris), une seule instance à la fois.\nPour l'arrêter : approve --stop")
    handle.seek(0)
    handle.truncate()
    handle.write(str(os.getpid()))
    handle.flush()
    return handle


def with_defaults(raw):
    merged = {**copy.deepcopy(DEFAULT_CONFIG), **raw}
    merged["me"] = dict(raw.get("me", {}))
    return merged


def validate_config(cfg):
    problems = []
    host = cfg.get("gitlab_host")
    if not host or host in PLACEHOLDERS:
        problems.append("gitlab_host : indique le serveur GitLab (ex. gitlab.macompagnie.com)")
    me = cfg.get("me", {})
    gitlab_id = me.get("gitlab_id")
    if not isinstance(gitlab_id, int) or isinstance(gitlab_id, bool) or gitlab_id <= 0:
        problems.append("me.gitlab_id : ton identifiant numérique GitLab (approve --init le trouve)")
    slack_id = me.get("slack_id")
    if slack_id is not None and (
        not isinstance(slack_id, str) or slack_id in PLACEHOLDERS or not re.fullmatch(r"[UW][A-Z0-9]{6,}", slack_id)
    ):
        problems.append("me.slack_id : ID de membre Slack de la forme U0123ABCD (ou supprime la ligne, il se déduit du jeton)")
    return problems


def auto_config(host=None):
    hosts = [host] if host else detect_glab_hosts()
    if len(hosts) != 1:
        return None
    try:
        me = GitLab(hosts[0]).api("user")
    except RuntimeError:
        return None
    return with_defaults({"gitlab_host": hosts[0], "me": {"gitlab_id": me["id"]}})


def load_config(host=None):
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        cfg = auto_config(host)
        if cfg:
            return cfg, True
        sys.exit(
            f"Pas de configuration ({CONFIG_PATH}) et le serveur GitLab n'a pas pu être deviné.\n"
            "Lance : approve --init   (ou précise --host <serveur> si glab est connecté à plusieurs)"
        )
    except ValueError as exc:
        sys.exit(f"{CONFIG_PATH} n'est pas un JSON valide : {exc}")
    cfg = with_defaults(raw)
    problems = validate_config(cfg)
    if problems:
        listing = "\n".join(f"  - {p}" for p in problems)
        sys.exit(f"Configuration incomplète ({CONFIG_PATH}) :\n{listing}\nLance : approve --doctor")
    return cfg, False


def gitlab_only_refusal(people):
    gated = [name for name, person in people.items() if person["require_announcement"]]
    if not gated:
        return None
    return (
        "Sans accès Slack, l'outil ne peut pas savoir quelles MR sont annoncées.\n"
        "Pour approuver TOUTES les MR ouvertes de la personne, écris : "
        + " ".join(f"approve {name}:all" for name in gated)
        + "\nPour le mode « annoncées sur Slack » et les réactions : approve --init"
    )


def detect_hosts(text):
    return sorted(set(re.findall(r"Logged in to (\S+) as", text)))


def detect_glab_hosts():
    if not shutil.which("glab"):
        return []
    proc = subprocess.run(["glab", "auth", "status"], capture_output=True, text=True)
    return detect_hosts(proc.stdout + proc.stderr)


def glab_hosts():
    if not shutil.which("glab"):
        sys.exit("glab n'est pas installé. Installe-le (brew install glab), puis : glab auth login")
    return detect_glab_hosts()


def choose_host(hosts, ask=input, interactive=None):
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if not hosts:
        sys.exit("Aucun serveur GitLab connecté dans glab. Lance : glab auth login --hostname <ton-gitlab>")
    if len(hosts) == 1:
        return hosts[0]
    if not interactive:
        sys.exit("Plusieurs serveurs GitLab connectés, précise --host <serveur> :\n  " + "\n  ".join(hosts))
    for number, host in enumerate(hosts, 1):
        print(f"  {number}. {host}")
    answer = ask("Numéro du serveur GitLab à utiliser : ").strip()
    if not answer.isdigit() or not 1 <= int(answer) <= len(hosts):
        sys.exit("Choix invalide.")
    return hosts[int(answer) - 1]


async def fetch_slack_id(cfg=None):
    async with slack_session(cfg or with_defaults({})) as slack:
        return await slack.whoami()


def ask_excluded(interactive=None, ask=input):
    interactive = sys.stdin.isatty() if interactive is None else interactive
    if not interactive:
        return []
    answer = ask(
        "Canaux Slack où ne JAMAIS réagir, noms séparés par des virgules (Entrée = aucun) : "
    )
    return [name.strip().lstrip("#") for name in answer.split(",") if name.strip()]


def build_config(host, gitlab_id, slack_id, excluded, backend="auto"):
    defaults = copy.deepcopy(DEFAULT_CONFIG)
    defaults["exclude_channels"] = excluded
    defaults["slack_backend"] = backend
    me = {"gitlab_id": gitlab_id}
    if slack_id:
        me["slack_id"] = slack_id
    return {"gitlab_host": host, "me": me, **defaults}


def write_config(cfg, path, force=False):
    if path.exists() and not force:
        raise FileExistsError(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(cfg, indent=2, ensure_ascii=False) + "\n")
    path.chmod(0o600)


def slack_token_help():
    manifest = Path(__file__).resolve().parent / "slack-app-manifest.json"
    return f"""
Pour lire et réagir aux messages Slack, l'outil a besoin d'un jeton à toi (une fois pour toutes) :
  1. Ouvre https://api.slack.com/apps puis « Create New App » > « From an app manifest ».
  2. Choisis ton espace de travail et colle le contenu de ce fichier :
       {manifest}
  3. Clique « Install to Workspace » (ton administrateur Slack peut devoir l'approuver).
  4. Menu « OAuth & Permissions » : copie le « User OAuth Token » (il commence par xoxp-).
"""


def prompt_token(ask=getpass.getpass):
    return ask("Colle ton jeton Slack (rien ne s'affiche pendant la saisie) : ").strip()


def check_token_scopes(slack):
    if slack.scopes is None:
        return []
    return [scope for scope in REQUIRED_SCOPES if scope not in slack.scopes]


def init_slack_token(interactive, ask_token=prompt_token):
    token = load_slack_token()
    from_env = bool(os.environ.get(TOKEN_ENV, "").strip())
    if not token:
        if not interactive:
            return None, "Aucun jeton Slack. Lance approve --init dans un terminal, ou définis " + TOKEN_ENV
        print(slack_token_help())
        token = ask_token()
        if not token:
            return None, "Aucun jeton saisi."
    slack = SlackWeb(token)
    try:
        slack_id = asyncio.run(slack.whoami())
    except Exception as exc:
        return None, f"Ce jeton ne fonctionne pas : {describe(exc)}"
    missing = check_token_scopes(slack)
    if missing:
        return None, ("Le jeton n'a pas tous les droits. Il manque : " + ", ".join(missing)
                      + "\nAjoute-les dans « OAuth & Permissions » (User Token Scopes), réinstalle l'app et recopie le jeton.")
    if not from_env:
        save_slack_token(token)
    return slack_id, None


def cmd_init(args):
    if CONFIG_PATH.exists() and not args.force:
        print(f"Une configuration existe déjà : {CONFIG_PATH}")
        print("Relance avec --force pour la remplacer (elle sera écrasée).")
        return 1
    host = args.host or choose_host(glab_hosts())
    try:
        me = GitLab(host).api("user")
    except RuntimeError as exc:
        print(f"glab ne peut pas se connecter à {host} : {exc}")
        print(f"Lance : glab auth login --hostname {host}")
        return 1
    print(f"GitLab : {host}, connecté en tant que {me['username']} (id {me['id']})")

    interactive = sys.stdin.isatty()
    use_hermes = args.hermes or (not args.token and not load_slack_token() and hermes_available())
    backend = "hermes" if use_hermes else "auto"
    slack_id = args.slack_id
    if use_hermes:
        if not args.hermes:
            print("Hermes détecté : j'utilise ton autorisation Slack existante (pas de jeton à créer).")
        ensure_hermes_python()
        if not slack_id:
            try:
                slack_id = asyncio.run(fetch_slack_id(with_defaults({"slack_backend": "hermes"})))
            except Exception as exc:
                print(f"Impossible de lire ton ID Slack via Hermes : {describe(exc)}")
                print("Donne-le à la main : approve --init --hermes --slack-id U0123ABCD")
                return 1
    else:
        slack_id, error = init_slack_token(interactive)
        if error:
            print(error)
            return 1
    print(f"Slack : connecté, ton ID de membre est {slack_id}")
    cfg = build_config(host, me["id"], slack_id, ask_excluded(), backend)
    write_config(cfg, CONFIG_PATH, force=True)
    print(f"\nConfiguration écrite : {CONFIG_PATH}")
    print("Étape suivante : approve --doctor")
    return 0


def check_python():
    ok = sys.version_info >= MIN_PYTHON
    version = ".".join(str(n) for n in sys.version_info[:3])
    return Check(ok, "Python 3.11 ou plus", version, "Installe Python 3.11+ (brew install python)")


def check_config(host=None):
    try:
        raw = json.loads(CONFIG_PATH.read_text())
    except FileNotFoundError:
        cfg = auto_config(host)
        if cfg:
            return Check(True, "Configuration", f"aucun fichier : détectée depuis glab ({cfg['gitlab_host']})", ""), cfg
        return Check(False, "Configuration", f"{CONFIG_PATH} absent",
                     "Lance : approve --init (ou approve --init --host <serveur> si plusieurs)"), None
    except ValueError as exc:
        return Check(False, "Configuration", f"JSON invalide : {exc}", f"Corrige {CONFIG_PATH} ou relance approve --init --force"), None
    cfg = with_defaults(raw)
    problems = validate_config(cfg)
    if problems:
        return Check(False, "Configuration", "; ".join(problems), f"Édite {CONFIG_PATH}"), None
    return Check(True, "Configuration", str(CONFIG_PATH), ""), cfg


def check_glab(cfg):
    if not shutil.which("glab"):
        return Check(False, "glab installé", "introuvable", "brew install glab")
    host = cfg["gitlab_host"]
    try:
        me = GitLab(host).api("user")
    except RuntimeError as exc:
        return Check(False, f"glab connecté à {host}", str(exc)[:120], f"glab auth login --hostname {host}")
    if me["id"] != cfg["me"]["gitlab_id"]:
        return Check(False, f"glab connecté à {host}", f"compte {me['username']} (id {me['id']}) ≠ me.gitlab_id {cfg['me']['gitlab_id']}",
                     "Corrige me.gitlab_id ou relance approve --init --force")
    return Check(True, f"glab connecté à {host}", f"compte {me['username']}", "")


def check_hermes():
    checks = []
    agent = HERMES / "hermes-agent"
    checks.append(Check(agent.is_dir(), "Hermes installé", str(agent),
                        "Installe Hermes, ou définis HERMES_HOME si ce n'est pas ~/.hermes"))
    try:
        import mcp  # noqa: F401
        from tools import mcp_oauth  # noqa: F401
        importable, detail = True, "modules mcp et mcp_oauth trouvés"
    except ImportError as exc:
        importable, detail = False, str(exc)
    checks.append(Check(importable, "Python de Hermes utilisé", detail,
                        "Lance approve via bin/approve (il choisit le Python de Hermes)"))
    load_env()
    checks.append(Check("SLACK_MCP_CLIENT_ID" in os.environ, "SLACK_MCP_CLIENT_ID défini",
                        str(HERMES / ".env"), "Ajoute SLACK_MCP_CLIENT_ID=... dans ~/.hermes/.env"))
    token = HERMES / "mcp-tokens" / "slack.json"
    checks.append(Check(token.is_file(), "Slack autorisé dans Hermes", str(token),
                        "Autorise Slack dans Hermes (connexion MCP Slack), puis relance"))
    return checks


async def check_slack(cfg):
    try:
        found = await fetch_slack_id(cfg)
    except Exception as exc:
        return Check(False, "Slack répond", describe(exc)[:160], "Ré-autorise Slack dans Hermes")
    expected = cfg["me"].get("slack_id")
    if expected and found != expected:
        return Check(False, "Slack répond", f"connecté en {found} ≠ me.slack_id {expected}",
                     "Corrige me.slack_id ou relance approve --init --force")
    return Check(True, "Slack répond", f"connecté en {found}", "")


def check_token_present():
    where = "variable " + TOKEN_ENV if os.environ.get(TOKEN_ENV, "").strip() else str(TOKEN_PATH)
    return Check(bool(load_slack_token()), "Jeton Slack présent", where, "Lance : approve --init")


async def check_slack_token(cfg):
    slack = SlackWeb(load_slack_token())
    try:
        found = await slack.whoami()
    except Exception as exc:
        return Check(False, "Slack accepte le jeton", describe(exc)[:160], "Relance approve --init pour saisir un nouveau jeton")
    missing = check_token_scopes(slack)
    if missing:
        return Check(False, "Droits du jeton Slack", "manque : " + ", ".join(missing),
                     "Ajoute-les (User Token Scopes), réinstalle l'app Slack, puis approve --init --force")
    expected = cfg["me"].get("slack_id")
    if expected and found != expected:
        return Check(False, "Slack accepte le jeton", f"connecté en {found} ≠ me.slack_id {expected}",
                     "Supprime me.slack_id de la config (il se déduit du jeton) ou relance approve --init --force")
    return Check(True, "Slack accepte le jeton", f"connecté en {found}, droits complets", "")


def print_check(check):
    print(f"  {check_mark(check.ok)} {check.label}" + (f" — {check.detail}" if check.detail else ""))
    if not check.ok:
        print(f"      → {check.hint}")


def cmd_doctor(host=None):
    heading("Vérification de l'installation", "Vérification de l'installation")
    print()
    results = [check_python()]
    notes = []
    config_check, cfg = check_config(host)
    results.append(config_check)
    if cfg:
        results.append(check_glab(cfg))
    backend = resolve_backend(cfg) if cfg else None
    if cfg and backend == "token":
        presence = check_token_present()
        results.append(presence)
        if presence.ok:
            results.append(asyncio.run(check_slack_token(cfg)))
    elif cfg and backend == "hermes":
        ensure_hermes_python()
        hermes = check_hermes()
        results.extend(hermes)
        if all(c.ok for c in hermes):
            results.append(asyncio.run(check_slack(cfg)))
    elif cfg:
        results.append(Check(True, "Accès Slack", "non configuré : mode GitLab seul (approve username:all)", ""))
        notes.append("Pour les réactions et le mode « annoncées sur Slack » : approve --init")
    for check in results:
        print_check(check)
    for note in notes:
        print(f"  ℹ {note}")
    failed = sum(1 for c in results if not c.ok)
    if failed:
        print(f"\n{failed} problème(s) à corriger.")
        return 1
    print("\nTout est prêt. Essaie : approve <username>:all --dry-run --once")
    return 0


def load_watch():
    try:
        data = json.loads(WATCH_PATH.read_text())
    except (OSError, ValueError):
        return []
    return [t for t in data.get("people", []) if isinstance(t, str) and t]


def save_watch(tokens):
    WATCH_PATH.parent.mkdir(parents=True, exist_ok=True)
    WATCH_PATH.write_text(json.dumps({"people": tokens}, indent=2, ensure_ascii=False) + "\n")


def token_name(token):
    return token.partition(":")[0]


def merge_watch(current, additions):
    names = {token_name(t): i for i, t in enumerate(current)}
    merged = list(current)
    for token in additions:
        if token_name(token) in names:
            merged[names[token_name(token)]] = token
        else:
            names[token_name(token)] = len(merged)
            merged.append(token)
    return merged


def shortcut_people():
    try:
        return json.loads(CONFIG_PATH.read_text()).get("people", {})
    except (OSError, ValueError):
        return {}


def offline_mode(token, shortcuts):
    name, _, flag = token.partition(":")
    if flag == "all":
        return "toutes ses MR ouvertes"
    if flag == "announced":
        return "seulement les MR annoncées sur Slack"
    person = shortcuts.get(name)
    return mode_text(person) if person else "seulement les MR annoncées sur Slack"


def print_watch(tokens):
    if not tokens:
        print("La liste est vide. Ajoute quelqu'un : approve add <username>")
        return
    shortcuts = shortcut_people()
    print("Personnes suivies :")
    for token in tokens:
        print(f"  {token_name(token):<24} {offline_mode(token, shortcuts)}")


def cmd_watch(sub, tokens, host=None):
    current = load_watch()
    if sub == "list":
        print_watch(current)
        return 0
    if not tokens:
        print(f"approve {sub} : indique au moins un username.")
        return 1
    if sub == "add":
        cfg, _ = load_config(host)
        resolve_people(tokens, cfg)
        current = merge_watch(current, tokens)
        save_watch(current)
    else:
        names = {token_name(t) for t in tokens}
        missing = sorted(names - {token_name(t) for t in current})
        current = [t for t in current if token_name(t) not in names]
        save_watch(current)
        for name in missing:
            print(f"{name} n'était pas dans la liste.")
    print_watch(current)
    return 0


WHY_ORDER = ("todo", "unannounced", "notallowed", "dnm", "draft", "done")
WHY_LABELS = {
    "todo": ("→", "à approuver"),
    "unannounced": ("○", "non annoncée"),
    "notallowed": ("✗", "non approuvable"),
    "dnm": ("✗", "DO NOT MERGE"),
    "draft": ("·", "brouillon"),
    "done": ("✓", "déjà approuvée"),
}


async def explain_person(person, cfg, gl, slack, state, recent):
    me = cfg["me"]
    mr_url_re = make_mr_url_re(cfg["gitlab_host"])
    now = time.time()
    scan_ok = recent is not None
    recent = recent or []
    mrs = await asyncio.to_thread(gl.open_mrs, person["gitlab_id"])
    rows = []
    gate = asyncio.Semaphore(GITLAB_CONCURRENCY)

    async def judge(mr):
        async with gate:
            key = mr_key(mr)

            def row(kind, reason=""):
                rows.append({"key": key, "title": mr.get("title", ""), "kind": kind, "reason": reason})

            if mr.get("draft"):
                return row("draft", "les brouillons ne sont jamais approuvés")
            if DO_NOT_MERGE.search(mr.get("title", "")):
                return row("dnm", "le titre contient « DO NOT MERGE »")
            if person["require_announcement"]:
                if isinstance(slack, NoSlack):
                    return row("unannounced", "Slack n'est pas branché : écris nom:all pour tout approuver")
                entry = state["mrs"].setdefault(key, {})
                apply_recent(entry, mr["web_url"], recent)
                try:
                    if needs_discovery(entry, person, cfg, now, scan_ok):
                        await discover(slack, mr, entry, mr_url_re, now, search_due(key, now, cfg))
                except RuntimeError as exc:
                    return row("unannounced", f"recherche Slack impossible ({exc})")
                if not entry.get("messages"):
                    return row("unannounced", "aucun lien trouvé dans Slack (mode « annoncées »)")
            approvers, can_approve = await asyncio.to_thread(gl.approval_info, mr["project_id"], mr["iid"])
            if me["gitlab_id"] in approvers:
                return row("done")
            if not can_approve:
                return row("notallowed", "tu n'es pas approbateur autorisé de cette MR")
            row("todo", "sera approuvée à la prochaine passe")

    await run_all(judge(mr) for mr in mrs)
    return sorted(rows, key=lambda r: (WHY_ORDER.index(r["kind"]), r["key"]))


def print_why(name, person, rows):
    print(f"\n{name} : {mode_text(person)}, {len(rows)} MR ouvertes")
    for row in rows:
        if row["kind"] == "done":
            continue
        icon, label = WHY_LABELS[row["kind"]]
        reason = f"  ({row['reason']})" if row["reason"] else ""
        print(f"  {icon} {label:<16} {row['key']:<10} {row['title'][:56]}{reason}")
    done = sum(1 for row in rows if row["kind"] == "done")
    if done:
        print(f"  ✓ {done} déjà approuvée(s)")


async def why_pass(cfg, names):
    gl = GitLab(cfg["gitlab_host"])
    state = copy.deepcopy(load_state())
    mr_url_re = make_mr_url_re(cfg["gitlab_host"])
    async with slack_session(cfg) as slack:
        recent = None
        if state.get("recent_scan"):
            try:
                recent = await slack.search_recent(state["recent_scan"] - SCAN_MARGIN_SECONDS, mr_url_re)
            except Exception:
                recent = None
        results = {}

        async def one(name):
            results[name] = await explain_person(cfg["people"][name], cfg, gl, slack, state, recent)

        await run_all(one(name) for name in names)
    for name in names:
        print_why(name, cfg["people"][name], results[name])
    return 0


def repo_root():
    return Path(__file__).resolve().parent


def git(root, *args):
    return subprocess.run(["git", "-C", str(root), *args], capture_output=True, text=True)


def version_text():
    root = repo_root()
    if not (root / ".git").exists() or not shutil.which("git"):
        return "approve (version inconnue : pas un clone Git)"
    sha = git(root, "rev-parse", "--short", "HEAD").stdout.strip()
    day = git(root, "log", "-1", "--format=%cs").stdout.strip()
    return f"approve {sha} ({day})"


def cmd_update():
    root = repo_root()
    if not (root / ".git").exists() or not shutil.which("git"):
        print("Cette installation n'est pas un clone Git : mets-la à jour comme tu l'as installée.")
        return 1
    branch = git(root, "rev-parse", "--abbrev-ref", "HEAD").stdout.strip()
    if branch != "main":
        print(f"Tu es sur la branche « {branch} », pas sur main. Reviens dessus avec : git -C {root} checkout main")
        return 1
    if git(root, "status", "--porcelain").stdout.strip():
        print(f"Tu as des modifications locales dans {root} : je ne touche à rien. Commite-les ou annule-les d'abord.")
        return 1
    fetched = git(root, "fetch", "origin", "main")
    if fetched.returncode != 0:
        print("Impossible de joindre GitHub :", (fetched.stderr or fetched.stdout).strip()[:200])
        return 1
    behind = git(root, "rev-list", "--count", "HEAD..origin/main").stdout.strip() or "0"
    if int(behind) == 0:
        print(f"Déjà à jour : {version_text()}")
        return 0
    news = git(root, "log", "--format=  %h %s", "HEAD..origin/main").stdout.rstrip()
    print(f"{behind} nouveauté(s) :\n{news}")
    merged = git(root, "merge", "--ff-only", "origin/main")
    if merged.returncode != 0:
        print("Mise à jour impossible sans conflit (ta copie a des commits que GitHub n'a pas) :", merged.stderr.strip()[:200])
        return 1
    print(f"\nMise à jour faite : {version_text()}")
    if lock_held():
        print("Une instance tourne encore avec l'ancien code : approve --stop, puis approve.")
    return 0


def build_parser(interval_default=None):
    parser = argparse.ArgumentParser(
        prog="approve",
        description="Approuve les merge requests GitLab de collègues et réagit à leur annonce Slack.",
        epilog="Premiers pas : approve --init, puis approve --doctor, puis approve <username> --dry-run --once",
    )
    parser.add_argument("people", nargs="*", metavar="username",
                        help="username GitLab (ou raccourci de la config) ; 'username:all' = toutes ses MR, même sans annonce Slack ; "
                             "'add|remove|list' gère la liste mémorisée (sans nom, approve utilise cette liste)")
    parser.add_argument("--update", action="store_true", help="met l'outil à jour (git pull sécurisé) et montre les nouveautés")
    parser.add_argument("--version", action="store_true", help="affiche la version installée")
    parser.add_argument("--why", action="store_true", help="explique pourquoi chaque MR est (ou n'est pas) approuvée, sans rien écrire")
    parser.add_argument("--stop", action="store_true", help="arrête proprement l'instance qui tourne, d'où qu'elle ait été lancée")
    parser.add_argument("--init", action="store_true", help="crée la configuration en la détectant")
    parser.add_argument("--doctor", action="store_true", help="vérifie l'installation et dit quoi corriger")
    parser.add_argument("--status", action="store_true", help="état de l'outil et bilan des 24 dernières heures")
    parser.add_argument("--pause", action="store_true", help="met la boucle en pause")
    parser.add_argument("--resume", action="store_true", help="reprend après une pause")
    parser.add_argument("--dry-run", action="store_true", help="simulation : n'écrit rien")
    parser.add_argument("--once", action="store_true", help="une seule passe puis arrêt")
    parser.add_argument("--interval", type=int, help="secondes entre deux passes (défaut : config)")
    parser.add_argument("--max-approvals", type=int, help="plafond d'approbations par passe (défaut : config)")
    parser.add_argument("--host", help="serveur GitLab à utiliser (si glab est connecté à plusieurs)")
    parser.add_argument("--hermes", action="store_true", help="avec --init : utiliser l'autorisation Slack de Hermes (choisi tout seul si Hermes est détecté et qu'il n'y a pas de jeton)")
    parser.add_argument("--token", action="store_true", help="avec --init : utiliser un jeton Slack même si Hermes est installé")
    parser.add_argument("--slack-id", help="avec --init --hermes : ton ID de membre Slack, si la détection échoue")
    parser.add_argument("--force", action="store_true", help="avec --init : remplace la config existante")
    return parser


def main(argv=None):
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.version:
        print(version_text())
        return 0
    if args.update:
        return cmd_update()
    if args.init:
        return cmd_init(args)
    if args.doctor:
        return cmd_doctor(args.host)
    if args.pause or args.resume:
        set_pause(args.pause)
        print("en pause" if args.pause else "reprise")
        return 0
    if args.status:
        heading("Statut")
        print(status_report())
        return 0
    if args.stop:
        return cmd_stop()
    if args.people and args.people[0] in SUBCOMMANDS:
        return cmd_watch(args.people[0], args.people[1:], args.host)
    tokens = args.people or load_watch()
    if not tokens:
        parser.error("indique au moins un username, ou mémorise une liste : approve add <username>")

    install_stop_handlers()
    cfg, auto = load_config(args.host)
    interval = args.interval or cfg["interval_seconds"]
    max_approvals = args.max_approvals or cfg["max_approvals_per_pass"]
    backend = resolve_backend(cfg)
    if backend == "hermes":
        ensure_hermes_python()
    cfg["people"] = resolve_people(tokens, cfg)
    args.people = list(cfg["people"])
    if args.why:
        return asyncio.run(why_pass(cfg, args.people))
    if backend is None:
        refusal = gitlab_only_refusal(cfg["people"])
        if refusal:
            sys.exit(refusal)
    lock = acquire_lock()
    state = load_state()
    failures, notified, exit_code = 0, False, 0
    mode = "simulation (aucune écriture)" if args.dry_run else "LIVE (approbations + réactions)"
    print_banner()
    if auto:
        log(f"Aucune config : serveur {cfg['gitlab_host']} et compte détectés depuis glab")
    if backend is None:
        log(GITLAB_ONLY_NOTE)
    for name, person in cfg["people"].items():
        log(f"  {name} : {mode_text(person)}")
    log(f"approve {' '.join(args.people)} — {mode} — toutes les {interval}s, "
        f"plafond {max_approvals} approbations/passe — Ctrl-C pour arrêter")
    try:
        while True:
            if PAUSE_PATH.exists():
                log("en pause (approve --resume pour reprendre)", "warn")
                while PAUSE_PATH.exists():
                    time.sleep(PAUSE_POLL_SECONDS)
                log("reprise")
            try:
                errors = asyncio.run(run_pass(cfg, args.people, args.dry_run, state, max_approvals))
                save_state(state)
                failed = errors > 0
                journal("pass", errors=errors, people=args.people, dry=args.dry_run)
            except BaseException as exc:
                if stop_requested(exc):
                    raise KeyboardInterrupt from None
                if contains(exc, ApprovalCapReached):
                    save_state(state)
                    journal("cap_reached", cap=max_approvals, dry=args.dry_run)
                    log(f"plafond de {max_approvals} approbations atteint dans une passe : arrêt. "
                        "Vérifie le journal, puis relance avec --max-approvals N si c'est voulu.", "warn")
                    notify("approve", f"plafond de {max_approvals} approbations atteint, arrêté")
                    exit_code = EXIT_CAP_REACHED
                    break
                if not isinstance(exc, Exception):
                    raise
                failed = True
                log(f"passe en échec: {describe(exc)}", "err")
                journal("pass_failed", error=describe(exc))
            failures = failures + 1 if failed else 0
            if failures >= FAILURES_BEFORE_NOTIFY and not notified:
                notify("approve", f"{failures} passes en échec d'affilée, voir la console")
                notified = True
            if not failed:
                notified = False
            if args.once:
                break
            time.sleep(interval)
    except KeyboardInterrupt:
        save_state(state)
        log("arrêté.")
    finally:
        lock.close()
    return exit_code


def cli():
    try:
        return main()
    except KeyboardInterrupt:
        print("\narrêté.", flush=True)
        return EXIT_INTERRUPTED


if __name__ == "__main__":
    sys.exit(cli())
