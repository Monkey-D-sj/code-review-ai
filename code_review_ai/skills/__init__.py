"""Bundled review skills and loaders.

The skill directories under this package are deployed to the platform's
user-scope skills dir by ``installer.deploy_skills``. ``load_skill_body`` reads
a skill's SKILL.md body (frontmatter stripped) so prompt consumers — the eval
harness and the post-commit hook — inline the same text the interactive skill
carries, keeping the review methodology in one place.
"""

from __future__ import annotations

import importlib.resources
import re

# ``\r?\n`` because a Windows checkout writes these files back with CRLF, and a
# frontmatter block that fails to match does not error -- it ships the YAML into
# whatever prompt the body was destined for.
_FRONTMATTER_RE = re.compile(r"^---\r?\n.*?\r?\n---\r?\n", re.DOTALL)


def strip_frontmatter(text: str) -> str:
    """Drop a leading YAML frontmatter block, if the text carries one."""
    return _FRONTMATTER_RE.sub("", text).strip()


def load_skill_body(name: str) -> str:
    """Return the body of a bundled skill's SKILL.md, frontmatter stripped."""
    path = importlib.resources.files("code_review_ai").joinpath(
        "skills", name, "SKILL.md")
    return strip_frontmatter(path.read_text(encoding="utf-8"))
