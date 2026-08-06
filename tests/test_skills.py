"""Structure guard for the bundled language-review skills."""
import importlib.resources
import re

SKILL_NAMES = (
    "code-review-langs",
    "code-review-python",
    "code-review-typescript",
    "code-review-javascript",
    "code-review-java",
)
LANGUAGE_SKILLS = SKILL_NAMES[1:]
REQUIRED_SECTIONS = ("安全", "正确性", "性能", "架构", "语言特有")


def _skills_source():
    return importlib.resources.files("code_review_ai").joinpath("skills")


def _read(name: str) -> str:
    return (_skills_source() / name / "SKILL.md").read_text(encoding="utf-8")


def _frontmatter(text: str) -> dict[str, str]:
    body = text.split("---", 2)[1]
    result = {}
    for line in body.strip().splitlines():
        if ":" in line:
            key, _, value = line.partition(":")
            result[key.strip()] = value.strip().strip('"')
    return result


def test_each_skill_is_a_directory_with_matching_frontmatter():
    for name in SKILL_NAMES:
        fm = _frontmatter(_read(name))
        assert fm.get("name") == name
        assert fm.get("description")


def test_entry_lists_exactly_the_language_skills():
    entry = _read("code-review-langs")
    referenced = set(re.findall(r"code-review-(?:python|typescript|javascript|java)", entry))
    assert referenced == set(LANGUAGE_SKILLS)


def test_language_skills_have_all_required_sections():
    for name in LANGUAGE_SKILLS:
        body = _read(name)
        for section in REQUIRED_SECTIONS:
            assert f"## {section}" in body, f"{name} missing '## {section}'"
        header_count = len(re.findall(r"^## ", body, flags=re.MULTILINE))
        expected = len(REQUIRED_SECTIONS) + 1
        assert header_count == expected, (
            f"{name} has {header_count} '## ' sections, expected {expected} "
            f"({len(REQUIRED_SECTIONS)} required + '## 审核方式')")
