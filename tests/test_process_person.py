import asyncio
import json
import time

import approve
from conftest import (
    ALICE, BOB, ME_GITLAB, URL_RE, FakeGitLab, FakeSlack, make_mr, make_msg,
)


def run(cfg, who, gl, slack, state=None, dry=False):
    state = {"mrs": {}} if state is None else state
    person = cfg["people"][who]
    stats = asyncio.run(approve.process_person(who, person, cfg, gl, slack, state, dry))
    return stats, state


def test_ungated_author_is_approved_without_any_announcement(cfg):
    gl, slack = FakeGitLab([make_mr(1)]), FakeSlack()
    stats, _ = run(cfg, "alice", gl, slack)
    assert gl.posts == [(1, "sha1")]
    assert stats["approved"] == 1 and slack.reacts == []


def test_gated_author_without_announcement_is_not_approved(cfg):
    gl, slack = FakeGitLab([make_mr(1, author=BOB)]), FakeSlack()
    stats, _ = run(cfg, "bob", gl, slack)
    assert gl.posts == []
    assert stats["gated"] == 1


def test_gated_author_is_not_approved_when_slack_search_fails(cfg):
    gl, slack = FakeGitLab([make_mr(1, author=BOB)]), FakeSlack(fail_search=True)
    stats, _ = run(cfg, "bob", gl, slack)
    assert gl.posts == []
    assert stats["errors"] == 1


def test_gated_author_with_announcement_is_approved_and_reacted(cfg):
    gl = FakeGitLab([make_mr(1, author=BOB)])
    slack = FakeSlack({1: [make_msg([1], poster="bob")]})
    run(cfg, "bob", gl, slack)
    assert gl.posts == [(1, "sha1")]
    assert slack.reacts == [("C1", "1.1", "lgtm")]


def test_announcement_in_excluded_channel_unlocks_approval_but_gets_no_reaction(cfg):
    gl = FakeGitLab([make_mr(1, author=BOB)])
    slack = FakeSlack({1: [make_msg([1], channel="no-reactions", channel_id="CX", poster="bob")]})
    run(cfg, "bob", gl, slack)
    assert gl.posts == [(1, "sha1")]
    assert slack.reacts == []


def test_announcement_that_does_not_list_the_exact_mr_does_not_count(cfg):
    gl = FakeGitLab([make_mr(1, author=BOB)])
    slack = FakeSlack({1: [make_msg([2])]})
    run(cfg, "bob", gl, slack)
    assert gl.posts == []


def test_bundle_with_an_unapproved_mr_gets_no_reaction(cfg):
    other = make_mr(2, author=999)
    gl = FakeGitLab([make_mr(1), other])
    slack = FakeSlack({1: [make_msg([1, 2])]})
    run(cfg, "alice", gl, slack)
    assert gl.posts == [(1, "sha1")]
    assert slack.reacts == []


def test_bundle_with_every_mr_approved_gets_one_reaction_for_all_members(cfg):
    gl = FakeGitLab([make_mr(1), make_mr(2), make_mr(3)])
    msg = make_msg([1, 2, 3])
    slack = FakeSlack({1: [msg], 2: [msg], 3: [msg]})
    run(cfg, "alice", gl, slack)
    assert len(gl.posts) == 3
    assert slack.reacts == [("C1", "1.1", "lgtm")]
    assert len(slack.reactor_reads) <= 2


def test_existing_reaction_of_mine_is_not_added_twice(cfg):
    gl = FakeGitLab([make_mr(1)])
    slack = FakeSlack({1: [make_msg([1])]}, reacted=[("C1", "1.1")])
    run(cfg, "alice", gl, slack)
    assert slack.reacts == []


def test_someone_elses_lgtm_does_not_count_as_mine(cfg):
    gl = FakeGitLab([make_mr(1)])
    slack = FakeSlack({1: [make_msg([1])]})
    slack.reactions[("C1", "1.1")] = {"USOMEONEELSE"}
    run(cfg, "alice", gl, slack)
    assert slack.reacts == [("C1", "1.1", "lgtm")]


def test_dry_run_writes_nothing(cfg):
    gl = FakeGitLab([make_mr(1)])
    slack = FakeSlack({1: [make_msg([1])]})
    stats, _ = run(cfg, "alice", gl, slack, dry=True)
    assert gl.posts == [] and slack.reacts == []
    assert stats["would_approve"] == 1


def test_drafts_are_never_touched(cfg):
    gl = FakeGitLab([make_mr(1, draft=True)])
    stats, _ = run(cfg, "alice", gl, FakeSlack())
    assert gl.posts == [] and stats["open"] == 0


