import asyncio
import threading
import time
from contextlib import asynccontextmanager

import pytest

import approve
from conftest import ALICE, BOB, ME_GITLAB, ME_SLACK, FakeGitLab, FakeSlack, make_mr, make_msg
from test_parse_search import HUMAN, block, search


def run(cfg, who, gl, slack, state=None, recent=None, dry=False, budget=None):
    state = {"mrs": {}} if state is None else state
    person = cfg["people"][who]
    stats = asyncio.run(approve.process_person(who, person, cfg, gl, slack, state, dry, budget, recent))
    return stats, state


class TestUnchangedMergeRequestsCostNothing:
    def test_second_pass_only_lists_the_merge_requests(self, cfg):
        gl, slack = FakeGitLab([make_mr(1), make_mr(2)]), FakeSlack()
        _, state = run(cfg, "alice", gl, slack)
        gl.calls.clear()
        stats, _ = run(cfg, "alice", gl, slack, state)
        assert gl.calls == ["open_mrs"] and stats["already"] == 2

    def test_a_push_makes_it_checked_again(self, cfg):
        gl, slack = FakeGitLab([make_mr(1)]), FakeSlack()
        _, state = run(cfg, "alice", gl, slack)
        gl.mrs[1]["sha"] = "pushed"
        gl.approvals.clear()
        gl.calls.clear()
        run(cfg, "alice", gl, slack, state)
        assert "approval_info:1" in gl.calls and gl.posts[-1] == (1, "pushed")

    def test_it_is_checked_again_after_the_reverify_delay(self, cfg):
        gl, slack = FakeGitLab([make_mr(1)]), FakeSlack()
        _, state = run(cfg, "alice", gl, slack)
        state["mrs"]["88!1"]["verified_at"] -= 31 * 60
        gl.calls.clear()
        run(cfg, "alice", gl, slack, state)
        assert "approval_info:1" in gl.calls

    def test_a_manually_revoked_approval_is_noticed_at_the_next_verification(self, cfg):
        gl, slack = FakeGitLab([make_mr(1)]), FakeSlack()
        _, state = run(cfg, "alice", gl, slack)
        gl.approvals.clear()
        state["mrs"]["88!1"]["verified_at"] -= 31 * 60
        run(cfg, "alice", gl, slack, state)
        assert len(gl.posts) == 2

    def test_an_already_approved_mr_is_not_read_in_full(self):
        gl = FakeGitLab([make_mr(1)], approved_by_me=[1])
        gl.calls.clear()
        assert approve.approve_mr(gl, make_mr(1), ME_GITLAB, False)[0] == "already"
        assert gl.calls == ["approval_info:1"]

    def test_mrs_i_cannot_approve_are_not_asked_again_until_they_change(self, cfg):
        gl, slack = FakeGitLab([make_mr(1)], cannot_approve=[1]), FakeSlack()
        stats, state = run(cfg, "alice", gl, slack)
        assert stats["not_allowed"] == 1
        gl.calls.clear()
        stats, _ = run(cfg, "alice", gl, slack, state)
        assert gl.calls == ["open_mrs"] and stats["not_allowed"] == 1
        gl.mrs[1]["sha"] = "changed"
        gl.calls.clear()
        run(cfg, "alice", gl, slack, state)
        assert "approval_info:1" in gl.calls


class TestRecentScanReplacesPerMrSearches:
    def test_warm_pass_never_searches_mr_by_mr(self, cfg):
        gl, slack = FakeGitLab([make_mr(1), make_mr(2), make_mr(3)]), FakeSlack()
        _, state = run(cfg, "alice", gl, slack)
        assert sorted(slack.searches) == [1, 2, 3]
        slack.searches.clear()
        run(cfg, "alice", gl, slack, state, recent=[])
        assert slack.searches == []

    def test_a_late_announcement_in_the_scan_gets_its_reaction(self, cfg):
        gl, slack = FakeGitLab([make_mr(1)]), FakeSlack()
        _, state = run(cfg, "alice", gl, slack)
        slack.searches.clear()
        run(cfg, "alice", gl, slack, state, recent=[make_msg([1])])
        assert slack.reacts == [("C1", "1.1", "lgtm")] and slack.searches == []

    def test_a_bundle_announced_in_the_scan_reaches_every_member(self, cfg):
        gl, slack = FakeGitLab([make_mr(1), make_mr(2)]), FakeSlack()
        _, state = run(cfg, "alice", gl, slack)
        run(cfg, "alice", gl, slack, state, recent=[make_msg([1, 2])])
        assert slack.reacts == [("C1", "1.1", "lgtm")]

    def test_a_failed_scan_falls_back_to_searching_each_mr(self, cfg):
        gl, slack = FakeGitLab([make_mr(1), make_mr(2)]), FakeSlack()
        _, state = run(cfg, "alice", gl, slack)
        slack.searches.clear()
        run(cfg, "alice", gl, slack, state, recent=None)
        assert sorted(slack.searches) == [1, 2]

    def test_a_gated_person_is_approved_as_soon_as_the_scan_sees_the_announcement(self, cfg):
        gl, slack = FakeGitLab([make_mr(1, author=BOB)]), FakeSlack()
        stats, state = run(cfg, "bob", gl, slack)
        assert gl.posts == [] and stats["gated"] == 1
        run(cfg, "bob", gl, slack, state, recent=[make_msg([1])])
        assert gl.posts == [(1, "sha1")]


