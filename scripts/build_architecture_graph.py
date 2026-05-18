"""Living architecture graph for t4l_editorial_cycle.

Statically analyzes the `app/` package and emits a standalone interactive HTML
graph at `var/architecture_graph.html`. Nodes: modules, agents, tools,
adapters, external services, and pydantic schemas. Edges: imports (call
graph), agent↔tool wiring, adapter→service links, and schema-labeled data
flows. Zoomable clusters, layer/kind filters, click-highlight upstream +
downstream, 2D/3D toggle.

Usage:
    ./venv/bin/python scripts/build_architecture_graph.py

All rendering deps are loaded from CDNs; no npm/build step required.
"""

from __future__ import annotations

import argparse
import ast
import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

import yaml

ROOT = Path(__file__).resolve().parents[1]
APP_DIR = ROOT / "app"
DEFAULT_OUTPUT = ROOT / "var" / "architecture_graph.html"
DEFAULT_MANUAL_CONFIG = ROOT / "scripts" / "architecture_graph_manual.yml"


# ---------- Data model ----------


@dataclass
class Node:
    id: str
    label: str
    type: str  # module | agent | tool | adapter | service | schema
    cluster: str = ""
    layer: str = ""
    file: str = ""
    line: int = 0
    meta: dict[str, Any] = field(default_factory=dict)


@dataclass
class Edge:
    source: str
    target: str
    kind: str  # import | agent_tool | tool_agent | adapter_service | data_flow
    label: str = ""


# ---------- Module + import extraction ----------


def _module_name(path: Path) -> str:
    rel = path.relative_to(ROOT).with_suffix("")
    return ".".join(rel.parts)


def _module_cluster(mod: str) -> str:
    parts = mod.split(".")
    if len(parts) >= 3 and parts[0] == "app":
        return f"{parts[0]}.{parts[1]}"
    return parts[0] if parts else ""


def _infer_layer(path: Path) -> str:
    name = path.name
    if name == "adapters.py":
        return "adapter_host"
    if name == "agents.py" or name == "persona_selector.py":
        return "agent_host"
    if name == "tools.py":
        return "tool_host"
    if name == "schemas.py":
        return "schema_host"
    if name == "config.py":
        return "config"
    return "module"


