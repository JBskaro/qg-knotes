# SPDX-License-Identifier: GPL-3.0-only
"""The skill for AI assistants (skills/qg-knotes) follows the Agent Skills format and names the same tools, parameters
and values as the MCP server: a tool added to the server without the skill fails here."""

import pathlib
import re
import unittest

from knotes import ai_tools, kinds, tasks

SKILL = pathlib.Path(__file__).parents[1] / "skills" / "qg-knotes"
NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
ROW = re.compile(r"^\| `(\w+)` \|(.*)\|$", re.MULTILINE)


def _frontmatter(text: str) -> dict[str, str]:
    head = text.split("---\n")[1]
    return dict(line.split(": ", 1) for line in head.splitlines() if line.strip())


class TestSkill(unittest.TestCase):
    def setUp(self):
        self.skill = (SKILL / "SKILL.md").read_text(encoding="utf-8")
        self.reference = (SKILL / "references" / "tools.md").read_text(encoding="utf-8")

    def test_agent_skills_format(self):
        fields = _frontmatter(self.skill)
        self.assertEqual(fields["name"], SKILL.name)  # the name matches the folder
        self.assertRegex(fields["name"], NAME)
        self.assertLessEqual(len(fields["name"]), 64)
        self.assertLessEqual(len(fields["description"]), 1024)
        self.assertLessEqual(len(fields["compatibility"]), 500)
        self.assertLess(self.skill.count("\n"), 500)

    def test_same_tools_as_the_server(self):
        self.assertEqual({name for name, _ in ROW.findall(self.skill)}, set(ai_tools.TOOLS))
        self.assertEqual({name for name, _ in ROW.findall(self.reference)}, set(ai_tools.TOOLS))

    def test_parameters_and_required_ones(self):
        for name, row in ROW.findall(self.reference):
            tool = ai_tools.TOOLS[name]
            with self.subTest(tool=name):
                self.assertEqual(set(re.findall(r"`(\w+)`", row)) & set(tool.params), set(tool.params))
                self.assertEqual(set(re.findall(r"\*\*`(\w+)`\*\*", row)), set(tool.required))

    def test_values_as_the_server_takes_them(self):
        values = set(re.findall(r"«([^»]+)»", self.reference))
        self.assertLessEqual(set(tasks.STATES), values)
        self.assertLessEqual({p for p in tasks.PRIORITIES if p}, values)
        self.assertLessEqual({kind.name for kind in kinds.kinds()}, values)
        self.assertLessEqual(set(ai_tools.KINDS), values)  # views of get_protocol
