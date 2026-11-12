import re
from pathlib import Path

import pytest

import approve

ROOT = Path(__file__).resolve().parents[1]
SHARED = ROOT / ".agents" / "skills" / "setup-approve" / "SKILL.md"
CLAUDE_ENTRY = ROOT / ".claude" / "skills" / "setup-approve" / "SKILL.md"
SKILLS = [SHARED, CLAUDE_ENTRY]


def frontmatter(path):
    match = re.match(r"---\n(.*?)\n---\n", path.read_text(), re.S)
    assert match, f"{path.name} doit commencer par un bloc de métadonnées"
    return dict(line.split(": ", 1) for line in match.group(1).splitlines())


def code_blocks(text):
    return re.findall(r"```(?:bash)?\n(.*?)```", text, re.S)


def approve_commands(path):
    lines = [line.strip() for block in code_blocks(path.read_text()) for line in block.splitlines()]
    return [line for line in lines if line.startswith("approve")]


def cli_options():
    parser = approve.build_parser()
    return {opt for action in parser._actions for opt in action.option_strings}


class TestSkillFiles:
    @pytest.mark.parametrize("path", SKILLS, ids=lambda p: p.parts[-4])
    def test_metadata_is_valid_and_matches_the_folder(self, path):
        meta = frontmatter(path)
        assert meta["name"] == path.parent.name == "setup-approve"
        assert len(meta["description"]) > 40

    def test_both_entries_share_the_same_description(self):
        assert frontmatter(SHARED)["description"] == frontmatter(CLAUDE_ENTRY)["description"]

    def test_the_claude_entry_points_to_the_shared_skill(self):
        assert ".agents/skills/setup-approve/SKILL.md" in CLAUDE_ENTRY.read_text()

    def test_claude_loads_the_common_instructions(self):
        assert (ROOT / "CLAUDE.md").read_text().strip() == "@AGENTS.md"

    def test_the_common_instructions_point_to_the_skill(self):
        assert ".agents/skills/setup-approve/SKILL.md" in (ROOT / "AGENTS.md").read_text()

    def test_the_readme_mentions_the_skill(self):
        assert "setup-approve" in (ROOT / "README.md").read_text()


class TestSkillMatchesTheCli:
    def test_the_skill_documents_commands(self):
        assert len(approve_commands(SHARED)) >= 8

    def test_every_option_it_cites_exists(self):
        known = cli_options()
        cited = {opt for cmd in approve_commands(SHARED) for opt in re.findall(r"--[a-z][a-z-]*", cmd)}
        assert cited, "le skill doit citer des options"
        assert cited <= known, f"options inconnues : {sorted(cited - known)}"

    def test_the_subcommands_it_uses_are_real(self):
        used = {cmd.split()[1] for cmd in approve_commands(SHARED) if len(cmd.split()) > 1}
        subcommands = {name for name in used if not name.startswith(("-", "#"))}
        assert subcommands and subcommands <= set(approve.SUBCOMMANDS), subcommands

    def test_it_never_tells_the_assistant_to_start_the_real_loop(self):
        steps = [cmd for cmd in approve_commands(SHARED) if cmd.split("#")[0].strip() == "approve"]
        assert len(steps) == 1, "seule la passation finale à l'utilisateur peut montrer la boucle réelle"
        final_block = code_blocks(SHARED.read_text())[-1]
        assert steps[0] in final_block
