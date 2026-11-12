import subprocess

import pytest

import approve

ENV = {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.com", "GIT_COMMITTER_NAME": "t",
       "GIT_COMMITTER_EMAIL": "t@example.com", "PATH": "/usr/bin:/bin:/usr/local/bin:/opt/homebrew/bin", "HOME": "/tmp"}


def sh(cwd, *args):
    return subprocess.run(args, cwd=cwd, env=ENV, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repos(tmp_path, monkeypatch):
    origin, work, clone = tmp_path / "origin.git", tmp_path / "work", tmp_path / "clone"
    sh(tmp_path, "git", "init", "-q", "--bare", "-b", "main", str(origin))
    sh(tmp_path, "git", "clone", "-q", str(origin), str(work))
    (work / "approve.py").write_text("v1\n")
    sh(work, "git", "add", "-A")
    sh(work, "git", "commit", "-q", "-m", "first")
    sh(work, "git", "push", "-q", "origin", "HEAD:main")
    sh(tmp_path, "git", "clone", "-q", str(origin), str(clone))
    monkeypatch.setattr(approve, "repo_root", lambda: clone)
    monkeypatch.setattr(approve, "lock_held", lambda: False)
    monkeypatch.setattr(approve, "git", lambda root, *a: subprocess.run(
        ["git", "-C", str(root), *a], capture_output=True, text=True, env=ENV))

    def publish(message):
        (work / "approve.py").write_text(message + "\n")
        sh(work, "git", "commit", "-q", "-am", message)
        sh(work, "git", "push", "-q", "origin", "HEAD:main")

    return origin, work, clone, publish


class TestUpdate:
    def test_already_up_to_date(self, repos, capsys):
        assert approve.cmd_update() == 0
        assert "Déjà à jour" in capsys.readouterr().out

    def test_pulls_new_commits_and_lists_them(self, repos, capsys):
        _, _, clone, publish = repos
        publish("feat: one")
        publish("fix: two")
        assert approve.cmd_update() == 0
        out = capsys.readouterr().out
        assert "2 nouveauté(s)" in out and "feat: one" in out and "fix: two" in out and "Mise à jour faite" in out
        assert (clone / "approve.py").read_text().strip() == "fix: two"

    def test_a_running_instance_is_reminded_to_restart(self, repos, capsys, monkeypatch):
        _, _, _, publish = repos
        publish("feat: one")
        monkeypatch.setattr(approve, "lock_held", lambda: True)
        approve.cmd_update()
        assert "approve --stop" in capsys.readouterr().out

    def test_local_changes_are_never_touched(self, repos, capsys):
        _, _, clone, publish = repos
        publish("feat: one")
        (clone / "approve.py").write_text("my edit\n")
        assert approve.cmd_update() == 1
        assert "modifications locales" in capsys.readouterr().out
        assert (clone / "approve.py").read_text() == "my edit\n"

    def test_another_branch_is_refused(self, repos, capsys):
        _, _, clone, _ = repos
        sh(clone, "git", "checkout", "-q", "-b", "wip")
        assert approve.cmd_update() == 1
        assert "pas sur main" in capsys.readouterr().out

    def test_diverged_copy_is_not_overwritten(self, repos, capsys):
        _, _, clone, publish = repos
        (clone / "extra.txt").write_text("local\n")
        sh(clone, "git", "add", "-A")
        sh(clone, "git", "commit", "-q", "-m", "local only")
        publish("feat: remote")
        assert approve.cmd_update() == 1
        assert (clone / "extra.txt").exists() and "Mise à jour impossible" in capsys.readouterr().out

    def test_unreachable_origin_is_reported(self, repos, capsys):
        origin, _, _, _ = repos
        sh(origin.parent, "mv", str(origin), str(origin) + ".gone")
        assert approve.cmd_update() == 1
        assert "Impossible de joindre GitHub" in capsys.readouterr().out

    def test_not_a_git_clone(self, tmp_path, monkeypatch, capsys):
        monkeypatch.setattr(approve, "repo_root", lambda: tmp_path)
        assert approve.cmd_update() == 1
        assert "pas un clone Git" in capsys.readouterr().out


class TestVersion:
    def test_shows_the_commit_and_its_date(self, repos):
        text = approve.version_text()
        assert text.startswith("approve ") and "(" in text

    def test_outside_a_clone_it_says_so(self, tmp_path, monkeypatch):
        monkeypatch.setattr(approve, "repo_root", lambda: tmp_path)
        assert "version inconnue" in approve.version_text()

    def test_routed_from_the_command_line(self, repos, capsys):
        assert approve.main(["--version"]) == 0
        assert capsys.readouterr().out.startswith("approve ")

    def test_update_is_routed_from_the_command_line(self, repos, capsys):
        assert approve.main(["--update"]) == 0
        assert "Déjà à jour" in capsys.readouterr().out