def extract_modules_and_imports(
    app_dir: Path,
) -> tuple[list[Node], list[Edge], dict[str, ast.Module]]:
    nodes: list[Node] = []
    edges: list[Edge] = []
    trees: dict[str, ast.Module] = {}

    known_modules: set[str] = set()
    for py_file in sorted(app_dir.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        if "__pycache__" in py_file.parts:
            continue
        mod = _module_name(py_file)
        known_modules.add(mod)

    for py_file in sorted(app_dir.rglob("*.py")):
        if py_file.name == "__init__.py":
            continue
        if "__pycache__" in py_file.parts:
            continue
        mod = _module_name(py_file)
        source = py_file.read_text(encoding="utf-8")
        try:
            tree = ast.parse(source, filename=str(py_file))
        except SyntaxError:
            continue
        trees[mod] = tree

        nodes.append(
            Node(
                id=f"module:{mod}",
                label=mod.split(".")[-1],
                type="module",
                cluster=_module_cluster(mod),
                layer=_infer_layer(py_file),
                file=str(py_file.relative_to(ROOT)),
                line=1,
                meta={"full_name": mod},
            )
        )

        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                target = node.module
                if target in known_modules:
                    edges.append(
                        Edge(source=f"module:{mod}", target=f"module:{target}", kind="import")
                    )
            elif isinstance(node, ast.Import):
                for alias in node.names:
                    if alias.name in known_modules:
                        edges.append(
                            Edge(
                                source=f"module:{mod}",
                                target=f"module:{alias.name}",
                                kind="import",
                            )
                        )

    return nodes, edges, trees


# ---------- Agent extraction ----------


def _literal_kwarg(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Constant) and isinstance(kw.value.value, str):
            return kw.value.value
    return None


def _name_kwarg(call: ast.Call, name: str) -> str | None:
    for kw in call.keywords:
        if kw.arg == name and isinstance(kw.value, ast.Name):
            return kw.value.id
    return None


def extract_agents(module: str, tree: ast.Module, file_path: str) -> list[Node]:
    """Find `Agent(name=..., output_type=..., tools=[...])` calls."""
    nodes: list[Node] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        func = node.func
        is_agent_ctor = (isinstance(func, ast.Name) and func.id == "Agent") or (
            isinstance(func, ast.Attribute) and func.attr == "Agent"
        )
        if not is_agent_ctor:
            continue
        name = _literal_kwarg(node, "name")
        if not name:
            continue
        output_type = _name_kwarg(node, "output_type") or ""
        model_expr = ""
        for kw in node.keywords:
            if kw.arg == "model":
                try:
                    model_expr = ast.unparse(kw.value)
                except Exception:
                    model_expr = ""
        nodes.append(
            Node(
                id=f"agent:{name}",
                label=name,
                type="agent",
                cluster=_module_cluster(module),
                layer="agent",
                file=file_path,
                line=node.lineno,
                meta={"output_type": output_type, "model": model_expr, "defined_in": module},
            )
        )
    return nodes


# ---------- Tool extraction ----------


def extract_tools(
    module: str, tree: ast.Module, file_path: str
) -> tuple[list[Node], list[tuple[str, str]]]:
    """Find `@function_tool(name_override=...)` decorated inner funcs.

    Returns (tool_nodes, tool_wraps_agent_hints).
    The hint is (tool_name, enclosing_factory_name) — used later to join with
    the manual config to infer which agent each tool wraps.
    """
    nodes: list[Node] = []
    hints: list[tuple[str, str]] = []

    for top in tree.body:
        if not isinstance(top, ast.FunctionDef):
            continue
        factory_name = top.name
        for inner in ast.walk(top):
            if not isinstance(inner, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            tool_name: str | None = None
            for dec in inner.decorator_list:
                call = dec if isinstance(dec, ast.Call) else None
                dec_func = call.func if call else dec
                is_ft = (
                    (isinstance(dec_func, ast.Name) and dec_func.id == "function_tool")
                    or (isinstance(dec_func, ast.Attribute) and dec_func.attr == "function_tool")
                )
                if not is_ft:
                    continue
                if call:
                    tool_name = _literal_kwarg(call, "name_override") or inner.name
                else:
                    tool_name = inner.name
            if tool_name:
                nodes.append(
                    Node(
                        id=f"tool:{tool_name}",
                        label=tool_name,
                        type="tool",
                        cluster=_module_cluster(module),
                        layer="tool",
                        file=file_path,
                        line=inner.lineno,
                        meta={"factory": factory_name, "defined_in": module},
                    )
                )
                hints.append((tool_name, factory_name))
    return nodes, hints


# ---------- Adapter extraction ----------

_TABLE_RE = re.compile(r"/rest/v1/([A-Za-z_][A-Za-z0-9_]*)")
_STORAGE_RE = re.compile(r"/storage/v1/object/(?:public/)?([A-Za-z_][A-Za-z0-9_-]*)")


def extract_adapters(
    module: str, tree: ast.Module, file_path: str
) -> tuple[list[Node], list[Edge], list[Node]]:
    """Return (adapter_nodes, adapter→service edges, auto-detected service nodes)."""
    adapter_nodes: list[Node] = []
    edges: list[Edge] = []
    service_nodes: dict[str, Node] = {}

    for top in tree.body:
        if not isinstance(top, ast.ClassDef):
            continue
        # Skip private base/exception classes (underscore-prefixed or look like errors).
        if top.name.startswith("_"):
            continue
        # Skip exceptions, pydantic models, and @dataclass value objects.
        if any(
            (isinstance(b, ast.Name) and b.id in {"RuntimeError", "Exception", "BaseModel"})
            or (isinstance(b, ast.Attribute) and b.attr == "BaseModel")
            for b in top.bases
        ):
            continue
        if any(
            (isinstance(d, ast.Name) and d.id == "dataclass")
            or (isinstance(d, ast.Call) and isinstance(d.func, ast.Name) and d.func.id == "dataclass")
            or (isinstance(d, ast.Attribute) and d.attr == "dataclass")
            for d in top.decorator_list
        ):
            continue
        adapter_nodes.append(
            Node(
                id=f"adapter:{top.name}",
                label=top.name,
                type="adapter",
                cluster=_module_cluster(module),
                layer="adapter",
                file=file_path,
                line=top.lineno,
                meta={"defined_in": module},
            )
        )
        # Scan class body string literals for URL paths → service edges.
        class_source = ast.unparse(top)
        for table in sorted(set(_TABLE_RE.findall(class_source))):
            service_id = f"service:supabase.{table}"
            service_nodes.setdefault(
                service_id,
                Node(
                    id=service_id,
                    label=f"supabase.{table}",
                    type="service",
                    cluster="external",
                    layer="service",
                    meta={"kind": "supabase_table"},
                ),
            )
            edges.append(
                Edge(source=f"adapter:{top.name}", target=service_id, kind="adapter_service")
            )
        for bucket in sorted(set(_STORAGE_RE.findall(class_source))):
            if bucket == "public":
                # regex false-positive: bucket name is an f-string placeholder
                continue
            service_id = f"service:supabase.storage.{bucket}"
            service_nodes.setdefault(
                service_id,
                Node(
                    id=service_id,
                    label=f"supabase.storage.{bucket}",
                    type="service",
                    cluster="external",
                    layer="service",
                    meta={"kind": "supabase_storage"},
                ),
            )
            edges.append(
                Edge(source=f"adapter:{top.name}", target=service_id, kind="adapter_service")
            )

    return adapter_nodes, edges, list(service_nodes.values())


# ---------- Schema extraction ----------


def extract_schemas(module: str, tree: ast.Module, file_path: str) -> list[Node]:
    nodes: list[Node] = []
    for top in tree.body:
        if not isinstance(top, ast.ClassDef):
            continue
        if not any(
            (isinstance(b, ast.Name) and b.id == "BaseModel")
            or (isinstance(b, ast.Attribute) and b.attr == "BaseModel")
            for b in top.bases
        ):
            continue
        nodes.append(
            Node(
                id=f"schema:{top.name}",
                label=top.name,
                type="schema",
                cluster=_module_cluster(module),
                layer="schema",
                file=file_path,
                line=top.lineno,
                meta={"defined_in": module},
            )
        )
    return nodes


# ---------- Manual config + wiring ----------


def load_manual_config(path: Path) -> dict:
    if not path.exists():
        return {}
    return yaml.safe_load(path.read_text(encoding="utf-8")) or {}


def build_wiring_edges(
    manual: dict,
    agent_ids: set[str],
    tool_ids: set[str],
) -> list[Edge]:
    edges: list[Edge] = []
    for entry in manual.get("agent_tool_edges", []) or []:
        agent_id = f"agent:{entry['agent']}"
        tool_id = f"tool:{entry['tool']}"
        if agent_id in agent_ids and tool_id in tool_ids:
            edges.append(Edge(source=agent_id, target=tool_id, kind="agent_tool"))
        wraps = entry.get("wraps_agent")
        if wraps:
            wrapped_id = f"agent:{wraps}"
            if tool_id in tool_ids and wrapped_id in agent_ids:
                edges.append(Edge(source=tool_id, target=wrapped_id, kind="tool_agent"))
    return edges


def build_adapter_service_edges(
    manual: dict, adapter_ids: set[str], service_ids: set[str]
) -> list[Edge]:
    edges: list[Edge] = []
    for entry in manual.get("adapter_service_edges", []) or []:
        adapter_id = f"adapter:{entry['adapter']}"
        service_id = entry["service"]
        if adapter_id in adapter_ids and service_id in service_ids:
            edges.append(Edge(source=adapter_id, target=service_id, kind="adapter_service"))
    return edges


def build_manual_service_nodes(manual: dict) -> list[Node]:
    nodes: list[Node] = []
    for entry in manual.get("external_services", []) or []:
        nodes.append(
            Node(
                id=entry["id"],
                label=entry["label"],
                type="service",
                cluster="external",
                layer="service",
                meta=entry.get("meta", {}) or {},
            )
        )
    return nodes


def build_data_flow_edges(
    agent_nodes: list[Node], schema_ids: set[str]
) -> list[Edge]:
    edges: list[Edge] = []
    for agent in agent_nodes:
        output_type = agent.meta.get("output_type") or ""
        if not output_type:
            continue
        schema_id = f"schema:{output_type}"
        if schema_id in schema_ids:
            edges.append(
                Edge(source=agent.id, target=schema_id, kind="data_flow", label=output_type)
            )
    return edges


# ---------- Graph assembly ----------


def build_graph(app_dir: Path, manual_path: Path) -> dict:
    module_nodes, import_edges, trees = extract_modules_and_imports(app_dir)
    all_nodes: list[Node] = list(module_nodes)
    all_edges: list[Edge] = list(import_edges)

    agent_nodes: list[Node] = []
    tool_nodes: list[Node] = []
    adapter_nodes: list[Node] = []
    service_nodes: dict[str, Node] = {}
    schema_nodes: list[Node] = []

    for mod, tree in trees.items():
        rel_file = (app_dir / Path(*mod.split(".")[1:])).with_suffix(".py")
        try:
            file_path = str(rel_file.relative_to(ROOT))
        except ValueError:
            file_path = str(rel_file)

        agent_nodes.extend(extract_agents(mod, tree, file_path))
        tools, _hints = extract_tools(mod, tree, file_path)
        tool_nodes.extend(tools)

        if rel_file.name in {"adapters.py", "image_clients.py", "image_validator.py"}:
            a_nodes, a_edges, a_services = extract_adapters(mod, tree, file_path)
            adapter_nodes.extend(a_nodes)
            all_edges.extend(a_edges)
            for svc in a_services:
                service_nodes.setdefault(svc.id, svc)

        if rel_file.name == "schemas.py":
            schema_nodes.extend(extract_schemas(mod, tree, file_path))

    manual = load_manual_config(manual_path)
    for svc in build_manual_service_nodes(manual):
        service_nodes.setdefault(svc.id, svc)

    all_nodes.extend(agent_nodes)
    all_nodes.extend(tool_nodes)
    all_nodes.extend(adapter_nodes)
    all_nodes.extend(schema_nodes)
    all_nodes.extend(service_nodes.values())

    agent_ids = {n.id for n in agent_nodes}
    tool_ids = {n.id for n in tool_nodes}
    adapter_ids = {n.id for n in adapter_nodes}
    service_ids = set(service_nodes.keys())
    schema_ids = {n.id for n in schema_nodes}

    all_edges.extend(build_wiring_edges(manual, agent_ids, tool_ids))
    all_edges.extend(build_adapter_service_edges(manual, adapter_ids, service_ids))
    all_edges.extend(build_data_flow_edges(agent_nodes, schema_ids))

    # Dedupe edges.
    seen: set[tuple[str, str, str, str]] = set()
    unique_edges: list[Edge] = []
    for e in all_edges:
        key = (e.source, e.target, e.kind, e.label)
        if key in seen:
            continue
        seen.add(key)
        unique_edges.append(e)

    # Dedupe nodes (by id, first wins).
    seen_ids: set[str] = set()
    unique_nodes: list[Node] = []
    for n in all_nodes:
        if n.id in seen_ids:
            continue
        seen_ids.add(n.id)
        unique_nodes.append(n)

    return {
        "nodes": [asdict(n) for n in unique_nodes],
        "edges": [asdict(e) for e in unique_edges],
    }


# ---------- HTML emitter ----------


_HTML_TEMPLATE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<title>t4l_editorial_cycle — Architecture Graph</title>
<script src="https://unpkg.com/cytoscape@3.30.2/dist/cytoscape.min.js"></script>
<script src="https://unpkg.com/layout-base/layout-base.js"></script>
<script src="https://unpkg.com/cose-base/cose-base.js"></script>
<script src="https://unpkg.com/cytoscape-cose-bilkent@4.1.0/cytoscape-cose-bilkent.js"></script>
<script src="https://unpkg.com/3d-force-graph"></script>
<style>
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; height: 100%; font: 13px -apple-system, BlinkMacSystemFont, "Segoe UI", sans-serif; background: #0e1116; color: #e6e8eb; }
  #bar { position: fixed; top: 0; left: 0; right: 0; padding: 8px 12px; background: #161a21; border-bottom: 1px solid #222; display: flex; flex-wrap: wrap; gap: 10px; align-items: center; z-index: 10; }
  #bar h1 { font-size: 13px; font-weight: 600; margin: 0 10px 0 0; color: #8b9bb4; }
  #bar label { font-size: 12px; margin-right: 6px; user-select: none; }
  #bar input[type=text] { background: #0e1116; color: #e6e8eb; border: 1px solid #2a313b; border-radius: 4px; padding: 4px 8px; }
  #bar button { background: #2a313b; color: #e6e8eb; border: 1px solid #3a424d; padding: 4px 10px; border-radius: 4px; cursor: pointer; }
  #bar button.active { background: #3a6df0; border-color: #3a6df0; }
  #bar .sep { width: 1px; height: 18px; background: #2a313b; }
  #bar .legend { display: inline-flex; align-items: center; gap: 4px; font-size: 11px; color: #8b9bb4; }
  #bar .legend .swatch { width: 10px; height: 10px; border-radius: 50%; display: inline-block; }
  #cy, #fg { position: fixed; top: 48px; left: 0; right: 0; bottom: 0; }
  #fg { display: none; background: #0e1116; }
  #info { position: fixed; right: 12px; bottom: 12px; max-width: 340px; background: #161a21; border: 1px solid #2a313b; border-radius: 6px; padding: 10px 12px; font-size: 12px; color: #c8d0db; display: none; }
  #info .label { font-weight: 600; color: #e6e8eb; font-size: 13px; margin-bottom: 4px; }
  #info .kv { color: #8b9bb4; }
  #info .kv b { color: #c8d0db; font-weight: normal; }
</style>
</head>
<body>
<div id="bar">
  <h1>t4l_editorial_cycle · architecture</h1>
  <input id="q" type="text" placeholder="search…" />
  <span class="sep"></span>
  <span class="legend"><span class="swatch" style="background:#3a6df0"></span>module</span>
  <span class="legend"><span class="swatch" style="background:#e0a030"></span>agent</span>
  <span class="legend"><span class="swatch" style="background:#50c070"></span>tool</span>
  <span class="legend"><span class="swatch" style="background:#b060e0"></span>adapter</span>
  <span class="legend"><span class="swatch" style="background:#e05060"></span>service</span>
  <span class="legend"><span class="swatch" style="background:#708090"></span>schema</span>
  <span class="sep"></span>
  <label><input type="checkbox" class="layer-f" value="module" checked>module</label>
  <label><input type="checkbox" class="layer-f" value="agent" checked>agent</label>
  <label><input type="checkbox" class="layer-f" value="tool" checked>tool</label>
  <label><input type="checkbox" class="layer-f" value="adapter" checked>adapter</label>
  <label><input type="checkbox" class="layer-f" value="service" checked>service</label>
  <label><input type="checkbox" class="layer-f" value="schema" checked>schema</label>
  <span class="sep"></span>
  <label><input type="checkbox" class="kind-f" value="import" checked>import</label>
  <label><input type="checkbox" class="kind-f" value="agent_tool" checked>agent→tool</label>
  <label><input type="checkbox" class="kind-f" value="tool_agent" checked>tool→agent</label>
  <label><input type="checkbox" class="kind-f" value="adapter_service" checked>adapter→service</label>
  <label><input type="checkbox" class="kind-f" value="data_flow" checked>data</label>
  <span class="sep"></span>
  <button id="view-toggle">3D view</button>
  <button id="reset">reset</button>
</div>
<div id="cy"></div>
<div id="fg"></div>
<div id="info"></div>

<script>
const GRAPH = __GRAPH_JSON__;
const COLORS = { module: '#3a6df0', agent: '#e0a030', tool: '#50c070', adapter: '#b060e0', service: '#e05060', schema: '#708090' };
const EDGE_COLORS = { import: '#2a313b', agent_tool: '#e0a030', tool_agent: '#50c070', adapter_service: '#b060e0', data_flow: '#708090' };

// ---- Cytoscape (2D) ----
const cy = cytoscape({
  container: document.getElementById('cy'),
  elements: [
    ...GRAPH.nodes.map(n => ({ data: { ...n } })),
    ...GRAPH.edges.map(e => ({ data: { ...e, id: e.source + '::' + e.target + '::' + e.kind + '::' + (e.label||'') } })),
  ],
  style: [
    { selector: 'node', style: {
        'background-color': ele => COLORS[ele.data('type')] || '#888',
        'label': 'data(label)',
        'color': '#e6e8eb',
        'font-size': 10,
        'text-outline-width': 2,
        'text-outline-color': '#0e1116',
        'text-valign': 'center',
        'text-halign': 'center',
        'width': 22, 'height': 22,
        'border-width': 1,
        'border-color': '#0e1116',
    }},
    { selector: 'node[type="agent"]', style: { 'shape': 'round-rectangle', 'width': 90, 'height': 28 }},
    { selector: 'node[type="tool"]', style: { 'shape': 'round-diamond', 'width': 60, 'height': 30 }},
    { selector: 'node[type="adapter"]', style: { 'shape': 'round-tag', 'width': 90, 'height': 24 }},
    { selector: 'node[type="service"]', style: { 'shape': 'round-hexagon', 'width': 90, 'height': 28 }},
    { selector: 'node[type="schema"]', style: { 'shape': 'round-rectangle', 'width': 70, 'height': 20, 'background-opacity': 0.7 }},
    { selector: 'edge', style: {
        'curve-style': 'bezier',
        'width': 1,
        'line-color': ele => EDGE_COLORS[ele.data('kind')] || '#555',
        'target-arrow-color': ele => EDGE_COLORS[ele.data('kind')] || '#555',
        'target-arrow-shape': 'triangle',
        'arrow-scale': 0.7,
        'opacity': 0.65,
    }},
    { selector: 'edge[kind="data_flow"]', style: { 'line-style': 'dashed', 'label': 'data(label)', 'font-size': 9, 'color': '#8b9bb4', 'text-background-color': '#0e1116', 'text-background-opacity': 0.8, 'text-background-padding': 2 }},
    { selector: 'edge[kind="import"]', style: { 'width': 0.5, 'opacity': 0.35 }},
    { selector: '.faded', style: { 'opacity': 0.08 }},
    { selector: '.highlighted', style: { 'opacity': 1, 'border-width': 2, 'border-color': '#fff' }},
    { selector: 'edge.highlighted', style: { 'width': 2.5, 'opacity': 1 }},
  ],
  layout: { name: 'cose-bilkent', nodeRepulsion: 8000, idealEdgeLength: 90, animate: false, randomize: true, gravity: 0.25 },
  wheelSensitivity: 0.25,
});

function clearHighlight() {
  cy.elements().removeClass('highlighted faded');
}

cy.on('tap', 'node', evt => {
  const n = evt.target;
  const nbhd = n.predecessors().union(n.successors()).union(n);
  cy.elements().addClass('faded').removeClass('highlighted');
  nbhd.removeClass('faded').addClass('highlighted');
  showInfo(n.data());
});
cy.on('tap', evt => { if (evt.target === cy) { clearHighlight(); hideInfo(); } });

function applyFilters() {
  const layers = new Set(Array.from(document.querySelectorAll('.layer-f:checked')).map(x => x.value));
  const kinds = new Set(Array.from(document.querySelectorAll('.kind-f:checked')).map(x => x.value));
  cy.nodes().forEach(n => n.style('display', layers.has(n.data('type')) ? 'element' : 'none'));
  cy.edges().forEach(e => {
    const ok = kinds.has(e.data('kind')) && layers.has(e.source().data('type')) && layers.has(e.target().data('type'));
    e.style('display', ok ? 'element' : 'none');
  });
}
document.querySelectorAll('.layer-f, .kind-f').forEach(el => el.addEventListener('change', applyFilters));

document.getElementById('q').addEventListener('input', e => {
  const q = e.target.value.trim().toLowerCase();
  if (!q) { clearHighlight(); return; }
  cy.elements().addClass('faded').removeClass('highlighted');
  cy.nodes().filter(n => n.data('label').toLowerCase().includes(q)).removeClass('faded').addClass('highlighted');
});

document.getElementById('reset').addEventListener('click', () => {
  clearHighlight();
  cy.fit(cy.nodes(':visible'), 30);
});

function showInfo(d) {
  const el = document.getElementById('info');
  el.style.display = 'block';
  const meta = d.meta || {};
  const metaHtml = Object.entries(meta).filter(([,v]) => v).map(([k,v]) => `<div class="kv">${k}: <b>${escapeHtml(String(v))}</b></div>`).join('');
  el.innerHTML = `<div class="label">${escapeHtml(d.label)}</div>`
    + `<div class="kv">type: <b>${d.type}</b></div>`
    + (d.file ? `<div class="kv">file: <b>${escapeHtml(d.file)}:${d.line}</b></div>` : '')
    + (d.cluster ? `<div class="kv">cluster: <b>${escapeHtml(d.cluster)}</b></div>` : '')
    + metaHtml;
}
function hideInfo() { document.getElementById('info').style.display = 'none'; }
function escapeHtml(s) { return s.replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c])); }

