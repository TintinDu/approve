import asyncio
import json
import subprocess
import sys
import textwrap
import time
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace

import pytest

import approve
from conftest import ALICE, BOB, ME_GITLAB, FakeGitLab, FakeSlack, make_mr, make_msg

ROOT = Path(__file__).resolve().parent.parent


@pytest.fixture
def home(tmp_path, monkeypatch):
    monkeypatch.setattr(approve, "APP_DIR", tmp_path)
    monkeypatch.setattr(approve, "STATE_DIR", tmp_path / "state")
    monkeypatch.setattr(approve, "LOCK_PATH", tmp_path / "state" / "approve.lock")
    monkeypatch.setattr(approve, "PAUSE_PATH", tmp_path / "state" / "PAUSE")
    monkeypatch.setattr(approve, "WATCH_PATH", tmp_path / "watch.json")
    monkeypatch.setattr(approve, "CONFIG_PATH", tmp_path / "config.json")
    monkeypatch.setattr(approve, "JOURNAL_PATH", tmp_path / "journal.jsonl")
    return tmp_path


def user(username, uid, name="N"):
    return {"username": username, "name": name, "state": "active", "id": uid}


class TestModeFollowsThePerson:
    def patch(self, monkeypatch, users):
        monkeypatch.setattr(approve, "GitLab", lambda host: FakeGitLab([], users=users))

    def test_full_username_of_a_configured_person_keeps_that_persons_mode(self, cfg, monkeypatch):
        self.patch(monkeypatch, [user("alice.martin", ALICE)])
        out = approve.resolve_people(["alice.martin"], cfg)
        assert out["alice.martin"]["require_announcement"] is False

    def test_an_unknown_person_stays_cautious(self, cfg, monkeypatch):
        self.patch(monkeypatch, [user("carol", 5)])
        assert approve.resolve_people(["carol"], cfg)["carol"]["require_announcement"] is True

    def test_announced_suffix_forces_the_cautious_mode(self, cfg, monkeypatch):
        self.patch(monkeypatch, [user("alice.martin", ALICE)])
        assert approve.resolve_people(["alice.martin:announced"], cfg)["alice.martin"]["require_announcement"] is True

    def test_all_suffix_still_lifts_the_requirement(self, cfg, monkeypatch):
        self.patch(monkeypatch, [user("carol", 5)])
        assert approve.resolve_people(["carol:all"], cfg)["carol"]["require_announcement"] is False

    def test_mode_is_described_in_plain_words(self):
        assert "annoncées" in approve.mode_text({"require_announcement": True})
        assert "toutes" in approve.mode_text({"require_announcement": False})


class TestWatchlist:
    def test_merge_replaces_the_same_person_and_keeps_order(self):
        assert approve.merge_watch(["a", "b:all", "c"], ["b", "d"]) == ["a", "b", "c", "d"]

    def test_empty_by_default_and_survives_a_corrupt_file(self, home):
        assert approve.load_watch() == []
        approve.WATCH_PATH.write_text("{oops")
        assert approve.load_watch() == []

    def test_add_validates_then_saves(self, home, cfg, monkeypatch, capsys):
        seen = []
        monkeypatch.setattr(approve, "load_config", lambda host=None: (cfg, False))
        monkeypatch.setattr(approve, "resolve_people", lambda tokens, c: seen.append(list(tokens)) or {})
        assert approve.cmd_watch("add", ["dave", "bob:all"]) == 0
        assert seen == [["dave", "bob:all"]] and approve.load_watch() == ["dave", "bob:all"]
        out = capsys.readouterr().out
        assert "dave" in out and "toutes ses MR" in out

    def test_add_of_an_ambiguous_name_saves_nothing(self, home, cfg, monkeypatch):
        monkeypatch.setattr(approve, "load_config", lambda host=None: (cfg, False))

        def ambiguous(tokens, c):
            raise SystemExit("2 correspondances")

        monkeypatch.setattr(approve, "resolve_people", ambiguous)
        with pytest.raises(SystemExit):
            approve.cmd_watch("add", ["dave"])
        assert approve.load_watch() == []

    def test_remove_and_unknown_names(self, home, capsys):
        approve.save_watch(["dave", "bob:all"])
        assert approve.cmd_watch("remove", ["bob", "zoe"]) == 0
        assert approve.load_watch() == ["dave"] and "zoe n'était pas dans la liste" in capsys.readouterr().out

    def test_list_shows_the_mode_of_each_person_without_network(self, home, capsys):
        approve.CONFIG_PATH.write_text(json.dumps({"people": {"alice": {"gitlab_id": 1, "require_announcement": False}}}))
        approve.save_watch(["alice", "carol", "dave:all"])
        approve.cmd_watch("list", [])
        lines = capsys.readouterr().out
        assert "alice" in lines and "carol" in lines
        assert lines.index("toutes") < lines.index("annoncées") or "toutes" in lines

    def test_empty_list_tells_how_to_fill_it(self, home, capsys):
        approve.cmd_watch("list", [])
        assert "approve add" in capsys.readouterr().out

    def test_add_without_name_is_refused(self, home):
        assert approve.cmd_watch("add", []) == 1


