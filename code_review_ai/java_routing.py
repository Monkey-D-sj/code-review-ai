"""Synthetic Spring MockMvc route edges.

Bridges the gap between MockMvc-based controller tests and controller methods:
a test method that performs ``get("/owners")`` is connected to the controller
method whose ``@GetMapping("/owners")`` matches, so a changed controller method
is reachable from its test in flow traversal. The edge carries kind='call' and
resolution='resolved' so flow_builder traverses it like any real call.
"""

from code_review_ai.parser import ParsedFile


def build_route_edges(parsed_files: list[ParsedFile],
                      existing_qnames: set[str]) -> list:
    """Match test MockMvc requests to controller mappings; emit synthetic
    resolved call edges test_method -> controller_method."""
    from code_review_ai.resolver import Edge  # lazy — avoid import cycle

    controllers = [(node.qualified_name, node.mappings)
                   for pf in parsed_files for node in pf.nodes if node.mappings]
    tests = [(node.qualified_name, node.mockmvc_requests, node.file_path)
             for pf in parsed_files for node in pf.nodes if node.mockmvc_requests]
    edges: list = []
    seen: set[tuple[str, str]] = set()
    for test_qn, requests, test_file in tests:
        for request_method, request_path in requests:
            test_segs = _normalize_path(request_path)
            for ctrl_qn, mappings in controllers:
                for ctrl_method, ctrl_path in mappings:
                    if ctrl_method != "ANY" and ctrl_method != request_method:
                        continue
                    if not _segments_match(test_segs, _normalize_path(ctrl_path)):
                        continue
                    key = (test_qn, ctrl_qn)
                    if key not in seen:
                        seen.add(key)
                        edges.append(Edge(
                            source=test_qn, target=ctrl_qn, kind="call",
                            file_path=test_file,
                            resolution="resolved" if ctrl_qn in existing_qnames
                            else "unresolved",
                            origin="framework",
                            rule_id="JAVA-F01",
                            evidence_json={
                                "method": request_method, "path": request_path,
                            },
                        ))
                    break
    return edges


def _normalize_path(path: str) -> list[str]:
    """Strip query/fragment and split a URL path into non-empty segments."""
    path = path.split("?", 1)[0].split("#", 1)[0]
    return [segment for segment in path.split("/") if segment]


def _segments_match(test_segs: list[str], ctrl_segs: list[str]) -> bool:
    if len(test_segs) != len(ctrl_segs):
        return False
    for test_segment, ctrl_segment in zip(test_segs, ctrl_segs):
        if test_segment == ctrl_segment:
            continue
        template = lambda segment: segment.startswith("{") and segment.endswith("}")
        if template(test_segment) or template(ctrl_segment):
            continue
        return False
    return True
