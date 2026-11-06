import asyncio
import json

import pytest

import approve
from conftest import ALICE, BOB, FakeGitLab, FakeSlack, make_mr, make_msg
from test_parse_search import block, search
from conftest import URL_RE, mr_url

AUTHOR = {"id": 1, "username": "alice", "name": "Alice Example"}


def msg(**kw):
    return {"poster_id": "U1", "poster_name": None, "poster_email": None, "poster_handle": None, **kw}


class TestPosterIsAuthor:
    def test_same_account_by_email(self):
        assert approve.poster_is_author(msg(poster_email="alice@example.com"), AUTHOR, {})

    def test_same_account_by_slack_handle(self):
        assert approve.poster_is_author(msg(poster_handle="Alice"), AUTHOR, {})

    @pytest.mark.parametrize("name", ["Alice Example", "Alice. EXAMPLE", "example alice", "ALICE  EXAMPLE"])
    def test_same_person_by_display_name(self, name):
        assert approve.poster_is_author(msg(poster_name=name), AUTHOR, {})

    def test_accents_and_punctuation_do_not_matter(self):
        author = {"username": "elodie", "name": "Élodie O'Neil-Dupont"}
        assert approve.poster_is_author(msg(poster_name="Elodie O Neil Dupont"), author, {})

    def test_someone_else_is_not_the_author(self):
        assert not approve.poster_is_author(msg(poster_name="Carol Other", poster_email="carol@example.com"), AUTHOR, {})

    def test_only_the_first_name_in_common_is_not_enough(self):
        assert not approve.poster_is_author(msg(poster_name="Alice Smith"), AUTHOR, {})

    def test_a_username_that_merely_contains_the_handle_is_not_enough(self):
        assert not approve.poster_is_author(msg(poster_handle="alic"), AUTHOR, {})

    def test_a_message_without_poster_never_counts(self):
        assert not approve.poster_is_author(msg(poster_id=None, poster_email="alice@example.com"), AUTHOR, {})

    def test_an_author_without_username_never_matches(self):
        assert not approve.poster_is_author(msg(poster_name="Alice Example"), {"name": "Alice Example"}, {})

    def test_a_recognised_account_is_remembered_by_slack_id(self):
        learned = {}
        assert approve.poster_is_author(msg(poster_email="alice@example.com"), AUTHOR, learned)
        assert learned == {"alice": "U1"}
        assert approve.poster_is_author(msg(poster_id="U1"), AUTHOR, learned)

    def test_once_known_a_different_slack_id_is_refused_even_with_the_same_name(self):
        learned = {"alice": "U1"}
        assert not approve.poster_is_author(msg(poster_id="U2", poster_name="Alice Example"), AUTHOR, learned)

    def test_a_pinned_id_from_the_config_wins(self):
        assert approve.poster_is_author(msg(poster_id="UPIN"), AUTHOR, {"alice": "UPIN"})

    def test_label_falls_back_gracefully(self):
        assert approve.poster_label(msg(poster_name="Carol")) == "Carol"
        assert approve.poster_label(msg(poster_handle="carol")) == "carol"
        assert approve.poster_label({"poster_id": "U9"}) == "U9"
        assert approve.poster_label({}) == "?"


def run(cfg, who, gl, slack, state=None, recent=None, dry=False):
    state = {"mrs": {}} if state is None else state
    stats = asyncio.run(approve.process_person(who, cfg["people"][who], cfg, gl, slack, state, dry, None, recent))
    return stats, state


