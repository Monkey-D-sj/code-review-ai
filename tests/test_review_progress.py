from code_review_ai.review_agent.progress import ReviewProgressDisplay


def test_progress_display_tracks_model_rounds_and_final_result():
    display = ReviewProgressDisplay("fake-model")
    display.on_event("incremental_sync_finished", {})
    display.on_event("summary_ready", {"changed_symbols": 2,
                                       "uncovered_changes": 1})
    display.on_event("model_request_started", {"turn": 1, "final_only": False})
    assert display.active_model_call is True
    display.on_event("model_response_received", {"turn": 1,
                                                   "response_chars": 42,
                                                   "tool_calls": 1})
    display.on_event("tool_requests", {"names": ["get_impact"], "calls": [{
        "name": "get_impact", "args": {"symbols": ["auth::login"]}}]})
    display.on_event("finished", {"findings": 3, "tool_calls": 1, "failed": False})

    assert display.active_model_call is False
    assert display.model_turn == 1
    assert display.tool_calls == 1
    assert display.findings == 3
    assert display.completed_steps == 4
    assert display.failed is False
