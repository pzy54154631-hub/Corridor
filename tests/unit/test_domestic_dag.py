"""DAG presentation must preserve dependency edges and escape untrusted labels."""

from copy import deepcopy
from xml.etree import ElementTree

from corridor.ui.dag import dag_svg


def test_graph_contains_real_edges_states_and_safe_labels_without_mutation():
    snapshot = {
        "plan": {
            "nodes": [
                {"node_id": "a", "tool": "lock_funds", "depends_on": []},
                {"node_id": "b", "tool": "<script>alert(1)</script>", "depends_on": ["a"]},
                {"node_id": "c", "tool": "merchant_order", "depends_on": ["a"]},
            ]
        },
        "nodes": {
            "a": {"status": "succeeded"},
            "b": {"status": "failed"},
            "c": {"status": "handoff"},
        },
    }
    before = deepcopy(snapshot)
    svg = dag_svg(snapshot)
    tree = ElementTree.fromstring(svg)
    ns = {"svg": "http://www.w3.org/2000/svg"}
    assert len(tree.findall(".//svg:path[@data-edge]", ns)) == 2
    assert {node.attrib["data-status"] for node in tree.findall(".//svg:g[@data-status]", ns)} == {
        "succeeded",
        "failed",
        "handoff",
    }
    assert not tree.findall(".//script") and "<script>" not in svg
    assert snapshot == before


def test_graph_cycle_never_recurses_forever_or_invents_execution():
    snapshot = {
        "plan": {
            "nodes": [
                {"node_id": "a", "tool": "transfer", "depends_on": ["b"]},
                {"node_id": "b", "tool": "transfer", "depends_on": ["a"]},
            ]
        }
    }
    assert 'data-status="pending"' in dag_svg(snapshot)
