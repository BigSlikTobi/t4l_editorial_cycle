"""Tests for scripts/build_architecture_graph.py.

Loads the script via importlib (it lives under scripts/, not as an installed
package) so extractor functions can be unit-tested in isolation plus a
smoke test against the real app/ tree.
"""

from __future__ import annotations

import ast
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build_architecture_graph.py"
MANUAL_CONFIG = ROOT / "scripts" / "architecture_graph_manual.yml"


@pytest.fixture(scope="module")
def graph_module():
    spec = importlib.util.spec_from_file_location("build_architecture_graph", SCRIPT)
    module = importlib.util.module_from_spec(spec)
    assert spec.loader is not None
    sys.modules["build_architecture_graph"] = module
    spec.loader.exec_module(module)
    return module


def test_extract_agents_finds_name_and_output_type(graph_module):
    source = """
from agents import Agent
from app.schemas import Foo
def build():
    return Agent(
        name="My Agent",
        instructions="...",
        tools=[some_tool],
        output_type=Foo,
        model="gpt-x",
    )
"""
    tree = ast.parse(source)
    nodes = graph_module.extract_agents("mod.test", tree, "mod/test.py")
    assert len(nodes) == 1
    agent = nodes[0]
    assert agent.label == "My Agent"
    assert agent.type == "agent"
    assert agent.meta["output_type"] == "Foo"


def test_extract_tools_reads_name_override(graph_module):
    source = """
from agents import function_tool
def build_my_tool(adapter):
    @function_tool(name_override="my_tool_name", strict_mode=True)
    async def inner(x: str) -> dict:
        return {}
    return inner
"""
    tree = ast.parse(source)
    nodes, hints = graph_module.extract_tools("mod.test", tree, "mod/test.py")
    assert len(nodes) == 1
    assert nodes[0].label == "my_tool_name"
    assert nodes[0].meta["factory"] == "build_my_tool"
    assert hints == [("my_tool_name", "build_my_tool")]


def test_extract_adapters_picks_up_rest_path(graph_module):
    source = '''
class MyAdapter:
    def __init__(self):
        self._base = "https://x"
    async def do(self):
        await self._client.get("/rest/v1/foo_table")
'''
    tree = ast.parse(source)
    a_nodes, edges, services = graph_module.extract_adapters(
        "mod.adapters", tree, "mod/adapters.py"
    )
    assert any(n.label == "MyAdapter" for n in a_nodes)
    assert any(svc.label == "supabase.foo_table" for svc in services)
    assert any(
        e.source == "adapter:MyAdapter"
        and e.target == "service:supabase.foo_table"
        and e.kind == "adapter_service"
        for e in edges
    )


def test_extract_adapters_skips_exceptions(graph_module):
    source = """
class ExternalServiceError(RuntimeError):
    pass
class _Private:
    pass
class RealAdapter:
    pass
"""
    tree = ast.parse(source)
    a_nodes, _edges, _services = graph_module.extract_adapters(
        "mod.adapters", tree, "mod/adapters.py"
    )
    labels = {n.label for n in a_nodes}
    assert labels == {"RealAdapter"}


def test_extract_schemas_finds_basemodel_subclasses(graph_module):
    source = """
from pydantic import BaseModel
class Foo(BaseModel):
    pass
class Bar:
    pass
class Baz(BaseModel):
    pass
"""
    tree = ast.parse(source)
    nodes = graph_module.extract_schemas("mod.schemas", tree, "mod/schemas.py")
    assert {n.label for n in nodes} == {"Foo", "Baz"}


def test_wiring_edges_from_manual_config(graph_module):
    manual = {
        "agent_tool_edges": [
            {"agent": "A1", "tool": "t1"},
            {"agent": "A2", "tool": "t2", "wraps_agent": "A1"},
            {"agent": "Missing", "tool": "t1"},  # agent not present → dropped
        ]
    }
    edges = graph_module.build_wiring_edges(
        manual, agent_ids={"agent:A1", "agent:A2"}, tool_ids={"tool:t1", "tool:t2"}
    )
    kinds = [(e.source, e.target, e.kind) for e in edges]
    assert ("agent:A1", "tool:t1", "agent_tool") in kinds
    assert ("agent:A2", "tool:t2", "agent_tool") in kinds
    assert ("tool:t2", "agent:A1", "tool_agent") in kinds
    assert not any(e.source == "agent:Missing" for e in edges)


def test_end_to_end_graph_has_expected_shape(graph_module, tmp_path):
    graph = graph_module.build_graph(ROOT / "app", MANUAL_CONFIG)
    nodes = graph["nodes"]
    edges = graph["edges"]

    types: dict[str, int] = {}
    for n in nodes:
        types[n["type"]] = types.get(n["type"], 0) + 1

    assert types.get("agent", 0) >= 3, types
    assert types.get("adapter", 0) >= 5, types
    assert types.get("tool", 0) >= 3, types
    assert types.get("schema", 0) >= 10, types
    assert types.get("module", 0) >= 10, types
    assert types.get("service", 0) >= 3, types

    kinds = {e["kind"] for e in edges}
    assert {"import", "agent_tool", "tool_agent", "adapter_service", "data_flow"} <= kinds

    ids = {n["id"] for n in nodes}
    assert "agent:Editorial Cycle Orchestrator" in ids
    assert "agent:Story Cluster Agent" in ids
    assert "agent:Article Data Agent" in ids
    assert "tool:digest_article" in ids
    assert "tool:analyze_story_cluster" in ids
    assert "adapter:ArticleWriter" in ids

    # HTML renders and embeds the graph JSON.
    out = tmp_path / "graph.html"
    out.write_text(graph_module.render_html(graph), encoding="utf-8")
    text = out.read_text(encoding="utf-8")
    assert "const GRAPH =" in text
    assert json.dumps("Editorial Cycle Orchestrator") in text