def test_approval_reset_by_a_push_is_restored_on_the_new_sha(cfg):
    gl = FakeGitLab([make_mr(1, sha="old")])
    slack = FakeSlack()
    run(cfg, "alice", gl, slack)
    gl.mrs[1]["sha"] = "new"
    gl.approvals.clear()
    run(cfg, "alice", gl, slack)
    assert gl.posts == [(1, "old"), (1, "new")]


def test_late_announcement_is_found_on_the_next_pass(cfg):
    gl = FakeGitLab([make_mr(1)])
    slack = FakeSlack()
    _, state = run(cfg, "alice", gl, slack)
    assert slack.reacts == []
    slack.announcements[1] = [make_msg([1])]
    run(cfg, "alice", gl, slack, state=state)
    assert slack.reacts == [("C1", "1.1", "lgtm")]


class TestMergedBeforeAnnouncement:
    def merged_state(self, age_seconds):
        now = time.time()
        return {"mrs": {"88!1": {"sha": "sha1", "author_id": ALICE, "last_seen": now - age_seconds}}}

    def test_reacts_on_the_announcement_of_a_recently_merged_mr(self, cfg):
        gl = FakeGitLab([make_mr(1, state="merged")], approved_by_me=[1])
        slack = FakeSlack({1: [make_msg([1])]})
        run(cfg, "alice", gl, slack, state=self.merged_state(3600))
        assert slack.reacts == [("C1", "1.1", "lgtm")]

    def test_ignores_merged_mr_older_than_the_grace_period(self, cfg):
        gl = FakeGitLab([make_mr(1, state="merged")], approved_by_me=[1])
        slack = FakeSlack({1: [make_msg([1])]})
        run(cfg, "alice", gl, slack, state=self.merged_state(49 * 3600))
        assert slack.reacts == [] and slack.searches == []

    def test_ignores_merged_mr_i_never_approved(self, cfg):
        gl = FakeGitLab([make_mr(1, state="merged")])
        slack = FakeSlack({1: [make_msg([1])]})
        run(cfg, "alice", gl, slack, state=self.merged_state(60))
        assert slack.reacts == []

    def test_ignores_closed_mr(self, cfg):
        gl = FakeGitLab([make_mr(1, state="closed")], approved_by_me=[1])
        slack = FakeSlack({1: [make_msg([1])]})
        run(cfg, "alice", gl, slack, state=self.merged_state(60))
        assert slack.reacts == []

    def test_does_not_touch_another_persons_merged_mr(self, cfg):
        gl = FakeGitLab([make_mr(1, state="merged", author=BOB)], approved_by_me=[1])
        slack = FakeSlack({1: [make_msg([1])]})
        state = self.merged_state(60)
        state["mrs"]["88!1"]["author_id"] = BOB
        run(cfg, "alice", gl, slack, state=state)
        assert slack.reacts == []

    def test_settled_merged_mr_costs_no_api_call(self, cfg):
        now = time.time()
        state = {"mrs": {"88!1": {
            "sha": "sha1", "author_id": ALICE, "last_seen": now - 60,
            "messages": [make_msg([1])], "last_search": now,
            "reactions": {"C1:1.1": {"at": now}},
        }}}
        gl = FakeGitLab([make_mr(1, state="merged")], approved_by_me=[1])
        slack = FakeSlack({1: [make_msg([1])]})
        run(cfg, "alice", gl, slack, state=state)
        assert slack.searches == [] and slack.reactor_reads == [] and slack.reacts == []


def test_first_approval_is_not_reported_as_a_reapproval(cfg):
    stats, _ = run(cfg, "alice", FakeGitLab([make_mr(1)]), FakeSlack())
    assert stats["approved"] == 1 and stats["reapproved"] == 0


def test_approval_restored_after_a_push_is_reported_as_a_reapproval(cfg, capsys):
    gl, slack = FakeGitLab([make_mr(1, sha="old")]), FakeSlack()
    _, state = run(cfg, "alice", gl, slack)
    gl.mrs[1]["sha"] = "new"
    gl.approvals.clear()
    stats, _ = run(cfg, "alice", gl, slack, state)
    assert stats["approved"] == 1 and stats["reapproved"] == 1
    assert "ré-approuvée" in capsys.readouterr().out
    rows = [json.loads(line) for line in approve.JOURNAL_PATH.read_text().splitlines()]
    assert [r["reapproved"] for r in rows if r["event"] == "approval"] == [False, True]


def test_default_reverify_delay_is_five_minutes():
    assert approve.DEFAULT_CONFIG["reverify_minutes"] == 5