class TestSomeoneElsePostsTheLink:
    """The incident: a colleague pasted the link of somebody else's MR in a thread."""

    def test_a_gated_mr_is_not_approved_because_a_colleague_pasted_its_link(self, cfg):
        gl = FakeGitLab([make_mr(1, author=BOB)])
        slack = FakeSlack({1: [make_msg([1], poster="carol")]})
        stats, state = run(cfg, "bob", gl, slack)
        assert gl.posts == [] and slack.reacts == [] and stats["gated"] == 1
        assert state["mrs"]["88!1"]["foreign"] == ["Carol"]

    def test_it_is_approved_once_its_author_announces_it(self, cfg):
        gl = FakeGitLab([make_mr(1, author=BOB)])
        slack = FakeSlack({1: [make_msg([1], poster="carol")]})
        _, state = run(cfg, "bob", gl, slack)
        slack.announcements[1].append(make_msg([1], poster="bob", ts="2.2"))
        slack.searches.clear()
        state["mrs"]["88!1"]["search_due"] = 0
        run(cfg, "bob", gl, slack, state, recent=[])
        assert gl.posts == [(1, "sha1")] and slack.reacts == [("C1", "2.2", "lgtm")]

    def test_the_reaction_never_goes_on_the_colleagues_message(self, cfg):
        gl = FakeGitLab([make_mr(1)])
        slack = FakeSlack({1: [make_msg([1], poster="carol", ts="9.9")]})
        stats, _ = run(cfg, "alice", gl, slack)
        assert gl.posts == [(1, "sha1")] and slack.reacts == []

    def test_a_link_pasted_by_a_colleague_in_the_recent_scan_is_ignored(self, cfg):
        gl = FakeGitLab([make_mr(1, author=BOB)])
        slack = FakeSlack()
        _, state = run(cfg, "bob", gl, slack)
        run(cfg, "bob", gl, slack, state, recent=[make_msg([1], poster="carol")])
        assert gl.posts == [] and slack.reacts == []

    def test_a_link_pasted_by_a_colleague_in_a_bundle_with_the_authors_mr_is_ignored(self, cfg):
        gl = FakeGitLab([make_mr(1, author=BOB), make_mr(2)])
        slack = FakeSlack({1: [make_msg([1, 2], poster="carol")]})
        run(cfg, "bob", gl, slack)
        assert gl.posts == []

    def test_the_author_may_announce_a_bundle_of_their_own_mrs(self, cfg):
        gl = FakeGitLab([make_mr(1), make_mr(2)])
        msg_ = make_msg([1, 2], poster="alice")
        slack = FakeSlack({1: [msg_], 2: [msg_]})
        run(cfg, "alice", gl, slack)
        assert slack.reacts == [("C1", "1.1", "lgtm")]

    def test_a_merged_mr_does_not_get_a_reaction_on_a_colleagues_message(self, cfg):
        import time

        gl = FakeGitLab([make_mr(1, state="merged")], approved_by_me=[1])
        slack = FakeSlack({1: [make_msg([1], poster="carol")]})
        state = {"mrs": {"88!1": {"sha": "s", "author_id": ALICE, "last_seen": time.time(),
                                  "author": {"username": "alice", "name": "Alice Example"}}}}
        run(cfg, "alice", gl, slack, state, recent=[])
        assert slack.reacts == []