class TestMainRouting:
    @pytest.fixture(autouse=True)
    def quiet(self, monkeypatch, cfg, home):
        monkeypatch.setattr(approve, "install_stop_handlers", lambda: None)
        full = approve.with_defaults(cfg)
        monkeypatch.setattr(approve, "load_config", lambda host=None: (full, False))
        monkeypatch.setattr(approve, "resolve_backend", lambda c: None)

    def test_subcommands_are_routed_to_the_watchlist(self, monkeypatch):
        calls = []
        monkeypatch.setattr(approve, "cmd_watch", lambda sub, rest, host=None: calls.append((sub, rest)) or 0)
        assert approve.main(["add", "dave"]) == 0 and approve.main(["list"]) == 0
        assert calls == [("add", ["dave"]), ("list", [])]

    def test_stop_is_routed(self, monkeypatch):
        monkeypatch.setattr(approve, "cmd_stop", lambda: 7)
        assert approve.main(["--stop"]) == 7

    def test_without_names_and_without_list_it_explains(self, capsys):
        with pytest.raises(SystemExit):
            approve.main([])
        assert "approve add" in capsys.readouterr().err

    def test_without_names_it_uses_the_remembered_list(self, monkeypatch):
        approve.save_watch(["dave", "bob:all"])
        seen = []
        monkeypatch.setattr(approve, "resolve_people", lambda tokens, c: seen.append(list(tokens)) or {"x": {}})

        async def why(cfg, names):
            return 0

        monkeypatch.setattr(approve, "why_pass", why)
        assert approve.main(["--why"]) == 0 and seen == [["dave", "bob:all"]]

    def test_names_on_the_command_line_win_over_the_list(self, monkeypatch):
        approve.save_watch(["dave"])
        seen = []
        monkeypatch.setattr(approve, "resolve_people", lambda tokens, c: seen.append(list(tokens)) or {"x": {}})

        async def why(cfg, names):
            return 0

        monkeypatch.setattr(approve, "why_pass", why)
        approve.main(["carol", "--why"])
        assert seen == [["carol"]]


