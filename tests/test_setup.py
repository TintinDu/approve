import argparse
import json
import stat
from pathlib import Path

import pytest

import approve
from conftest import FakeGitLab

GLAB_STATUS = """github.com
  X Failed to log in to github.com account someone (default)
  - The token in default is invalid.
gitlab.example.com
  ✓ Logged in to gitlab.example.com as alice (/home/alice/.config/glab-cli/config.yml)
  ✓ Git operations for gitlab.example.com configured to use ssh protocol.
"""


@pytest.fixture(autouse=True)
def isolated(tmp_path, monkeypatch):
    monkeypatch.setattr(approve, "APP_DIR", tmp_path / "app")
    monkeypatch.setattr(approve, "TOKEN_PATH", tmp_path / "app" / "slack_token")
    monkeypatch.delenv(approve.TOKEN_ENV, raising=False)
    monkeypatch.setattr(approve, "HERMES", tmp_path / "no-hermes")
    monkeypatch.setattr(approve, "detect_glab_hosts", lambda: [])


@pytest.fixture
def config_path(tmp_path, monkeypatch):
    path = tmp_path / "app" / "config.json"
    monkeypatch.setattr(approve, "CONFIG_PATH", path)
    return path


def good_raw():
    return {"gitlab_host": "gitlab.test", "me": {"gitlab_id": 1000, "slack_id": "U0ALICE001"}}


class TestPaths:
    def test_defaults_live_in_one_folder_of_the_home(self):
        paths = approve.resolve_paths({}, Path("/home/x"))
        assert paths["app"] == Path("/home/x/.mr-approver")
        assert paths["config"] == Path("/home/x/.mr-approver/config.json")
        assert paths["hermes"] == Path("/home/x/.hermes")

    def test_app_folder_can_be_moved(self):
        paths = approve.resolve_paths({"MR_APPROVER_HOME": "/data/ma"}, Path("/home/x"))
        assert paths["config"] == Path("/data/ma/config.json")

    def test_config_file_can_be_pointed_at_directly(self):
        paths = approve.resolve_paths({"APPROVE_CONFIG": "/etc/a.json"}, Path("/home/x"))
        assert paths["config"] == Path("/etc/a.json")

    def test_hermes_location_can_be_overridden(self):
        paths = approve.resolve_paths({"HERMES_HOME": "/opt/hermes"}, Path("/home/x"))
        assert paths["hermes"] == Path("/opt/hermes")


