from code_review_ai.eval_gold import (
    evaluation_gold_to_dict,
    parse_evaluation_gold,
    score_agent_review,
    score_context,
)


def _record():
    return {
        "gold": {
            "root_causes": [{
                "id": "silent-failure",
                "fix_file": "app/service.py",
                "alternate_files": ["app/caller.py"],
                "mechanism_terms": ["empty", "exception"],
                "min_matches": 2,
            }],
            "context": {
                "symbols": ["app.caller::run"],
                "files": ["app/caller.py"],
                "entries": ["app.api::post"],
                "tests": ["tests/test_api.py"],
                "hard_negatives": {
                    "symbols": ["app.unrelated::run"],
                    "files": ["app/unrelated.py"],
                },
            },
        },
    }


def test_parse_structured_gold_and_legacy_adapter():
    gold = parse_evaluation_gold(_record(), "case")
    assert gold.root_causes[0].file == "app/service.py"
    assert gold.root_causes[0].keywords == ("empty", "exception")
    assert gold.context.symbols == ("app.caller::run",)
    assert gold.context.hard_negative_files == ("app/unrelated.py",)
    assert evaluation_gold_to_dict(gold)["context"]["files"] == [
        "app/caller.py"]

    legacy = parse_evaluation_gold({
        "gold_findings": [{
            "id": "old", "file": "app\\service.py",
            "keywords": ["failure"],
        }],
        "gold_files": ["app\\caller.py"],
    }, "legacy")
    assert legacy.root_causes[0].file == "app/service.py"
    assert legacy.context.files == ("app/caller.py",)


def test_graph_context_scores_dimensions_and_hard_negatives_separately():
    gold = parse_evaluation_gold(_record(), "case")
    score = score_context({
        "symbols": ["app.caller::run", "app.unrelated::run"],
        "files": ["app/caller.py", "app/unrelated.py"],
        "entries": ["app.api::post"],
        "tests": [],
    }, gold.context)

    assert score["symbols"]["recall"] == 1.0
    assert score["symbols"]["precision"] == 0.5
    assert score["tests"]["recall"] == 0.0
    assert score["hard_negatives"]["correctness"] == 0.0


def test_agent_review_uses_root_cause_and_structured_context_from_same_gold():
    gold = parse_evaluation_gold(_record(), "case")
    score = score_agent_review({
        "findings": [{
            "file": "app/caller.py", "line": 10,
            "title": "Exception becomes empty result",
            "description": "The exception is replaced by an empty value.",
        }],
        "affected_symbols": ["app.caller::run"],
        "affected_files": ["app/caller.py"],
        "affected_entries": ["app.api::post"],
        "tests": ["tests/test_api.py"],
    }, gold)

    assert score["root_causes"]["f1"] == 1.0
    assert score["affected_context"]["macro_recall"] == 1.0
    assert score["affected_context"]["hard_negatives"]["correctness"] == 1.0