class TestWhy:
    def rows(self, cfg, mrs, who="alice", slack=None, **gl_kw):
        gl = FakeGitLab(mrs, **gl_kw)
        slack = slack or FakeSlack()
        rows = asyncio.run(approve.explain_person(cfg["people"][who], cfg, gl, slack, {"mrs": {}}, None))
        return {r["key"]: r for r in rows}, gl, slack

    def test_each_situation_gets_its_own_verdict(self, cfg):
        mrs = [make_mr(1, draft=True), {**make_mr(2), "title": "demo DO NOT MERGE"}, make_mr(3), make_mr(4), make_mr(5)]
        rows, _, _ = self.rows(cfg, mrs, approved_by_me=[4], cannot_approve=[5])
        assert {k: r["kind"] for k, r in rows.items()} == {
            "88!1": "draft", "88!2": "dnm", "88!3": "todo", "88!4": "done", "88!5": "notallowed"}

    def test_it_never_writes(self, cfg):
        rows, gl, slack = self.rows(cfg, [make_mr(1), make_mr(2)], slack=FakeSlack({1: [make_msg([1])]}))
        assert gl.posts == [] and slack.reacts == []

    def test_announcement_gate_is_explained(self, cfg):
        mrs = [make_mr(1, author=BOB), make_mr(2, author=BOB)]
        rows, _, _ = self.rows(cfg, mrs, who="bob", slack=FakeSlack({1: [make_msg([1], poster="bob")]}))
        assert rows["88!1"]["kind"] == "todo" and rows["88!2"]["kind"] == "unannounced"
        assert "Slack" in rows["88!2"]["reason"]

    def test_slack_failure_is_reported_not_hidden(self, cfg):
        rows, _, _ = self.rows(cfg, [make_mr(1, author=BOB)], who="bob", slack=FakeSlack(fail_search=True))
        assert rows["88!1"]["kind"] == "unannounced" and "impossible" in rows["88!1"]["reason"]

    def test_without_slack_it_says_how_to_approve_everything(self, cfg):
        rows, _, _ = self.rows(cfg, [make_mr(1, author=BOB)], who="bob", slack=approve.NoSlack())
        assert ":all" in rows["88!1"]["reason"]

    def test_ungated_person_does_not_need_slack(self, cfg):
        rows, _, slack = self.rows(cfg, [make_mr(1)], slack=approve.NoSlack())
        assert rows["88!1"]["kind"] == "todo"

    def test_already_approved_are_counted_not_listed(self, cfg, capsys):
        mrs = [make_mr(1), make_mr(2), make_mr(3)]
        rows = asyncio.run(approve.explain_person(
            cfg["people"]["alice"], cfg, FakeGitLab(mrs, approved_by_me=[1, 2]), FakeSlack(), {"mrs": {}}, None))
        approve.print_why("alice", cfg["people"]["alice"], rows)
        out = capsys.readouterr().out
        assert "2 déjà approuvée" in out and "88!3" in out and "88!1" not in out

    def test_the_report_orders_what_needs_attention_first(self, cfg):
        mrs = [make_mr(1, draft=True), make_mr(2), make_mr(3)]
        rows, _, _ = self.rows(cfg, mrs, approved_by_me=[3])
        ordered = asyncio.run(approve.explain_person(
            cfg["people"]["alice"], cfg, FakeGitLab(mrs, approved_by_me=[3]), FakeSlack(), {"mrs": {}}, None))
        assert [r["kind"] for r in ordered] == ["todo", "draft", "done"]

    def test_why_pass_prints_every_person(self, cfg, monkeypatch, capsys, home):
        @asynccontextmanager
        async def session(config):
            yield FakeSlack()

        monkeypatch.setattr(approve, "slack_session", session)
        monkeypatch.setattr(approve, "GitLab", lambda host: FakeGitLab([make_mr(1), make_mr(2, author=BOB)]))
        assert asyncio.run(approve.why_pass(cfg, ["alice", "bob"])) == 0
        out = capsys.readouterr().out
        assert "alice :" in out and "bob :" in out


CHILD = textwrap.dedent('''
    import sys, time
    sys.path.insert(0, {root!r})
    import approve
    approve.install_stop_handlers()
    lock = approve.acquire_lock()
    print("ready", flush=True)
    try:
        time.sleep(60)
    except KeyboardInterrupt:
        pass
''')