class TestConfig:
    def test_minimal_config_gets_every_default(self):
        cfg = approve.with_defaults(good_raw())
        assert cfg["emoji"] == "lgtm" and cfg["people"] == {} and cfg["max_approvals_per_pass"] == 30
        assert cfg["slack_mcp"]["url"].startswith("https://")

    def test_user_values_win_over_defaults(self):
        cfg = approve.with_defaults({**good_raw(), "emoji": "thumbsup", "interval_seconds": 60})
        assert cfg["emoji"] == "thumbsup" and cfg["interval_seconds"] == 60

    def test_defaults_are_not_shared_between_loads(self):
        approve.with_defaults(good_raw())["exclude_channels"].append("x")
        assert approve.DEFAULT_CONFIG["exclude_channels"] == []

    def test_valid_config_has_no_problem(self):
        assert approve.validate_config(approve.with_defaults(good_raw())) == []

    def test_example_values_are_reported_as_to_fill(self):
        example = json.loads(Path("approve_config.example.json").read_text())
        problems = approve.validate_config(approve.with_defaults(example))
        assert len(problems) == 2

    @pytest.mark.parametrize("me", [{}, {"gitlab_id": 0, "slack_id": "U0ALICE001"},
                                    {"gitlab_id": "12", "slack_id": "U0ALICE001"},
                                    {"gitlab_id": 5, "slack_id": "alice"}])
    def test_bad_identity_is_reported(self, me):
        cfg = approve.with_defaults({"gitlab_host": "gitlab.test", "me": me})
        assert approve.validate_config(cfg)

    def test_missing_file_tells_to_run_init(self, config_path):
        with pytest.raises(SystemExit, match="approve --init"):
            approve.load_config()

    def test_broken_json_is_explained(self, config_path):
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{oops")
        with pytest.raises(SystemExit, match="JSON valide"):
            approve.load_config()

    def test_incomplete_config_lists_each_problem_and_points_to_doctor(self, config_path):
        config_path.parent.mkdir(parents=True)
        config_path.write_text(json.dumps({"gitlab_host": "gitlab.test"}))
        with pytest.raises(SystemExit) as exc:
            approve.load_config()
        assert "me.gitlab_id" in str(exc.value) and "approve --doctor" in str(exc.value)

    def test_valid_file_loads_with_defaults(self, config_path):
        approve.write_config(good_raw(), config_path)
        cfg, auto = approve.load_config()
        assert cfg["interval_seconds"] == 300 and auto is False

    def test_people_section_is_optional_for_resolving_usernames(self, monkeypatch):
        user = {"username": "carol", "name": "C", "state": "active", "id": 7}
        monkeypatch.setattr(approve, "GitLab", lambda host: FakeGitLab([], users=[user]))
        cfg = approve.with_defaults(good_raw())
        assert approve.resolve_people(["carol"], cfg)["carol"]["gitlab_id"] == 7


class TestInitHelpers:
    def test_detects_only_hosts_where_glab_is_logged_in(self):
        assert approve.detect_hosts(GLAB_STATUS) == ["gitlab.example.com"]

    def test_detects_several_hosts_sorted(self):
        text = "✓ Logged in to b.test as x\n✓ Logged in to a.test as x\n"
        assert approve.detect_hosts(text) == ["a.test", "b.test"]

    def test_no_connected_host_explains_how_to_log_in(self):
        with pytest.raises(SystemExit, match="glab auth login"):
            approve.choose_host([], interactive=False)

    def test_single_host_is_chosen_without_asking(self):
        assert approve.choose_host(["a.test"], ask=lambda _: pytest.fail("asked")) == "a.test"

    def test_several_hosts_without_a_terminal_ask_for_the_host_option(self):
        with pytest.raises(SystemExit, match="--host"):
            approve.choose_host(["a.test", "b.test"], interactive=False)

    def test_several_hosts_with_a_terminal_let_the_user_pick(self, capsys):
        assert approve.choose_host(["a.test", "b.test"], ask=lambda _: "2", interactive=True) == "b.test"

    @pytest.mark.parametrize("answer", ["", "0", "3", "x"])
    def test_invalid_choice_is_refused(self, answer):
        with pytest.raises(SystemExit, match="invalide"):
            approve.choose_host(["a.test", "b.test"], ask=lambda _: answer, interactive=True)

    def test_excluded_channels_are_cleaned_up(self):
        out = approve.ask_excluded(interactive=True, ask=lambda _: " #general , random,, ")
        assert out == ["general", "random"]

    def test_no_question_asked_without_a_terminal(self):
        assert approve.ask_excluded(interactive=False, ask=lambda _: pytest.fail("asked")) == []

    def test_built_config_is_valid_and_keeps_the_choices(self):
        cfg = approve.build_config("gitlab.test", 1000, "U0ALICE001", ["general"])
        assert approve.validate_config(cfg) == []
        assert cfg["exclude_channels"] == ["general"]
        assert approve.DEFAULT_CONFIG["exclude_channels"] == []

    def test_slack_id_is_optional_in_a_built_config(self):
        cfg = approve.build_config("gitlab.test", 1000, None, [])
        assert "slack_id" not in cfg["me"] and approve.validate_config(cfg) == []

    def test_existing_config_is_never_overwritten_silently(self, config_path):
        approve.write_config(good_raw(), config_path)
        with pytest.raises(FileExistsError):
            approve.write_config({}, config_path)
        approve.write_config({"x": 1}, config_path, force=True)
        assert json.loads(config_path.read_text()) == {"x": 1}

    def test_config_file_is_private_to_the_user(self, config_path):
        approve.write_config(good_raw(), config_path)
        assert stat.S_IMODE(config_path.stat().st_mode) == 0o600


class TestCmdInit:
    def args(self, **kw):
        return argparse.Namespace(**{"force": False, "host": "gitlab.test", "slack_id": None, "hermes": False, "token": False, **kw})

    @pytest.fixture(autouse=True)
    def fakes(self, monkeypatch):
        me = {"id": 1000, "username": "alice"}
        monkeypatch.setattr(approve, "GitLab", lambda host: type("G", (), {"api": lambda s, p, **k: me})())
        monkeypatch.setattr(approve, "init_slack_token", lambda interactive: ("U0ALICE001", None))
        monkeypatch.setattr(approve, "ask_excluded", lambda *a, **k: [])
        monkeypatch.setattr(approve, "ensure_hermes_python", lambda: None)

    def test_writes_a_valid_config_found_automatically(self, config_path, capsys):
        assert approve.cmd_init(self.args()) == 0
        cfg = json.loads(config_path.read_text())
        assert cfg["me"] == {"gitlab_id": 1000, "slack_id": "U0ALICE001"}
        assert cfg["slack_backend"] == "auto"
        assert "approve --doctor" in capsys.readouterr().out

    def test_refuses_to_replace_an_existing_config(self, config_path, capsys):
        approve.write_config(good_raw(), config_path)
        assert approve.cmd_init(self.args()) == 1
        assert "--force" in capsys.readouterr().out
        assert json.loads(config_path.read_text())["gitlab_host"] == "gitlab.test"

    def test_force_replaces_it(self, config_path):
        approve.write_config({"old": True}, config_path)
        assert approve.cmd_init(self.args(force=True)) == 0
        assert "old" not in json.loads(config_path.read_text())

    def test_slack_problem_is_explained_and_nothing_is_written(self, config_path, monkeypatch, capsys):
        monkeypatch.setattr(approve, "init_slack_token", lambda interactive: (None, "Ce jeton ne fonctionne pas"))
        assert approve.cmd_init(self.args()) == 1
        assert "ne fonctionne pas" in capsys.readouterr().out and not config_path.exists()

    def test_gitlab_failure_explains_how_to_log_in(self, config_path, monkeypatch, capsys):
        def broken(host):
            return type("G", (), {"api": lambda s, p, **k: (_ for _ in ()).throw(RuntimeError("401"))})()

        monkeypatch.setattr(approve, "GitLab", broken)
        assert approve.cmd_init(self.args()) == 1
        assert "glab auth login" in capsys.readouterr().out and not config_path.exists()

    def test_hermes_mode_reads_the_slack_id_from_hermes(self, config_path, monkeypatch):
        async def slack_id(cfg=None):
            return "U0HERMES01"

        monkeypatch.setattr(approve, "fetch_slack_id", slack_id)
        monkeypatch.setattr(approve, "init_slack_token", lambda interactive: pytest.fail("token path used"))
        assert approve.cmd_init(self.args(hermes=True)) == 0
        cfg = json.loads(config_path.read_text())
        assert cfg["slack_backend"] == "hermes" and cfg["me"]["slack_id"] == "U0HERMES01"

    def test_hermes_is_chosen_automatically_when_detected_and_no_token(self, config_path, monkeypatch, capsys):
        async def slack_id(cfg=None):
            return "U0HERMES01"

        monkeypatch.setattr(approve, "fetch_slack_id", slack_id)
        monkeypatch.setattr(approve, "hermes_available", lambda: True)
        monkeypatch.setattr(approve, "init_slack_token", lambda interactive: pytest.fail("token asked"))
        assert approve.cmd_init(self.args()) == 0
        assert json.loads(config_path.read_text())["slack_backend"] == "hermes"
        assert "Hermes détecté" in capsys.readouterr().out

    def test_an_existing_token_keeps_the_token_path_even_with_hermes_installed(self, config_path, monkeypatch):
        approve.save_slack_token("xoxp-abc")
        monkeypatch.setattr(approve, "hermes_available", lambda: True)
        assert approve.cmd_init(self.args()) == 0
        assert json.loads(config_path.read_text())["slack_backend"] == "auto"

    def test_token_flag_forces_the_token_path(self, config_path, monkeypatch):
        monkeypatch.setattr(approve, "hermes_available", lambda: True)
        assert approve.cmd_init(self.args(token=True)) == 0
        assert json.loads(config_path.read_text())["slack_backend"] == "auto"

    def test_hermes_mode_slack_id_by_hand_skips_detection(self, config_path, monkeypatch):
        async def boom(cfg=None):
            raise AssertionError("detection should be skipped")

        monkeypatch.setattr(approve, "fetch_slack_id", boom)
        assert approve.cmd_init(self.args(hermes=True, slack_id="U0BOB000001")) == 0
        assert json.loads(config_path.read_text())["me"]["slack_id"] == "U0BOB000001"

    def test_hermes_mode_failure_explains_how_to_give_the_id(self, config_path, monkeypatch, capsys):
        async def fail(cfg=None):
            raise RuntimeError("Hermes introuvable")

        monkeypatch.setattr(approve, "fetch_slack_id", fail)
        assert approve.cmd_init(self.args(hermes=True)) == 1
        assert "--slack-id" in capsys.readouterr().out and not config_path.exists()


class TestDoctor:
    def test_python_check_passes_on_this_interpreter(self):
        assert approve.check_python().ok

    def test_config_check_reports_each_state(self, config_path):
        assert not approve.check_config()[0].ok
        config_path.parent.mkdir(parents=True)
        config_path.write_text("{")
        assert not approve.check_config()[0].ok
        approve.write_config(good_raw(), config_path, force=True)
        check, cfg = approve.check_config()
        assert check.ok and cfg["gitlab_host"] == "gitlab.test"

    def test_glab_missing(self, monkeypatch):
        monkeypatch.setattr(approve.shutil, "which", lambda name: None)
        check = approve.check_glab(approve.with_defaults(good_raw()))
        assert not check.ok and "brew install glab" in check.hint

    def test_glab_not_logged_in(self, monkeypatch):
        monkeypatch.setattr(approve.shutil, "which", lambda name: "/bin/glab")
        monkeypatch.setattr(approve, "GitLab", lambda host: type("G", (), {
            "api": lambda s, p, **k: (_ for _ in ()).throw(RuntimeError("401"))})())
        check = approve.check_glab(approve.with_defaults(good_raw()))
        assert not check.ok and "glab auth login --hostname gitlab.test" in check.hint

    def test_glab_account_different_from_config(self, monkeypatch):
        monkeypatch.setattr(approve.shutil, "which", lambda name: "/bin/glab")
        monkeypatch.setattr(approve, "GitLab", lambda host: type("G", (), {
            "api": lambda s, p, **k: {"id": 5, "username": "other"}})())
        check = approve.check_glab(approve.with_defaults(good_raw()))
        assert not check.ok and "≠" in check.detail

    def test_hermes_missing_everything(self, tmp_path, monkeypatch):
        monkeypatch.setattr(approve, "HERMES", tmp_path / "nothing")
        monkeypatch.delenv("SLACK_MCP_CLIENT_ID", raising=False)
        results = approve.check_hermes()
        assert [c.ok for c in results if "installé" in c.label or "défini" in c.label or "autorisé" in c.label] == [False, False, False]

    def test_hermes_present(self, tmp_path, monkeypatch):
        (tmp_path / "hermes-agent").mkdir()
        (tmp_path / "mcp-tokens").mkdir()
        (tmp_path / "mcp-tokens" / "slack.json").write_text("{}")
        (tmp_path / ".env").write_text("SLACK_MCP_CLIENT_ID=abc\n")
        monkeypatch.setattr(approve, "HERMES", tmp_path)
        monkeypatch.delenv("SLACK_MCP_CLIENT_ID", raising=False)
        monkeypatch.setattr(approve, "load_env", lambda: monkeypatch.setenv("SLACK_MCP_CLIENT_ID", "abc"))
        by_label = {c.label: c.ok for c in approve.check_hermes()}
        assert by_label["Hermes installé"] and by_label["SLACK_MCP_CLIENT_ID défini"] and by_label["Slack autorisé dans Hermes"]

    def test_slack_check_detects_a_wrong_account(self, monkeypatch):
        import asyncio

        async def other(cfg=None):
            return "U0SOMEONE01"

        monkeypatch.setattr(approve, "fetch_slack_id", other)
        check = asyncio.run(approve.check_slack(approve.with_defaults(good_raw())))
        assert not check.ok and "≠" in check.detail


def test_notification_is_skipped_silently_without_osascript(monkeypatch):
    monkeypatch.setattr(approve.shutil, "which", lambda name: None)
    monkeypatch.setattr(approve.subprocess, "run", lambda *a, **k: pytest.fail("should not run"))
    approve.notify("t", "m")


class TestZeroConfig:
    def fake_gitlab(self, monkeypatch, me=None, fail=False):
        def api(self_, path, **kw):
            if fail:
                raise RuntimeError("401")
            return me or {"id": 1000, "username": "alice"}

        monkeypatch.setattr(approve, "GitLab", lambda host: type("G", (), {"api": api})())

    def test_single_glab_server_is_enough_to_work_without_any_file(self, config_path, monkeypatch):
        monkeypatch.setattr(approve, "detect_glab_hosts", lambda: ["gitlab.test"])
        self.fake_gitlab(monkeypatch)
        cfg, auto = approve.load_config()
        assert auto is True
        assert cfg["gitlab_host"] == "gitlab.test" and cfg["me"]["gitlab_id"] == 1000
        assert not config_path.exists()

    def test_several_glab_servers_cannot_be_guessed(self, config_path, monkeypatch):
        monkeypatch.setattr(approve, "detect_glab_hosts", lambda: ["a.test", "b.test"])
        with pytest.raises(SystemExit, match="--init"):
            approve.load_config()

    def test_explicit_host_resolves_the_ambiguity(self, config_path, monkeypatch):
        monkeypatch.setattr(approve, "detect_glab_hosts", lambda: ["a.test", "b.test"])
        self.fake_gitlab(monkeypatch)
        cfg, auto = approve.load_config("b.test")
        assert cfg["gitlab_host"] == "b.test" and auto

    def test_glab_not_logged_in_gives_no_config(self, monkeypatch):
        monkeypatch.setattr(approve, "detect_glab_hosts", lambda: ["a.test"])
        self.fake_gitlab(monkeypatch, fail=True)
        assert approve.auto_config() is None

    def test_a_config_file_always_wins_over_detection(self, config_path, monkeypatch):
        approve.write_config(good_raw(), config_path)
        monkeypatch.setattr(approve, "detect_glab_hosts", lambda: pytest.fail("glab consulted"))
        assert approve.load_config() [0]["gitlab_host"] == "gitlab.test"

    def test_doctor_accepts_a_detected_config(self, config_path, monkeypatch):
        monkeypatch.setattr(approve, "detect_glab_hosts", lambda: ["gitlab.test"])
        self.fake_gitlab(monkeypatch)
        check, cfg = approve.check_config()
        assert check.ok and cfg["gitlab_host"] == "gitlab.test"


class TestGitlabOnly:
    def test_no_slack_access_resolves_to_no_backend(self):
        assert approve.resolve_backend(approve.with_defaults(good_raw())) is None

    def test_gated_people_are_refused_with_the_exact_command_to_type(self):
        people = {"alice": {"require_announcement": True}, "bob": {"require_announcement": False}}
        message = approve.gitlab_only_refusal(people)
        assert "approve alice:all" in message and "bob" not in message

    def test_people_approved_in_full_need_nothing(self):
        assert approve.gitlab_only_refusal({"bob": {"require_announcement": False}}) is None

    def test_the_placeholder_slack_never_finds_anything_and_never_reacts(self):
        import asyncio

        slack = approve.NoSlack()
        assert asyncio.run(slack.search_mr(1, approve.re.compile("x"))) == []
        assert asyncio.run(slack.reactors("C1", "1.1", "lgtm")) == set()
        assert asyncio.run(slack.react("C1", "1.1", "lgtm")) is None

    def test_session_without_backend_yields_the_placeholder(self):
        import asyncio

        async def get():
            async with approve.slack_session(approve.with_defaults(good_raw())) as slack:
                return slack

        assert isinstance(asyncio.run(get()), approve.NoSlack)


class TestTokenAndBackend:
    def test_environment_token_wins_over_the_file(self, monkeypatch):
        approve.save_slack_token("xoxp-file")
        monkeypatch.setenv(approve.TOKEN_ENV, " xoxp-env ")
        assert approve.load_slack_token() == "xoxp-env"

    def test_file_token_is_trimmed_and_private(self):
        approve.save_slack_token("xoxp-abc\n\n")
        assert approve.load_slack_token() == "xoxp-abc"
        assert stat.S_IMODE(approve.TOKEN_PATH.stat().st_mode) == 0o600

    def test_no_token_is_none(self):
        assert approve.load_slack_token() is None

    def test_a_token_selects_the_token_backend(self):
        approve.save_slack_token("xoxp-abc")
        assert approve.resolve_backend(approve.with_defaults(good_raw())) == "token"

    def test_hermes_is_used_when_there_is_no_token_but_hermes_is_ready(self, monkeypatch):
        monkeypatch.setattr(approve, "hermes_available", lambda: True)
        assert approve.resolve_backend(approve.with_defaults(good_raw())) == "hermes"

    def test_the_token_wins_over_hermes(self, monkeypatch):
        approve.save_slack_token("xoxp-abc")
        monkeypatch.setattr(approve, "hermes_available", lambda: True)
        assert approve.resolve_backend(approve.with_defaults(good_raw())) == "token"

    def test_explicit_choice_is_respected(self, monkeypatch):
        approve.save_slack_token("xoxp-abc")
        assert approve.resolve_backend({"slack_backend": "hermes"}) == "hermes"

    def test_token_session_yields_the_web_client(self):
        import asyncio

        approve.save_slack_token("xoxp-abc")

        async def get():
            async with approve.slack_session(approve.with_defaults(good_raw())) as slack:
                return slack

        assert isinstance(asyncio.run(get()), approve.SlackWeb)


class FakeWeb:
    def __init__(self, token, slack_id="U0ALICE001", scopes=None, error=None):
        self.slack_id, self.error = slack_id, error
        self.scopes = set(approve.REQUIRED_SCOPES) if scopes is None else scopes

    async def whoami(self):
        if self.error:
            raise self.error
        return self.slack_id


class TestInitSlackToken:
    def patch(self, monkeypatch, **kw):
        monkeypatch.setattr(approve, "SlackWeb", lambda token: FakeWeb(token, **kw))

    def test_without_a_terminal_and_without_token_it_explains(self, monkeypatch):
        self.patch(monkeypatch)
        slack_id, error = approve.init_slack_token(False)
        assert slack_id is None and approve.TOKEN_ENV in error

    def test_prompts_validates_and_saves_the_token(self, monkeypatch, capsys):
        self.patch(monkeypatch)
        slack_id, error = approve.init_slack_token(True, ask_token=lambda: "xoxp-new")
        assert (slack_id, error) == ("U0ALICE001", None)
        assert approve.load_slack_token() == "xoxp-new"
        assert "api.slack.com/apps" in capsys.readouterr().out

    def test_token_from_the_environment_is_not_copied_to_disk(self, monkeypatch):
        self.patch(monkeypatch)
        monkeypatch.setenv(approve.TOKEN_ENV, "xoxp-env")
        assert approve.init_slack_token(False)[1] is None
        assert not approve.TOKEN_PATH.exists()

    def test_empty_input_is_refused(self, monkeypatch):
        self.patch(monkeypatch)
        assert approve.init_slack_token(True, ask_token=lambda: "")[0] is None

    def test_a_rejected_token_is_not_saved(self, monkeypatch):
        self.patch(monkeypatch, error=approve.SlackError("invalid_auth"))
        slack_id, error = approve.init_slack_token(True, ask_token=lambda: "xoxp-bad")
        assert slack_id is None and "ne fonctionne pas" in error and not approve.TOKEN_PATH.exists()

    def test_missing_rights_are_listed_and_the_token_is_not_saved(self, monkeypatch):
        self.patch(monkeypatch, scopes={"search:read"})
        slack_id, error = approve.init_slack_token(True, ask_token=lambda: "xoxp-weak")
        assert slack_id is None and "reactions:write" in error and not approve.TOKEN_PATH.exists()

    def test_unknown_scopes_header_does_not_block(self, monkeypatch):
        self.patch(monkeypatch)
        monkeypatch.setattr(approve, "SlackWeb", lambda token: type("W", (), {
            "scopes": None, "whoami": lambda s: __import__("asyncio").sleep(0, "U0ALICE001")})())
        assert approve.init_slack_token(True, ask_token=lambda: "xoxp-x")[1] is None


class TestDoctorToken:
    def run(self, monkeypatch, cfg=None, **kw):
        import asyncio

        approve.save_slack_token("xoxp-abc")
        monkeypatch.setattr(approve, "SlackWeb", lambda token: FakeWeb(token, **kw))
        return asyncio.run(approve.check_slack_token(cfg or approve.with_defaults(good_raw())))

    def test_valid_token(self, monkeypatch):
        assert self.run(monkeypatch).ok

    def test_rejected_token(self, monkeypatch):
        check = self.run(monkeypatch, error=approve.SlackError("invalid_auth"))
        assert not check.ok and "approve --init" in check.hint

    def test_missing_scopes_are_named(self, monkeypatch):
        check = self.run(monkeypatch, scopes={"search:read"})
        assert not check.ok and "reactions:read" in check.detail

    def test_token_of_another_account(self, monkeypatch):
        check = self.run(monkeypatch, slack_id="U0OTHER001")
        assert not check.ok and "≠" in check.detail

    def test_presence_check(self, monkeypatch):
        assert not approve.check_token_present().ok
        approve.save_slack_token("xoxp-abc")
        assert approve.check_token_present().ok
