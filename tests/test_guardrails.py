import asyncio
import json
from datetime import datetime, timedelta

import pytest

import approve
from conftest import FakeGitLab, FakeSlack, make_mr


@pytest.fixture
def state_dir(tmp_path, monkeypatch):
    monkeypatch.setattr(approve, "STATE_DIR", tmp_path)
    monkeypatch.setattr(approve, "LOCK_PATH", tmp_path / "approve.lock")
    monkeypatch.setattr(approve, "PAUSE_PATH", tmp_path / "PAUSE")
    return tmp_path


def run(cfg, gl, budget, dry=False):
    person = cfg["people"]["alice"]
    return asyncio.run(
        approve.process_person("alice", person, cfg, gl, FakeSlack(), {"mrs": {}}, dry, budget)
    )


class TestApprovalCap:
    def test_stops_before_exceeding_the_cap(self, cfg):
        gl = FakeGitLab([make_mr(i) for i in range(1, 6)])
        with pytest.raises(approve.ApprovalCapReached):
            run(cfg, gl, approve.Budget(3))
        assert len(gl.posts) == 3

    def test_cap_equal_to_the_number_of_mrs_is_fine(self, cfg):
        gl = FakeGitLab([make_mr(i) for i in range(1, 4)])
        run(cfg, gl, approve.Budget(3))
        assert len(gl.posts) == 3

    def test_already_approved_mrs_do_not_consume_the_budget(self, cfg):
        gl = FakeGitLab([make_mr(i) for i in range(1, 6)], approved_by_me=[1, 2, 3, 4])
        run(cfg, gl, approve.Budget(1))
        assert gl.posts == [(5, "sha5")]

    def test_simulation_reports_the_stop_without_writing(self, cfg):
        gl = FakeGitLab([make_mr(i) for i in range(1, 6)])
        with pytest.raises(approve.ApprovalCapReached):
            run(cfg, gl, approve.Budget(2), dry=True)
        assert gl.posts == []

    def test_no_budget_means_no_limit(self, cfg):
        gl = FakeGitLab([make_mr(i) for i in range(1, 6)])
        run(cfg, gl, None)
        assert len(gl.posts) == 5

    def test_cap_is_found_inside_an_exception_group(self):
        group = BaseExceptionGroup("g", [ValueError(), approve.ApprovalCapReached(3)])
        assert approve.contains(group, approve.ApprovalCapReached)
        assert not approve.contains(BaseExceptionGroup("g", [ValueError()]), approve.ApprovalCapReached)


class TestNotAllowed:
    def test_mr_i_am_not_an_eligible_approver_of_is_skipped_without_posting(self, cfg):
        gl = FakeGitLab([make_mr(1), make_mr(2)], cannot_approve=[1])
        stats = run(cfg, gl, None)
        assert gl.posts == [(2, "sha2")]
        assert stats["not_allowed"] == 1 and stats["errors"] == 0

    def test_it_does_not_consume_the_approval_budget(self, cfg):
        gl = FakeGitLab([make_mr(1), make_mr(2)], cannot_approve=[1])
        run(cfg, gl, approve.Budget(1))
        assert gl.posts == [(2, "sha2")]


class TestDoNotMerge:
    def mr(self, iid, title):
        return {**make_mr(iid), "title": title}

    @pytest.mark.parametrize("title", [
        "chore: demo — DO NOT MERGE [ML-1]", "do not merge", "Do-Not-Merge: spike", "x DO_NOT_MERGE",
    ])
    def test_titled_do_not_merge_is_never_approved_nor_searched(self, cfg, title):
        gl, slack = FakeGitLab([self.mr(1, title), self.mr(2, "fix: ok")]), FakeSlack()
        stats = asyncio.run(approve.process_person(
            "alice", cfg["people"]["alice"], cfg, gl, slack, {"mrs": {}}, False))
        assert gl.posts == [(2, "sha2")]
        assert 1 not in slack.searches
        assert stats["do_not_merge"] == 1 and stats["errors"] == 0

    def test_title_changed_after_listing_is_caught_before_posting(self, cfg):
        gl = FakeGitLab([self.mr(1, "fix: ok")])
        listed = gl.open_mrs(1001)
        gl.mrs[1]["title"] = "WIP DO NOT MERGE"
        assert approve.approve_mr(gl, listed[0], 1000, False)[0] == "do_not_merge"
        assert gl.posts == []

    def test_ordinary_titles_are_not_affected(self, cfg):
        gl = FakeGitLab([self.mr(1, "fix(parser): merge conflict handling, do not crash")])
        asyncio.run(approve.process_person(
            "alice", cfg["people"]["alice"], cfg, gl, FakeSlack(), {"mrs": {}}, False))
        assert gl.posts == [(1, "sha1")]


