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

_FRONTMATTER_RE = re.compile(r"^---\n.*?\n---\n", re.DOTALL)


def load_skill_body(name: str) -> str:
    """Return the body of a bundled skill's SKILL.md, frontmatter stripped."""
    path = importlib.resources.files("code_review_ai").joinpath(
        "skills", name, "SKILL.md")
    return _FRONTMATTER_RE.sub("", path.read_text(encoding="utf-8")).strip()