class TestStop:
    def start(self, tmp_path):
        folder = tmp_path / "child"
        folder.mkdir()
        script = folder / "approve.py"
        script.write_text(CHILD.format(root=str(ROOT)))
        env = {"MR_APPROVER_HOME": str(tmp_path), "PATH": "/usr/bin:/bin", "HOME": str(tmp_path)}
        proc = subprocess.Popen([sys.executable, str(script)], env=env, stdout=subprocess.PIPE, text=True)
        assert proc.stdout.readline().strip() == "ready"
        return proc

    def test_no_instance_is_reported(self, home, capsys):
        assert approve.cmd_stop() == 0 and "Aucune instance" in capsys.readouterr().out

    def test_stops_a_real_running_instance_from_anywhere(self, tmp_path, home, capsys):
        proc = self.start(tmp_path)
        try:
            assert approve.lock_held() and approve.read_lock_pid() == proc.pid
            assert approve.cmd_stop(wait_seconds=10) == 0
            assert proc.wait(timeout=10) == 0 and not approve.lock_held()
            assert "Instance arrêtée" in capsys.readouterr().out
        finally:
            if proc.poll() is None:
                proc.kill()

    def test_status_shows_the_pid_of_the_running_instance(self, tmp_path, home):
        proc = self.start(tmp_path)
        try:
            assert f"PID {proc.pid}" in approve.status_report()
        finally:
            proc.kill()
            proc.wait()

    def test_a_refused_second_start_does_not_erase_the_pid(self, tmp_path, home):
        proc = self.start(tmp_path)
        try:
            with pytest.raises(SystemExit) as exc:
                approve.acquire_lock()
            assert "approve --stop" in str(exc.value) and approve.read_lock_pid() == proc.pid
        finally:
            proc.kill()
            proc.wait()

    def test_unknown_pid_explains_what_to_do(self, home, monkeypatch, capsys):
        monkeypatch.setattr(approve, "lock_held", lambda: True)
        monkeypatch.setattr(approve, "running_pid", lambda: None)
        assert approve.cmd_stop() == 1 and "Ctrl-C" in capsys.readouterr().out

    def test_instance_that_will_not_stop_is_reported(self, home, monkeypatch, capsys):
        monkeypatch.setattr(approve, "lock_held", lambda: True)
        monkeypatch.setattr(approve, "running_pid", lambda: 999999)
        monkeypatch.setattr(approve.os, "kill", lambda pid, sig: None)
        assert approve.cmd_stop(wait_seconds=0.3) == 1 and "kill -9 999999" in capsys.readouterr().out

    def ps(self, monkeypatch, listing):
        monkeypatch.setattr(approve.subprocess, "run", lambda *a, **k: SimpleNamespace(stdout=listing))

    def test_legacy_instance_without_pid_is_found_by_its_command(self, home, monkeypatch):
        self.ps(monkeypatch, "100 /usr/bin/python /x/approve.py alice\n200 /usr/bin/vim notes\n")
        assert approve.running_pid() == 100

    def test_ambiguous_legacy_processes_are_not_guessed(self, home, monkeypatch):
        self.ps(monkeypatch, "100 python /x/approve.py alice\n101 python /y/approve.py bob\n")
        assert approve.running_pid() is None

    def test_other_commands_of_the_tool_are_never_taken_for_the_instance(self, home, monkeypatch):
        self.ps(monkeypatch, "100 python /x/approve.py --stop\n101 python /x/approve.py --status\n")
        assert approve.running_pid() is None


class TestPsIsNotTruncated:
    def capture(self, monkeypatch):
        seen = []

        def fake(args, **kw):
            seen.append(list(args))
            return SimpleNamespace(stdout="100 python /very/long/path/approve.py\n")

        monkeypatch.setattr(approve.subprocess, "run", fake)
        return seen

    def test_the_command_of_a_pid_is_read_with_unlimited_width(self, monkeypatch):
        seen = self.capture(monkeypatch)
        assert approve.pid_is_approve(100)
        assert "-ww" in seen[0]

    def test_the_process_listing_is_read_with_unlimited_width(self, monkeypatch, home):
        seen = self.capture(monkeypatch)
        approve.running_pid()
        assert any("-wwaxo" in call for call in seen)
