import re

import pytest

import approve

HOST = "gitlab.test"
PATH = "grp/proj"
PROJECT_ID = 88
ME_GITLAB = 1000
ME_SLACK = "UME"
ALICE = 1001
BOB = 1002
URL_RE = re.compile(r"(https://" + re.escape(HOST) + r"/[\w.\-/]+?/-/merge_requests/\d+)")


def mr_url(iid):
    return f"https://{HOST}/{PATH}/-/merge_requests/{iid}"


PEOPLE = {ALICE: ("alice", "Alice Example"), BOB: ("bob", "Bob Example"), 999: ("other", "Other Person")}


def author_of(user_id):
    username, name = PEOPLE.get(user_id, (f"user{user_id}", f"User {user_id}"))
    return {"id": user_id, "username": username, "name": name}


def make_mr(iid, author=ALICE, state="opened", draft=False, sha=None):
    return {
        "project_id": PROJECT_ID,
        "iid": iid,
        "state": state,
        "draft": draft,
        "sha": sha or f"sha{iid}",
        "title": f"MR {iid}",
        "web_url": mr_url(iid),
        "author": author_of(author),
    }


def make_msg(iids, channel="front", channel_id="C1", ts="1.1", poster="alice"):
    full = {name[0]: name[1] for name in PEOPLE.values()}.get(poster, poster.title())
    return {
        "channel": channel,
        "channel_id": channel_id,
        "ts": ts,
        "urls": [mr_url(i) for i in iids],
        "poster_id": f"U0{poster.upper()}01",
        "poster_name": full,
        "poster_email": f"{poster}@example.com",
        "poster_handle": poster,
    }


class FakeGitLab:
    def __init__(self, mrs, approved_by_me=(), users=None, cannot_approve=()):
        self.calls = []
        self.cannot_approve = set(cannot_approve)
        self.mrs = {m["iid"]: m for m in mrs}
        self.approvals = {i: {ME_GITLAB} for i in approved_by_me}
        self.users = users or []
        self.posts = []

    def _iid(self, iid):
        return int(iid)

    def open_mrs(self, author_id):
        self.calls.append("open_mrs")
        return [
            m for m in self.mrs.values()
            if m["state"] == "opened" and m["author"]["id"] == author_id
        ]

    def mr(self, project, iid):
        self.calls.append(f"mr:{self._iid(iid)}")
        return self.mrs[self._iid(iid)]

    def approvers(self, project, iid):
        self.calls.append(f"approvers:{self._iid(iid)}")
        if self._iid(iid) not in self.mrs:
            raise RuntimeError("404 Not Found")
        return set(self.approvals.get(self._iid(iid), set()))

    def approval_info(self, project, iid):
        self.calls.append(f"approval_info:{self._iid(iid)}")
        if self._iid(iid) not in self.mrs:
            raise RuntimeError("404 Not Found")
        return set(self.approvals.get(self._iid(iid), set())), self._iid(iid) not in self.cannot_approve

    def approve(self, project, iid, sha):
        self.posts.append((self._iid(iid), sha))
        self.approvals.setdefault(self._iid(iid), set()).add(ME_GITLAB)

    def api(self, path, method="GET", fields=None, paginate=False):
        return self.users


class FakeSlack:
    def __init__(self, announcements=None, fail_search=False, reacted=(), recent=None, fail_recent=False):
        self.recent = recent or []
        self.fail_recent = fail_recent
        self.recent_calls = []
        self.announcements = announcements or {}
        self.fail_search = fail_search
        self.reactions = {key: {ME_SLACK} for key in reacted}
        self.reacts = []
        self.searches = []
        self.reactor_reads = []

    async def whoami(self):
        return ME_SLACK

    async def search_recent(self, since, mr_url_re):
        self.recent_calls.append(since)
        if self.fail_recent:
            raise RuntimeError("slack down")
        return [dict(m) for m in self.recent]

    async def search_mr(self, iid, mr_url_re):
        self.searches.append(iid)
        if self.fail_search:
            raise RuntimeError("slack down")
        return [dict(m) for m in self.announcements.get(iid, [])]

    async def reactors(self, channel_id, ts, emoji):
        self.reactor_reads.append((channel_id, ts))
        return set(self.reactions.get((channel_id, ts), set()))

    async def react(self, channel_id, ts, emoji):
        self.reacts.append((channel_id, ts, emoji))
        self.reactions.setdefault((channel_id, ts), set()).add(ME_SLACK)


@pytest.fixture(autouse=True)
def isolated_journal(tmp_path, monkeypatch):
    monkeypatch.setattr(approve, "JOURNAL_PATH", tmp_path / "journal.jsonl")


@pytest.fixture
def cfg():
    return {
        "gitlab_host": HOST,
        "me": {"gitlab_id": ME_GITLAB, "slack_id": ME_SLACK},
        "emoji": "lgtm",
        "exclude_channels": ["no-reactions"],
        "interval_seconds": 300,
        "rediscovery_minutes": 60,
        "merged_grace_hours": 48,
        "people": {
            "alice": {"gitlab_id": ALICE, "require_announcement": False},
            "bob": {"gitlab_id": BOB, "require_announcement": True},
        },
    }
