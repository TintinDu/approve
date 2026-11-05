import pytest

import approve
from conftest import ME_GITLAB, FakeGitLab, make_mr, mr_url, URL_RE


def entry(**kw):
    return kw


class TestNeedsDiscovery:
    def test_without_announcement_search_every_pass_even_if_recent(self, cfg):
        e = entry(last_search=1000)
        assert approve.needs_discovery(e, {}, cfg, now=1001) is True

    def test_with_announcement_and_fresh_search_does_not_search(self, cfg):
        e = entry(messages=[{"x": 1}], last_search=1000)
        assert approve.needs_discovery(e, {}, cfg, now=1000 + 60) is False

    def test_with_announcement_searches_again_after_rediscovery_delay(self, cfg):
        e = entry(messages=[{"x": 1}], last_search=1000)
        assert approve.needs_discovery(e, {}, cfg, now=1000 + 3601) is True


class TestSettled:
    def msg(self, channel="front", ts="1.1"):
        return {"channel": channel, "channel_id": "C1", "ts": ts, "urls": []}

    def test_no_messages_is_never_settled(self, cfg):
        assert approve.settled(entry(), cfg, now=10) is False

    def test_all_reacted_and_fresh_is_settled(self, cfg):
        e = entry(messages=[self.msg()], last_search=10, reactions={"C1:1.1": {"at": 10}})
        assert approve.settled(e, cfg, now=20) is True

    def test_missing_reaction_record_is_not_settled(self, cfg):
        e = entry(messages=[self.msg()], last_search=10)
        assert approve.settled(e, cfg, now=20) is False

    def test_excluded_channel_needs_no_reaction(self, cfg):
        e = entry(messages=[self.msg(channel="no-reactions")], last_search=10)
        assert approve.settled(e, cfg, now=20) is True

    def test_stale_search_is_not_settled(self, cfg):
        e = entry(messages=[self.msg()], last_search=10, reactions={"C1:1.1": {"at": 10}})
        assert approve.settled(e, cfg, now=10 + 3601) is False


class TestApproveMr:
    def test_posts_with_current_sha_and_confirms(self):
        gl = FakeGitLab([make_mr(1, sha="abc")])
        assert approve.approve_mr(gl, make_mr(1), ME_GITLAB, dry=False) == ("approved", "abc")
        assert gl.posts == [(1, "abc")]

    def test_already_approved_is_not_posted_again(self):
        gl = FakeGitLab([make_mr(1)], approved_by_me=[1])
        assert approve.approve_mr(gl, make_mr(1), ME_GITLAB, dry=False)[0] == "already"
        assert gl.posts == []

    @pytest.mark.parametrize("state,draft", [("merged", False), ("closed", False), ("opened", True)])
    def test_skips_draft_and_non_open(self, state, draft):
        gl = FakeGitLab([make_mr(1, state=state, draft=draft)])
        assert approve.approve_mr(gl, make_mr(1), ME_GITLAB, dry=False)[0] == "skipped"
        assert gl.posts == []

    def test_dry_run_never_posts(self):
        gl = FakeGitLab([make_mr(1)])
        assert approve.approve_mr(gl, make_mr(1), ME_GITLAB, dry=True)[0] == "would_approve"
        assert gl.posts == []

    def test_fails_loudly_when_approval_is_not_visible_after_post(self):
        gl = FakeGitLab([make_mr(1)])
        gl.approve = lambda *a, **k: None
        with pytest.raises(RuntimeError, match="not visible"):
            approve.approve_mr(gl, make_mr(1), ME_GITLAB, dry=False)