def test_describe_unwraps_exception_groups():
    group = BaseExceptionGroup("unhandled errors", [ConnectionError("reset"), ValueError("x")])
    assert approve.describe(group) == "ConnectionError: reset; ValueError: x"


class TestPause:
    def test_pause_then_resume(self, state_dir):
        approve.set_pause(True)
        assert approve.PAUSE_PATH.exists()
        approve.set_pause(False)
        assert not approve.PAUSE_PATH.exists()

    def test_resume_without_pause_is_harmless(self, state_dir):
        approve.set_pause(False)


class TestStatus:
    def write(self, tmp_path, monkeypatch, rows):
        path = tmp_path / "journal.jsonl"
        path.write_text("\n".join(json.dumps(r) if isinstance(r, dict) else r for r in rows) + "\n")
        monkeypatch.setattr(approve, "JOURNAL_PATH", path)

    def row(self, event, ago_hours=0.0, **kw):
        at = datetime.now() - timedelta(hours=ago_hours)
        return {"at": at.isoformat(timespec="seconds"), "event": event, **kw}

    def test_counts_only_live_events_of_the_window(self, state_dir, monkeypatch):
        self.write(state_dir, monkeypatch, [
            self.row("approval", dry=False),
            self.row("approval", dry=False),
            self.row("approval", dry=True),
            self.row("approval", ago_hours=30, dry=False),
            self.row("reaction_added"),
            self.row("error"),
            self.row("pass", errors=0),
        ])
        report = approve.status_report()
        assert "2 approbations, 1 réactions, 1 erreurs" in report
        assert "pause : non" in report

    def test_reports_pause_and_stopped_state(self, state_dir, monkeypatch):
        self.write(state_dir, monkeypatch, [])
        approve.set_pause(True)
        report = approve.status_report()
        assert "pause : oui" in report and "arrêté" in report and "aucune" in report

    def test_last_failed_pass_is_labelled_as_a_failure(self, state_dir, monkeypatch):
        self.write(state_dir, monkeypatch, [self.row("pass_failed", error="x")])
        assert "(échec)" in approve.status_report()

    def test_flags_a_reached_cap(self, state_dir, monkeypatch):
        self.write(state_dir, monkeypatch, [self.row("cap_reached", cap=30)])
        assert "--max-approvals" in approve.status_report()

    def test_survives_a_corrupt_journal_line(self, state_dir, monkeypatch):
        self.write(state_dir, monkeypatch, ["{not json", self.row("pass", errors=0)])
        assert "dernière passe" in approve.status_report()

    def test_missing_journal(self, state_dir, monkeypatch):
        monkeypatch.setattr(approve, "JOURNAL_PATH", state_dir / "absent.jsonl")
        assert "aucune" in approve.status_report()

    def test_detects_a_running_instance_through_the_lock(self, state_dir, monkeypatch):
        monkeypatch.setattr(approve, "JOURNAL_PATH", state_dir / "absent.jsonl")
        handle = approve.acquire_lock()
        try:
            assert "en cours" in approve.status_report()
        finally:
            handle.close()
        assert "arrêté" in approve.status_report()


class TestInterruptAtStartup:
    def test_ctrl_c_before_the_loop_stops_cleanly_instead_of_a_traceback(self, monkeypatch, capsys):
        def interrupted(argv=None):
            raise KeyboardInterrupt

        monkeypatch.setattr(approve, "main", interrupted)
        assert approve.cli() == approve.EXIT_INTERRUPTED
        assert "arrêté" in capsys.readouterr().out

    def test_normal_exit_codes_are_passed_through(self, monkeypatch):
        monkeypatch.setattr(approve, "main", lambda argv=None: 3)
        assert approve.cli() == 3


class TestStopRequest:
    @pytest.fixture(autouse=True)
    def reset(self):
        approve.INTERRUPTED.clear()
        yield
        approve.INTERRUPTED.clear()

    def test_a_plain_error_is_not_a_stop_request(self):
        assert not approve.stop_requested(RuntimeError("glab failed"))

    def test_a_remembered_signal_wins_over_whatever_error_it_caused(self):
        approve.INTERRUPTED.set()
        assert approve.stop_requested(RuntimeError("glab was killed by the same Ctrl-C"))

    def test_the_handler_remembers_the_request_and_interrupts(self):
        with pytest.raises(KeyboardInterrupt):
            approve.request_stop(2, None)
        assert approve.INTERRUPTED.is_set()

    def test_both_ctrl_c_and_kill_are_handled(self, monkeypatch):
        installed = {}
        monkeypatch.setattr(approve.signal, "signal", lambda sig, handler: installed.update({sig: handler}))
        approve.install_stop_handlers()
        assert installed == {approve.signal.SIGINT: approve.request_stop, approve.signal.SIGTERM: approve.request_stop}
