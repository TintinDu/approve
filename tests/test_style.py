import pytest

import approve


@pytest.fixture
def tty(monkeypatch):
    monkeypatch.delenv("NO_COLOR", raising=False)
    monkeypatch.setenv("TERM", "xterm-256color")
    monkeypatch.setattr(approve, "color_enabled", lambda stream=None: True)


class TestPlainOutput:
    def test_log_is_unchanged_without_a_terminal(self, capsys):
        approve.log("  approuvée 1!1 titre", "ok")
        out = capsys.readouterr().out
        assert out.endswith("] " + "  approuvée 1!1 titre\n") and "\x1b" not in out

    def test_no_banner_and_no_heading_without_a_terminal(self, capsys):
        approve.print_banner()
        approve.heading("Statut")
        assert capsys.readouterr().out == ""

    def test_heading_falls_back_to_the_plain_text(self, capsys):
        approve.heading("Vérification", "Vérification")
        assert capsys.readouterr().out == "Vérification\n"

    def test_checks_keep_their_classic_marks(self):
        assert (approve.check_mark(True), approve.check_mark(False)) == ("✓", "✗")

    @pytest.mark.parametrize("env", [{"NO_COLOR": "1"}, {"TERM": "dumb"}])
    def test_no_color_and_dumb_terminals_stay_plain(self, monkeypatch, env):
        class Tty:
            def isatty(self):
                return True
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm")
        for key, value in env.items():
            monkeypatch.setenv(key, value)
        assert approve.color_enabled(Tty()) is False

    def test_a_real_terminal_gets_color(self, monkeypatch):
        class Tty:
            def isatty(self):
                return True
        monkeypatch.delenv("NO_COLOR", raising=False)
        monkeypatch.setenv("TERM", "xterm")
        assert approve.color_enabled(Tty()) is True


class TestColoredOutput:
    def test_each_event_kind_has_its_own_mark_and_color(self, tty, capsys):
        for kind in approve.LOG_MARKS:
            approve.log("  message", kind)
        out = capsys.readouterr().out
        for mark, color in approve.LOG_MARKS.values():
            assert mark in out and f"\x1b[{approve.ANSI[color]}" in out

    def test_unknown_kind_is_printed_as_plain_text(self, tty, capsys):
        approve.log("  message")
        assert capsys.readouterr().out.endswith("]\x1b[0m   message\n")

    def test_banner_is_five_rows_inside_eighty_columns(self, tty, capsys):
        rows = approve.banner_rows()
        assert len(rows) == 5 and max(len(row) for row in rows) <= 80
        approve.print_banner()
        out = capsys.readouterr().out
        assert "LGTM" in out and out.count("\x1b[0m") == 6

    def test_banner_has_no_pixel_outside_the_font(self):
        assert {ch for row in approve.banner_rows() for ch in row} <= {" ", "█"}

    def test_checks_use_pixel_marks(self, tty):
        assert "■" in approve.check_mark(True) and "▚" in approve.check_mark(False)