class TestBundle:
    def test_all_approved(self):
        gl = FakeGitLab([make_mr(1), make_mr(2)], approved_by_me=[1, 2])
        urls = [mr_url(1), mr_url(2)]
        assert approve.bundle_fully_approved(gl, urls, ME_GITLAB, URL_RE, {}) is True

    def test_one_not_approved_blocks_the_whole_bundle(self):
        gl = FakeGitLab([make_mr(1), make_mr(2)], approved_by_me=[1])
        urls = [mr_url(1), mr_url(2)]
        assert approve.bundle_fully_approved(gl, urls, ME_GITLAB, URL_RE, {}) is False

    def test_unreadable_mr_counts_as_not_approved(self):
        gl = FakeGitLab([make_mr(1)], approved_by_me=[1])
        urls = [mr_url(1), mr_url(99)]
        assert approve.bundle_fully_approved(gl, urls, ME_GITLAB, URL_RE, {}) is False

    def test_result_is_cached_per_url(self):
        gl = FakeGitLab([make_mr(1)], approved_by_me=[1])
        cache = {}
        approve.bundle_fully_approved(gl, [mr_url(1)], ME_GITLAB, URL_RE, cache)
        gl.approvals.clear()
        assert approve.bundle_fully_approved(gl, [mr_url(1)], ME_GITLAB, URL_RE, cache) is True


class TestResolvePeople:
    def user(self, username, name="N", state="active", uid=1):
        return {"username": username, "name": name, "state": state, "id": uid}

    def patch(self, monkeypatch, users):
        monkeypatch.setattr(approve, "GitLab", lambda host: FakeGitLab([], users=users))

    def test_known_name_keeps_its_configured_mode(self, cfg, monkeypatch):
        self.patch(monkeypatch, [])
        out = approve.resolve_people(["alice", "bob"], cfg)
        assert out["alice"]["require_announcement"] is False
        assert out["bob"]["require_announcement"] is True

    def test_unknown_person_defaults_to_announcement_required(self, cfg, monkeypatch):
        self.patch(monkeypatch, [self.user("marie.dupont", uid=7)])
        out = approve.resolve_people(["marie.dupont"], cfg)
        assert out["marie.dupont"]["gitlab_id"] == 7
        assert out["marie.dupont"]["require_announcement"] is True

    def test_all_suffix_lifts_the_announcement_requirement(self, cfg, monkeypatch):
        self.patch(monkeypatch, [self.user("marie.dupont", uid=7)])
        out = approve.resolve_people(["marie.dupont:all"], cfg)
        assert out["marie.dupont"]["require_announcement"] is False

    def test_all_suffix_overrides_a_gated_known_name(self, cfg, monkeypatch):
        self.patch(monkeypatch, [self.user("bob", uid=60)])
        out = approve.resolve_people(["bob:all"], cfg)
        assert out["bob"]["require_announcement"] is False

    def test_ambiguous_name_lists_candidates_and_exits(self, cfg, monkeypatch, capsys):
        self.patch(monkeypatch, [self.user("carol.a"), self.user("carol.b")])
        with pytest.raises(SystemExit) as exc:
            approve.resolve_people(["carol"], cfg)
        assert "carol.a" in str(exc.value) and "2 correspondances" in str(exc.value)

    def test_exact_username_wins_over_partial_matches(self, cfg, monkeypatch):
        self.patch(monkeypatch, [self.user("anne", uid=1), self.user("anne.martin", uid=2)])
        assert approve.resolve_people(["anne"], cfg)["anne"]["gitlab_id"] == 1

    def test_inactive_users_are_ignored(self, cfg, monkeypatch):
        self.patch(monkeypatch, [self.user("anne", state="blocked")])
        with pytest.raises(SystemExit):
            approve.resolve_people(["anne"], cfg)

    def test_unknown_suffix_exits(self, cfg, monkeypatch):
        self.patch(monkeypatch, [])
        with pytest.raises(SystemExit, match="suffixe inconnu"):
            approve.resolve_people(["alice:none"], cfg)


def test_is_interrupt_sees_through_exception_groups():
    assert approve.is_interrupt(KeyboardInterrupt())
    assert approve.is_interrupt(BaseExceptionGroup("g", [ValueError(), KeyboardInterrupt()]))
    assert not approve.is_interrupt(BaseExceptionGroup("g", [ValueError()]))
    assert not approve.is_interrupt(RuntimeError())