class TestDiscoverySchedule:
    def test_unannounced_mr_with_a_scan_is_left_alone(self, cfg):
        entry = {"last_search": 1000}
        assert approve.needs_discovery(entry, {}, cfg, 1001, scan_ok=True) is False
        assert approve.needs_discovery(entry, {}, cfg, 1001, scan_ok=False) is True

    def test_the_safety_net_search_still_happens_when_due(self, cfg):
        entry = {"last_search": 1000, "search_due": 5000}
        assert approve.needs_discovery(entry, {}, cfg, 4999, scan_ok=True) is False
        assert approve.needs_discovery(entry, {}, cfg, 5000, scan_ok=True) is True

    def test_never_searched_is_always_searched(self, cfg):
        assert approve.needs_discovery({}, {}, cfg, 10, scan_ok=True) is True

    def test_due_dates_are_spread_between_one_and_one_and_a_half_delays(self, cfg):
        delay = cfg["rediscovery_minutes"] * 60
        dues = {approve.search_due(f"88!{i}", 0, cfg) for i in range(200)}
        assert all(delay <= d <= delay * 1.5 for d in dues) and len(dues) > 10

    def test_due_date_is_stable_for_a_given_mr(self, cfg):
        assert approve.search_due("88!7", 100, cfg) == approve.search_due("88!7", 100, cfg)


class TestMergedMrsAreNotPolledForNothing:
    def state(self, **entry):
        base = {"sha": "s", "author_id": ALICE, "last_seen": time.time() - 60, "last_search": time.time()}
        return {"mrs": {"88!1": {**base, **entry}}}

    def test_merged_without_announcement_and_not_due_costs_nothing(self, cfg):
        gl = FakeGitLab([make_mr(1, state="merged")], approved_by_me=[1])
        run(cfg, "alice", gl, FakeSlack(), self.state(), recent=[])
        assert gl.calls == ["open_mrs"]

    def test_merged_is_looked_at_when_its_search_is_due(self, cfg):
        gl = FakeGitLab([make_mr(1, state="merged")], approved_by_me=[1])
        slack = FakeSlack({1: [make_msg([1])]})
        run(cfg, "alice", gl, slack, self.state(search_due=time.time() - 1), recent=[])
        assert slack.reacts == [("C1", "1.1", "lgtm")]

    def test_incomplete_bundle_is_rechecked_later_not_every_pass(self, cfg):
        other = make_mr(2, author=999)
        gl = FakeGitLab([make_mr(1, state="merged"), other], approved_by_me=[1])
        slack = FakeSlack()
        state = self.state(messages=[make_msg([1, 2])])
        run(cfg, "alice", gl, slack, state, recent=[])
        assert slack.reacts == [] and state["mrs"]["88!1"]["recheck_at"] > time.time()
        gl.calls.clear()
        run(cfg, "alice", gl, slack, state, recent=[])
        assert gl.calls == ["open_mrs"]

    def test_a_new_announcement_in_the_scan_reaches_a_settled_merged_mr(self, cfg):
        gl = FakeGitLab([make_mr(1, state="merged")], approved_by_me=[1])
        slack = FakeSlack()
        state = self.state(web_url=approve_mr_url(1))
        run(cfg, "alice", gl, slack, state, recent=[make_msg([1])])
        assert slack.reacts == [("C1", "1.1", "lgtm")]


def approve_mr_url(iid):
    from conftest import mr_url

    return mr_url(iid)