// ---- 3D toggle ----
let fg = null;
const cyDiv = document.getElementById('cy');
const fgDiv = document.getElementById('fg');
document.getElementById('view-toggle').addEventListener('click', e => {
  const is3d = fgDiv.style.display === 'block';
  if (is3d) {
    fgDiv.style.display = 'none';
    cyDiv.style.display = 'block';
    e.target.textContent = '3D view';
    e.target.classList.remove('active');
  } else {
    fgDiv.style.display = 'block';
    cyDiv.style.display = 'none';
    e.target.textContent = '2D view';
    e.target.classList.add('active');
    if (!fg) {
      fg = ForceGraph3D()(fgDiv)
        .graphData({
          nodes: GRAPH.nodes.map(n => ({ ...n })),
          links: GRAPH.edges.map(e => ({ source: e.source, target: e.target, kind: e.kind, label: e.label })),
        })
        .nodeLabel(n => `${n.label} (${n.type})`)
        .nodeColor(n => COLORS[n.type] || '#888')
        .nodeVal(n => ({module:3, agent:8, tool:5, adapter:6, service:7, schema:3})[n.type] || 3)
        .linkColor(l => EDGE_COLORS[l.kind] || '#555')
        .linkOpacity(0.55)
        .linkDirectionalArrowLength(3)
        .linkDirectionalArrowRelPos(1)
        .backgroundColor('#0e1116');
    }
  }
});
</script>
</body>
</html>
"""


def render_html(graph: dict) -> str:
    payload = json.dumps(graph, ensure_ascii=False)
    return _HTML_TEMPLATE.replace("__GRAPH_JSON__", payload)


# ---------- CLI ----------


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--manual-config", type=Path, default=DEFAULT_MANUAL_CONFIG)
    parser.add_argument(
        "--print-graph",
        action="store_true",
        help="Also emit the raw graph JSON next to the HTML output.",
    )
    args = parser.parse_args()

    graph = build_graph(APP_DIR, args.manual_config)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(render_html(graph), encoding="utf-8")

    if args.print_graph:
        json_path = args.output.with_suffix(".json")
        json_path.write_text(json.dumps(graph, indent=2, ensure_ascii=False), encoding="utf-8")
        print(f"graph json: file://{json_path}")

    n_nodes = len(graph["nodes"])
    n_edges = len(graph["edges"])
    by_type: dict[str, int] = {}
    for n in graph["nodes"]:
        by_type[n["type"]] = by_type.get(n["type"], 0) + 1
    breakdown = ", ".join(f"{k}={v}" for k, v in sorted(by_type.items()))
    print(f"nodes: {n_nodes} ({breakdown})")
    print(f"edges: {n_edges}")
    print(f"file://{args.output}")


if __name__ == "__main__":
    main()