class TestStateMigration:
    def old_state(self):
        return {"mrs": {"88!1": {
            "sha": "s", "approved_sha": "s", "verified_at": 5, "author_id": 1,
            "messages": [{"channel_id": "C1", "ts": "1.1"}], "last_search": 9, "search_due": 99,
            "reactions": {"C1:1.1": {"at": 1}}, "recheck_at": 3}}, "recent_scan": 123}

    def test_announcements_found_under_the_old_rule_are_forgotten(self):
        state = approve.migrate_state(self.old_state())
        entry = state["mrs"]["88!1"]
        assert not {"messages", "last_search", "search_due", "reactions", "recheck_at"} & set(entry)
        assert entry["approved_sha"] == "s" and entry["verified_at"] == 5 and "recent_scan" not in state
        assert state["version"] == approve.STATE_VERSION

    def test_loading_an_old_file_migrates_it(self, tmp_path, monkeypatch):
        monkeypatch.setattr(approve, "STATE_PATH", tmp_path / "state.json")
        (tmp_path / "state.json").write_text(json.dumps(self.old_state()))
        assert "messages" not in approve.load_state()["mrs"]["88!1"]

    def test_a_current_file_is_left_alone(self, tmp_path, monkeypatch):
        monkeypatch.setattr(approve, "STATE_PATH", tmp_path / "state.json")
        current = {**self.old_state(), "version": approve.STATE_VERSION}
        (tmp_path / "state.json").write_text(json.dumps(current))
        assert approve.load_state()["mrs"]["88!1"]["messages"]

    def test_a_missing_file_starts_at_the_current_version(self, tmp_path, monkeypatch):
        monkeypatch.setattr(approve, "STATE_PATH", tmp_path / "absent.json")
        assert approve.load_state() == {"mrs": {}, "version": approve.STATE_VERSION}

    def test_after_migration_the_old_foreign_announcement_no_longer_counts(self, cfg):
        old = self.old_state()
        old["mrs"]["88!1"]["author_id"] = BOB
        state = approve.migrate_state(old)
        gl = FakeGitLab([make_mr(1, author=BOB)])
        slack = FakeSlack({1: [make_msg([1], poster="carol")]})
        run(cfg, "bob", gl, slack, state)
        assert slack.searches == [1]
        assert gl.posts == []


class TestWhyShowsTheAuthorRule:
    def rows(self, cfg, mrs, slack, **kw):
        gl = FakeGitLab(mrs, **kw)
        rows = asyncio.run(approve.explain_person(cfg["people"]["bob"], cfg, gl, slack, {"mrs": {}}, None))
        return {r["key"]: r for r in rows}

    def test_a_link_posted_only_by_others_is_explained_with_their_names(self, cfg):
        rows = self.rows(cfg, [make_mr(1, author=BOB)], FakeSlack({1: [make_msg([1], poster="carol")]}))
        assert rows["88!1"]["kind"] == "unannounced"
        assert "Carol" in rows["88!1"]["reason"] and "l'auteur" in rows["88!1"]["reason"]

    def test_an_mr_i_approved_without_an_announcement_by_its_author_is_flagged(self, cfg):
        rows = self.rows(cfg, [make_mr(1, author=BOB)], FakeSlack({1: [make_msg([1], poster="carol")]}), approved_by_me=[1])
        assert rows["88!1"]["kind"] == "orphan" and "à vérifier" in rows["88!1"]["reason"]

    def test_the_flagged_ones_come_first_in_the_report(self, cfg):
        mrs = [make_mr(1, author=BOB), make_mr(2, author=BOB)]
        slack = FakeSlack({2: [make_msg([2], poster="bob")]})
        rows = asyncio.run(approve.explain_person(
            cfg["people"]["bob"], cfg, FakeGitLab(mrs, approved_by_me=[1]), slack, {"mrs": {}}, None))
        assert [r["kind"] for r in rows] == ["orphan", "todo"]


class TestParsersCarryThePoster:
    def test_poster_without_an_email(self):
        text = search(block(1, 1, "front", "C1", "Alice (ID: U1) ", "1.5", f"<{mr_url(7)}|x>"))
        [m] = approve.parse_search(text, URL_RE)
        assert (m["poster_id"], m["poster_name"], m["poster_email"]) == ("U1", "Alice", None)

    def test_web_matches_keep_the_user_and_handle(self):
        match = {"channel": {"id": "C1", "name": "f"}, "ts": "1.1", "text": f"<{mr_url(7)}|x>", "user": "U9", "username": "carol"}
        [m] = approve.parse_web_matches([match], URL_RE)
        assert (m["poster_id"], m["poster_handle"]) == ("U9", "carol")

    def test_config_can_pin_slack_ids(self, cfg):
        assert approve.with_defaults({"slack_ids": {"alice": "U1"}})["slack_ids"] == {"alice": "U1"}