class TestConcurrency:
    def test_cap_holds_exactly_under_parallel_processing(self, cfg):
        gl = FakeGitLab([make_mr(i) for i in range(1, 41)])
        with pytest.raises(approve.ApprovalCapReached):
            run(cfg, "alice", gl, FakeSlack(), budget=approve.Budget(7))
        assert len(gl.posts) == 7

    def test_budget_is_thread_safe(self):
        budget, granted = approve.Budget(10), []

        def worker():
            try:
                budget.take()
                granted.append(1)
            except approve.ApprovalCapReached:
                pass

        threads = [threading.Thread(target=worker) for _ in range(60)]
        [t.start() for t in threads]
        [t.join() for t in threads]
        assert len(granted) == 10

    def test_unwrap_prefers_the_interrupt_then_the_cap(self):
        group = BaseExceptionGroup("g", [ValueError("x"), BaseExceptionGroup("n", [approve.ApprovalCapReached(3)])])
        assert isinstance(approve.unwrap_group(group), approve.ApprovalCapReached)
        both = BaseExceptionGroup("g", [approve.ApprovalCapReached(3), KeyboardInterrupt()])
        assert isinstance(approve.unwrap_group(both), KeyboardInterrupt)
        assert isinstance(approve.unwrap_group(BaseExceptionGroup("g", [ValueError("a")])), ValueError)

    def test_a_reaction_on_a_shared_message_is_read_and_added_once(self, cfg):
        gl = FakeGitLab([make_mr(i) for i in range(1, 9)])
        msg = make_msg(list(range(1, 9)))
        slack = FakeSlack({i: [msg] for i in range(1, 9)})
        run(cfg, "alice", gl, slack)
        assert slack.reacts == [("C1", "1.1", "lgtm")]


class TestRunPass:
    @pytest.fixture
    def patched(self, monkeypatch, cfg):
        holder = {}

        @asynccontextmanager
        async def session(config):
            yield holder["slack"]

        monkeypatch.setattr(approve, "slack_session", session)
        gl = FakeGitLab([make_mr(1), make_mr(2, author=BOB)])
        monkeypatch.setattr(approve, "GitLab", lambda host: gl)
        return holder, gl

    def go(self, cfg, state, names=("alice", "bob")):
        return asyncio.run(approve.run_pass(cfg, list(names), True, state, 30))

    def test_first_run_has_no_scan_and_remembers_the_time(self, cfg, patched):
        holder, _ = patched
        holder["slack"] = FakeSlack()
        state = {"mrs": {}}
        self.go(cfg, state)
        assert holder["slack"].recent_calls == [] and state["recent_scan"] > 0

    def test_later_runs_scan_from_the_last_scan_minus_a_margin(self, cfg, patched):
        holder, _ = patched
        holder["slack"] = FakeSlack()
        state = {"mrs": {}, "recent_scan": 10_000}
        self.go(cfg, state)
        assert holder["slack"].recent_calls == [10_000 - approve.SCAN_MARGIN_SECONDS]
        assert state["recent_scan"] > 10_000

    def test_failed_scan_keeps_the_old_time_so_the_next_scan_is_wider(self, cfg, patched):
        holder, _ = patched
        holder["slack"] = FakeSlack(fail_recent=True)
        state = {"mrs": {}, "recent_scan": 10_000}
        self.go(cfg, state)
        assert state["recent_scan"] == 10_000

    def test_both_people_are_processed(self, cfg, patched, capsys):
        holder, gl = patched
        holder["slack"] = FakeSlack({2: [make_msg([2])]})
        self.go(cfg, {"mrs": {}})
        out = capsys.readouterr().out
        assert "alice:" in out and "bob:" in out


class FakeSession:
    def __init__(self, pages):
        self.pages, self.calls = list(pages), []

    async def call_tool(self, tool, args):
        import json
        from types import SimpleNamespace

        self.calls.append((tool, args))
        text = json.dumps(self.pages.pop(0))
        return SimpleNamespace(content=[SimpleNamespace(text=text)], is_error=False)


class TestHermesRecentScan:
    def page(self, ts, cursor=None):
        results = search(block(1, 1, "front", "C1", HUMAN, ts, "<https://gitlab.test/grp/proj/-/merge_requests/7|!7>"))
        info = f"For the next page use cursor `{cursor}`\n" if cursor else "End of results\n"
        return {"results": results, "pagination_info": info}

    def test_follows_the_cursor_until_the_last_page(self, monkeypatch):
        from conftest import URL_RE

        monkeypatch.setattr(approve, "SLACK_PAUSE", 0)
        session = FakeSession([self.page("2.1", "abc"), self.page("1.1")])
        found = asyncio.run(approve.HermesSlack(session).search_recent(1234.9, URL_RE))
        assert [m["ts"] for m in found] == ["2.1", "1.1"]
        first, second = session.calls[0][1], session.calls[1][1]
        assert first["after"] == "1234" and first["keywords"] == ["merge_requests"] and "cursor" not in first
        assert second["cursor"] == "abc"

    def test_stops_after_the_page_limit(self, monkeypatch):
        from conftest import URL_RE

        monkeypatch.setattr(approve, "SLACK_PAUSE", 0)
        session = FakeSession([self.page(f"{i}.1", "more") for i in range(10)])
        asyncio.run(approve.HermesSlack(session).search_recent(0, URL_RE))
        assert len(session.calls) == approve.SCAN_MAX_PAGES
