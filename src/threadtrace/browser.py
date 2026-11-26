"""Read-only loopback browser companion for stored ThreadTrace results."""

from __future__ import annotations

import json
import socket
import time
from collections import defaultdict
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlsplit

from .capabilities import capability_report
from .engine import ThreadError
from .evidence import record
from .query import QueryService

GRAPH_FAMILIES = {
    "dependency": "Dependency and import graph",
    "call": "Function call graph",
    "class": "Class and module graph",
    "ast": "AST structure graph",
    "dataflow": "Static data-flow graph",
    "control-flow": "Control-flow graph",
    "execution-flow": "Evidence-supported execution flow",
}
GRAPH_LIMIT = 100
GRAPH_MAX_LIMIT = 500
SOURCE_CONTEXT = 30
_MAX_SOURCE_BYTES = 2_000_000


_PAGE = """<!doctype html>
<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>ThreadTrace</title><style>
:root{color-scheme:dark light;font-family:system-ui,sans-serif}*{box-sizing:border-box}body{margin:0;height:100vh;display:flex;flex-direction:column;line-height:1.4}
header{padding:.7rem 1rem;border-bottom:1px solid #8886}header h1{display:inline;font-size:1.1rem;margin:0 .7rem 0 0}.muted{opacity:.72}
.controls{display:flex;flex-wrap:wrap;gap:.45rem;align-items:center;margin-top:.5rem}.controls select,.controls input,.controls button{font:inherit;padding:.28rem .4rem}
main{flex:1;display:grid;grid-template-columns:minmax(0,1fr) 21rem;min-height:0}@media(max-width:54rem){main{grid-template-columns:1fr}#details{display:none}}
#viewport{position:relative;overflow:hidden;background:radial-gradient(circle at 1px 1px,#8883 1px,transparent 0);background-size:18px 18px}
#graph{width:100%;height:100%;cursor:grab;touch-action:none}#graph.dragging{cursor:grabbing}
#hint{position:absolute;left:.8rem;bottom:.6rem;font-size:.8rem;opacity:.7;pointer-events:none}
#details{border-left:1px solid #8886;padding:.8rem;overflow:auto;font-size:.88rem}#details h2{font-size:1rem;margin:.2rem 0 .5rem}
#details pre{white-space:pre-wrap;word-break:break-word;background:#8881;padding:.5rem;border-radius:.3rem;max-height:20rem;overflow:auto}
button{cursor:pointer}.result{display:block;width:100%;text-align:left;margin:.2rem 0;padding:.25rem;border:1px solid #8886;border-radius:.25rem;background:transparent;color:inherit}
.row{display:flex;justify-content:space-between;gap:.5rem;border-bottom:1px solid #8884;padding:.2rem 0}.row span:last-child{text-align:right}
.legend span{display:inline-block;margin-right:.5rem}.swatch{display:inline-block;width:.7rem;height:.7rem;border-radius:50%;margin-right:.2rem;vertical-align:middle}
</style></head><body>
<header><h1>ThreadTrace</h1><span id="status" class="muted">Loading…</span>
<div class="controls">
<select id="family" aria-label="Graph family"></select>
<input id="target" list="targets" placeholder="Module, symbol, or entry point" size="30" aria-label="Graph target">
<datalist id="targets"></datalist>
<select id="direction" aria-label="Dependency direction"><option value="dependencies">dependencies</option><option value="dependants">dependants</option></select>
<input id="depth" type="number" min="1" max="8" value="1" style="width:4rem" aria-label="Hops">
<input id="q" placeholder="Filter labels" aria-label="Filter labels">
<select id="kind" aria-label="Relationship type"><option value="">all relationships</option></select>
<select id="evidence" aria-label="Evidence class"><option value="">all evidence</option><option>statically resolved</option><option>heuristically predicted</option><option>observed at runtime</option><option>unknown</option></select>
<button id="load">Load graph</button><button id="more" hidden>Load more</button><a id="export" href="#" download="threadtrace.mmd">Export Mermaid</a>
</div>
<div class="controls"><input id="find" placeholder="Find module or symbol" aria-label="Find module or symbol" size="26"><button id="search">Find</button><span id="results"></span></div>
</header>
<main><section id="viewport"><svg id="graph" role="img" aria-label="Stored analysis graph"><defs><marker id="arrow" viewBox="0 0 10 10" refX="9" refY="5" markerWidth="6" markerHeight="6" orient="auto"><path d="M0 0L10 5L0 10z" fill="#888"/></marker></defs><g id="scene"></g></svg><div id="hint">Read-only local view · drag to pan · wheel to zoom · double-click a node to focus</div></section>
<aside id="details"><h2>Graph</h2><p class="muted">Select a node or relationship to inspect its evidence, source, capability status, and staleness.</p><div class="legend"><span><i class="swatch" style="background:#5aa9e6"></i>statically resolved</span><span><i class="swatch" style="background:#e6b85a"></i>heuristically predicted</span><span><i class="swatch" style="background:#b07de6"></i>observed at runtime</span><span><i class="swatch" style="background:#999"></i>unknown</span></div></aside></main>
<script>
const families=__FAMILIES__;
const colors={"statically resolved":"#5aa9e6","heuristically predicted":"#e6b85a","observed at runtime":"#b07de6","unknown":"#999"};
const state={family:"dependency",target:"",direction:"dependencies",depth:1,q:"",kind:"",evidence:"",offset:0,nodeOffset:0,limit:100,nodes:new Map(),edges:[],next:null,nodeNext:null,graph:null,selected:null};
const $=id=>document.getElementById(id);
for(const [value,label] of Object.entries(families)){const option=document.createElement("option");option.value=value;option.textContent=label;$("family").append(option)}
const params=extra=>{const values={family:state.family,target:state.target,direction:state.direction,depth:String(state.depth),q:state.q,kind:state.kind,evidence:state.evidence,offset:String(state.offset),node_offset:String(state.nodeOffset),limit:String(state.limit)};for(const [key,value] of Object.entries(extra))values[key]=value;return Object.fromEntries(Object.entries(values).filter(([,value])=>value!==""))};
const api=async(path,values)=>{const response=await fetch(path+"?"+new URLSearchParams(values));const body=await response.json().catch(()=>({error:response.statusText}));if(!response.ok)throw new Error(body.error||("HTTP "+response.status));return body};
const row=(label,value)=>{const item=document.createElement("div");item.className="row";const key=document.createElement("span");key.textContent=label;const text=document.createElement("span");text.textContent=value==null||value===""?"—":String(value);item.append(key,text);return item};
const button=(label,action)=>{const item=document.createElement("button");item.type="button";item.textContent=label;item.addEventListener("click",action);return item};
function status(payload){state.graph=payload;const stale=payload.stale||{};const capability=payload.capability||{};$("status").textContent=(stale.stale?"Stale analysis · ":"Current analysis · ")+(capability.feature||"Stored evidence")+": "+(capability.status||"unknown")}
function details(title,items,source){const box=$("details");box.replaceChildren();const heading=document.createElement("h2");heading.textContent=title;box.append(heading);for(const [label,value] of items)box.append(row(label,value));if(source&&source.path)box.append(button("Open source",()=>showSource(source)));if(source&&(source.focus||source.id))box.append(button("Focus this node",()=>{state.target=source.focus||source.id;$("target").value=state.target;load(true)}));const capability=(state.graph&&state.graph.capability)||{};box.append(row("Capability",(capability.status||"unknown")+" — "+(capability.feature||"")));const stale=(state.graph&&state.graph.stale)||{};box.append(row("Staleness",stale.stale?(stale.causes||[]).map(item=>item.cause).join(", "):"current"))}
async function showSource(source){const pre=document.createElement("pre");try{const data=await api("/api/source",{path:source.path,line:String(source.line||1)});pre.textContent=data.lines.map(line=>String(line.number).padStart(5)+"  "+line.text).join("\\n")}catch(error){pre.textContent="Could not open source: "+error.message}$("details").append(pre)}
function layout(){const nodes=[...state.nodes.values()];const columns=5;nodes.forEach((node,index)=>{node.x=(index%columns)*230;node.y=Math.floor(index/columns)*120});return new Map(nodes.map(node=>[node.id,node]))}
function draw(){const scene=$("scene");scene.replaceChildren();const positioned=layout();const namespace="http://www.w3.org/2000/svg";for(const edge of state.edges){const source=positioned.get(edge.source),target=positioned.get(edge.target);if(!source||!target)continue;const line=document.createElementNS(namespace,"line");line.setAttribute("x1",source.x+90);line.setAttribute("y1",source.y+26);line.setAttribute("x2",target.x+90);line.setAttribute("y2",target.y+26);line.setAttribute("stroke","#888");line.setAttribute("stroke-width","1.4");line.setAttribute("marker-end","url(#arrow)");const title=document.createElementNS(namespace,"title");title.textContent=(edge.label||edge.kind)+" · "+edge.evidence_class;line.append(title);line.addEventListener("click",()=>details(edge.kind,[["From",edge.source],["To",edge.target],["Evidence",edge.evidence_class],["Source",edge.source_location?edge.source_location.path+(edge.source_location.line?":"+edge.source_location.line:""):"—"],["Label",edge.label||edge.kind]],{path:edge.source_location&&edge.source_location.path,line:edge.source_location&&edge.source_location.line}));scene.append(line)}for(const node of positioned.values()){const group=document.createElementNS(namespace,"g");group.setAttribute("transform","translate("+node.x+" "+node.y+")");const width=Math.max(110,Math.min(210,node.label.length*7));const rect=document.createElementNS(namespace,"rect");rect.setAttribute("width",width);rect.setAttribute("height","52");rect.setAttribute("rx","8");rect.setAttribute("fill",colors[node.evidence_class]||colors.unknown);rect.setAttribute("fill-opacity","0.22");rect.setAttribute("stroke",colors[node.evidence_class]||colors.unknown);const text=document.createElementNS(namespace,"text");text.setAttribute("x","8");text.setAttribute("y","21");text.setAttribute("fill","currentColor");text.textContent=node.label.length>28?node.label.slice(0,27)+"…":node.label;const kind=document.createElementNS(namespace,"text");kind.setAttribute("x","8");kind.setAttribute("y","39");kind.setAttribute("fill","currentColor");kind.setAttribute("font-size","10");kind.setAttribute("opacity","0.72");kind.textContent=node.kind+" · "+node.evidence_class;group.append(rect,text,kind);group.addEventListener("click",()=>{state.selected=node;details(node.label,[["Kind",node.kind],["Evidence",node.evidence_class],["Source",node.source?node.source.path+(node.source.line?":"+node.source.line:""):"—"],["Details",JSON.stringify(node.details||{})]],{path:node.source&&node.source.path,line:node.source&&node.source.line,focus:node.focus,id:node.id})});group.addEventListener("dblclick",()=>{if(node.focus){state.target=node.focus;$("target").value=node.focus;load(true)}});const title=document.createElementNS(namespace,"title");title.textContent=node.label;group.append(title);scene.append(group)}applyTransform();$("more").hidden=state.next===null&&state.nodeNext===null;$("export").href="/api/graph.mmd?"+new URLSearchParams(params({offset:"",node_offset:"",limit:""}))}
let pan={x:40,y:40},zoom=1;
function applyTransform(){$("scene").setAttribute("transform","translate("+pan.x+" "+pan.y+") scale("+zoom+")")}
$("graph").addEventListener("wheel",event=>{event.preventDefault();const rect=$("graph").getBoundingClientRect();const scale=event.deltaY<0?1.12:0.89;const next=Math.max(0.25,Math.min(3,zoom*scale));const x=event.clientX-rect.left,y=event.clientY-rect.top;pan.x=x-(x-pan.x)*(next/zoom);pan.y=y-(y-pan.y)*(next/zoom);zoom=next;applyTransform()},{passive:false});
let drag=null;
$("graph").addEventListener("pointerdown",event=>{drag={x:event.clientX,y:event.clientY,pan:{...pan}};$("graph").classList.add("dragging");$("graph").setPointerCapture(event.pointerId)});
$("graph").addEventListener("pointermove",event=>{if(!drag)return;pan={x:drag.pan.x+event.clientX-drag.x,y:drag.pan.y+event.clientY-drag.y};applyTransform()});
$("graph").addEventListener("pointerup",()=>{drag=null;$("graph").classList.remove("dragging")});
async function load(reset){state.family=$("family").value;state.target=$("target").value.trim();state.direction=$("direction").value;state.depth=Math.max(1,Math.min(8,Number($("depth").value)||1));state.q=$("q").value.trim();state.kind=$("kind").value;state.evidence=$("evidence").value;if(reset){state.offset=0;state.nodeOffset=0;state.nodes.clear();state.edges=[];state.next=null;state.nodeNext=null;state.selected=null;draw()}$("status").textContent="Loading…";try{const payload=await api("/api/graph",params({offset:String(state.offset),node_offset:String(state.nodeOffset)}));if(reset){state.nodes.clear();state.edges=[]}payload.nodes.forEach(node=>state.nodes.set(node.id,node));const seen=new Set(state.edges.map(edge=>edge.source+"|"+edge.target+"|"+edge.kind+"|"+edge.label));payload.edges.forEach(edge=>{const key=edge.source+"|"+edge.target+"|"+edge.kind+"|"+edge.label;if(!seen.has(key)){seen.add(key);state.edges.push(edge)}});state.next=payload.page.next_offset;state.nodeNext=payload.page.next_node_offset;state.offset=payload.page.offset;state.nodeOffset=payload.page.node_offset;status(payload);const kinds=new Set(payload.kinds||[]);const select=$("kind");const current=select.value;select.replaceChildren(new Option("all relationships",""));for(const kind of [...kinds].sort())select.append(new Option(kind,kind));select.value=current;draw();$("hint").textContent=payload.page.edge_total+" relationships · "+payload.page.node_total+" nodes · showing "+state.edges.length+" loaded"+(payload.page.truncated?" · bounded by stored analysis":"")}catch(error){$("status").textContent="Could not load graph: "+error.message;details("Graph error",[["Message",error.message],["Family",families[state.family]||state.family]])}}
async function find(){const term=$("find").value.trim();if(!term)return;const box=$("results");box.replaceChildren();try{const payload=await api("/api/search",{q:term});for(const result of payload.results){box.append(button(result.kind+" · "+result.label,()=>{state.family=result.family;state.target=result.target;state.offset=0;$("family").value=result.family;$("target").value=result.target;load(true)}))}if(!payload.results.length)box.textContent="No stored match."}catch(error){box.textContent="Search failed: "+error.message}}
$("load").addEventListener("click",()=>load(true));$("more").addEventListener("click",()=>{if(state.next!==null){state.offset=state.next}else if(state.nodeNext!==null){state.nodeOffset=state.nodeNext}load(false)});$("search").addEventListener("click",find);$("find").addEventListener("keydown",event=>{if(event.key==="Enter")find()});$("family").addEventListener("change",()=>{state.family=$("family").value;$("direction").hidden=state.family!=="dependency";$("hint").textContent="Press Load graph to render "+families[state.family]+"."});$("q").addEventListener("keydown",event=>{if(event.key==="Enter")load(true)});$("kind").addEventListener("change",()=>load(true));$("evidence").addEventListener("change",()=>load(true));$("direction").addEventListener("change",()=>load(true));$("depth").addEventListener("change",()=>load(true));
api("/api/overview",{}).then(overview=>{const project=overview.project||{};document.title=(project.name||"ThreadTrace")+" · ThreadTrace";$("status").textContent=(project.name||"ThreadTrace")+" · "+(overview.purpose||"stored analysis")+(overview.stale?" · stale":"")}).catch(()=>{});
load(true);
</script></body></html>"""


def is_loopback_host(host: str) -> bool:
    """Return whether a command-line address is an allowed browser binding."""
    return host in {"127.0.0.1", "::1"}


def _first(params: dict[str, list[str]], name: str, default: str = "") -> str:
    return params.get(name, [default])[0]


def _bounded_int(value: str, default: int, low: int, high: int) -> int:
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(low, min(number, high))


def _source_location(row: dict[str, Any], line_key: str = "line_start") -> dict[str, Any] | None:
    path = row.get("relative_path") or row.get("path")
    line = row.get("line_number") or row.get(line_key)
    if path and line:
        return {"path": path, "line": line}
    return {"path": path} if path else None


def _node(identifier: Any, label: Any, kind: Any, evidence_class: str = "unknown",
          source: dict[str, Any] | None = None, focus: Any = None,
          details: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "id": str(identifier), "label": str(label)[:300], "kind": str(kind),
        "evidence_class": evidence_class, "source": source,
        "focus": str(focus) if focus is not None else None,
        "details": details or {},
    }


def _edge(source: Any, target: Any, kind: Any, evidence_class: str = "unknown",
          label: str = "", details: dict[str, Any] | None = None,
          source_location: dict[str, Any] | None = None) -> dict[str, Any]:
    return {
        "source": str(source), "target": str(target), "kind": str(kind),
        "label": label or str(kind), "evidence_class": evidence_class,
        "source_location": source_location, "details": details or {},
    }


def _dedupe(nodes: list[dict[str, Any]], edges: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    by_id: dict[str, dict[str, Any]] = {}
    for node in nodes:
        by_id.setdefault(node["id"], node)
    valid = set(by_id)
    found: dict[tuple[str, str, str, str], dict[str, Any]] = {}
    for edge in edges:
        if edge["source"] in valid and edge["target"] in valid:
            found.setdefault((edge["source"], edge["target"], edge["kind"], edge["label"]), edge)
    locations = {node_id: node.get("source") for node_id, node in by_id.items()}
    for edge in found.values():
        edge["source_location"] = edge.get("source_location") or locations.get(edge["source"])
    return list(by_id.values()), list(found.values())


def _matches(node: dict[str, Any], target: str) -> bool:
    values = {node["id"], node["label"], node.get("focus") or ""}
    return target in values or any(value.endswith("." + target) for value in values if value)


def _focus(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], target: str,
           depth: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    starts = {node["id"] for node in nodes if _matches(node, target)}
    if not starts:
        raise ThreadError(f"No stored graph node matches '{target}'.")
    adjacency: dict[str, set[str]] = defaultdict(set)
    for edge in edges:
        adjacency[edge["source"]].add(edge["target"])
        adjacency[edge["target"]].add(edge["source"])
    seen = set(starts)
    frontier = set(starts)
    for _ in range(depth):
        frontier = {neighbour for node_id in frontier for neighbour in adjacency[node_id] if neighbour not in seen}
        seen |= frontier
        if not frontier:
            break
    return ([node for node in nodes if node["id"] in seen],
            [edge for edge in edges if edge["source"] in seen and edge["target"] in seen])


def _module_nodes(queries: QueryService, project: str) -> tuple[list[dict[str, Any]], dict[str, str]]:
    nodes: list[dict[str, Any]] = []
    paths: dict[str, str] = {}
    for module in queries.modules(project):
        name = module["module_name"]
        paths[name] = module["relative_path"]
        nodes.append(_node(
            name, name, "module", "statically resolved", {"path": module["relative_path"]}, focus=name,
            details={"parse_status": module.get("parse_status"), "dependencies": module.get("dependencies"),
                     "dependants": module.get("dependants")},
        ))
    return nodes, paths


def _dependency_model(queries: QueryService, project: str, target: str, direction: str,
                      depth: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, list[str]]:
    modules, paths = _module_nodes(queries, project)
    if target:
        data = queries.dependencies(project, target, reverse=direction in {"dependants", "incoming"}, depth=depth)
        names = {target}
        edges: list[dict[str, Any]] = []
        for item in data["edges"]:
            names.update({item["from"], item["to"]})
            edges.append(_edge(item["from"], item["to"], "import", item.get("evidence_class", "statically resolved"),
                               f"import depth {item.get('depth', 1)}"))
        nodes = [node for node in modules if node["id"] in names]
        return nodes, edges, False, ["Only resolved static imports are followed; dynamic and conditional imports may be missing."]
    data = queries.file_graph(project)
    nodes = [node for node in modules if node["id"] in {item["id"] for item in data["nodes"]}]
    edges = [
        _edge(item["source"], item["target"], item["kind"], item.get("evidence_class", "unknown"),
              ", ".join(item.get("labels", [])[:2]) or item["kind"])
        for item in data["edges"]
    ]
    return nodes, edges, False, ["File-level relationships group stored static evidence; unresolved imports are omitted."]


def _code_model(queries: QueryService, project: str, family: str, target: str,
                depth: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, list[str]]:
    code = queries.code_graph(project)
    modules, _ = _module_nodes(queries, project)
    module_names = {node["id"] for node in modules}
    nodes: list[dict[str, Any]] = []
    for item in code["nodes"]:
        kind = item.get("kind")
        if family == "class" and kind not in {"class", "method", "async_method"}:
            continue
        nodes.append(_node(
            item["id"], item.get("qualified_name") or item.get("name"), kind,
            item.get("evidence_class", "statically resolved"),
            _source_location(item), focus=item.get("qualified_name"),
            details={"module": item.get("module_name"), "signature": item.get("signature"),
                     "purpose": item.get("purpose"), "complexity": item.get("complexity")},
        ))
    nodes.extend(modules)
    kinds = {"call", "contains", "inherits"} if family in {"class", "ast"} else set()
    edges: list[dict[str, Any]] = []
    for item in code["edges"]:
        kind = item["kind"]
        if family in {"class", "ast"}:
            if kind not in {"contains", "inherits"}:
                continue
            if family == "class" and not any(node["id"] in {item["source"], item["target"]}
                                             for node in nodes if node["kind"] in {"class", "method", "async_method"}):
                continue
        elif "call" not in kind.lower():
            continue
        edges.append(_edge(item["source"], item["target"], kind, item.get("evidence_class", "unknown"),
                           ", ".join(item.get("labels", [])[:2]) or kind))
    for item in code["nodes"]:
        parent = item.get("parent_qualified_name")
        if family in {"class", "ast"} and parent in module_names and parent != item.get("qualified_name"):
            edges.append(_edge(parent, item["id"], "defines", "statically resolved", "module definition"))
    if target:
        nodes, edges = _focus(nodes, edges, target, depth)
    limitations = ["Only stored static relationships are shown; dynamic dispatch and indirect registration remain boundaries."]
    return nodes, edges, False, limitations


def _dataflow_model(queries: QueryService, project: str,
                    target: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, list[str]]:
    data = queries.dataflow(project, target)
    symbols = queries.symbol(project, target)
    if len(symbols) != 1:
        raise ThreadError("Data-flow graph requires one exact qualified symbol name.")
    symbol = symbols[0]
    location = _source_location(symbol)
    nodes = [_node(symbol["symbol_id"], symbol["qualified_name"], symbol["kind"], "statically resolved",
                   location, focus=symbol["qualified_name"])]
    edges: list[dict[str, Any]] = []
    for item in data["edges"]:
        source, target_name = item.get("source_name"), item.get("target_name")
        if not source or not target_name:
            continue
        for name in (source, target_name):
            nodes.append(_node(f"value:{name}", name, "value",
                               item.get("evidence_class", "heuristically predicted"), location))
        edges.append(_edge(f"value:{source}", f"value:{target_name}", item.get("kind", "data"),
                           item.get("evidence_class", "heuristically predicted"),
                           f"{source} → {target_name}"))
    for resource in data.get("direct_resources", []):
        identifier = f"resource:{resource.get('module_name')}.{resource['name']}"
        nodes.append(_node(identifier, resource["name"], resource["kind"], resource.get("evidence_class", "unknown"),
                           _source_location(resource), details={"operation": resource.get("operation")}))
        edges.append(_edge(symbol["symbol_id"], identifier, "uses", resource.get("evidence_class", "unknown"),
                           resource.get("operation") or "resource use"))
    return nodes, edges, bool(data.get("truncated")), [data.get("limitation", "Only resolved data edges are shown.")]


def _control_model(queries: QueryService, project: str,
                   target: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, list[str]]:
    data = queries.control_flow(project, target)
    symbols = queries.symbol(project, target)
    if len(symbols) != 1:
        raise ThreadError("Control-flow graph requires one exact qualified symbol name.")
    symbol = symbols[0]
    location = _source_location(symbol)
    nodes = [_node(symbol["symbol_id"], symbol["qualified_name"], symbol["kind"], "statically resolved",
                   location, focus=symbol["qualified_name"])]
    edges: list[dict[str, Any]] = []
    for item in data["nodes"]:
        nodes.append(_node(f"cf:{item['id']}", f"{item.get('label', item['kind'])} · line {item.get('line')}",
                           item["kind"], item.get("evidence_class", "statically resolved"),
                           {"path": location["path"], "line": item.get("line")} if location else None))
    for item in data["edges"]:
        edges.append(_edge(f"cf:{item['source']}", f"cf:{item['target']}", item["kind"], "statically resolved", item["kind"]))
    start = next((node["id"] for node in nodes if node["kind"] == "start"), None)
    if start:
        edges.append(_edge(symbol["symbol_id"], start, "contains", "statically resolved", "function entry"))
    return nodes, edges, bool(data.get("truncated")), [data.get("limitation", "Syntax-level control flow is not a runtime path.")]


def _execution_model(queries: QueryService, project: str, target: str,
                     depth: int) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool, list[str]]:
    data = queries.path(project, target, depth=depth)
    nodes: list[dict[str, Any]] = []
    edges: list[dict[str, Any]] = []
    known: set[str] = set()
    for step in data.get("steps", []):
        source, target_id = step.get("source_symbol_id"), step.get("target_symbol_id")
        if not source or not target_id:
            continue
        for identifier, label in ((source, step.get("source_name")), (target_id, step.get("target_name"))):
            if identifier not in known:
                known.add(identifier)
                nodes.append(_node(identifier, label or identifier, "symbol",
                                   step.get("evidence_class", "heuristically predicted"),
                                   _source_location(step), focus=label))
        edges.append(_edge(source, target_id, step.get("kind", "relationship"),
                           step.get("evidence_class", "heuristically predicted"),
                           f"{step.get('source_name') or source} → {step.get('target_name') or target_id}"))
    handler = data.get("handler")
    if handler and handler not in known:
        nodes.append(_node(handler, data.get("entry_point") or handler, "handler",
                           "heuristically predicted", None, focus=data.get("entry_point")))
        known.add(handler)
    if not nodes:
        nodes.append(_node(f"entry:{data.get('entry_point', target)}", data.get("entry_point", target),
                           "entry", "heuristically predicted", None, focus=data.get("entry_point")))
    for resource in data.get("resources", []):
        identifier = f"resource:{resource.get('module_name')}.{resource['name']}"
        nodes.append(_node(identifier, resource["name"], resource["kind"], resource.get("evidence_class", "unknown"),
                           _source_location(resource), details={"operation": resource.get("operation")}))
        source = resource.get("symbol_id") if resource.get("symbol_id") in known else handler
        if source:
            edges.append(_edge(source, identifier, "uses", resource.get("evidence_class", "unknown"),
                               resource.get("operation") or "resource use"))
    return nodes, edges, len(data.get("steps", [])) >= 500, [
        "Execution flow follows stored direct relationships from the selected entry point and is depth-bounded.",
    ]


def _graph_model(queries: QueryService, project: str, family: str, target: str = "",
                 direction: str = "dependencies", depth: int = 1) -> dict[str, Any]:
    if family not in GRAPH_FAMILIES:
        raise ThreadError(f"Unknown graph family '{family}'.")
    depth = max(1, min(int(depth), 8))
    if family == "dependency":
        nodes, edges, truncated, limitations = _dependency_model(queries, project, target, direction, depth)
    elif family in {"call", "class", "ast"}:
        nodes, edges, truncated, limitations = _code_model(queries, project, family, target, depth)
    elif family == "dataflow":
        if not target:
            raise ThreadError("The data-flow graph needs an exact symbol target.")
        nodes, edges, truncated, limitations = _dataflow_model(queries, project, target)
    elif family == "control-flow":
        if not target:
            raise ThreadError("The control-flow graph needs an exact symbol target.")
        nodes, edges, truncated, limitations = _control_model(queries, project, target)
    else:
        if not target:
            raise ThreadError("The execution-flow graph needs an entry point target.")
        nodes, edges, truncated, limitations = _execution_model(queries, project, target, depth)
    nodes, edges = _dedupe(nodes, edges)
    if not nodes:
        raise ThreadError("No stored graph nodes match this view.")
    evidence_class = "heuristically predicted" if family in {"dataflow", "execution-flow"} else "statically resolved"
    return {
        "family": family, "title": GRAPH_FAMILIES[family], "target": target, "depth": depth,
        "nodes": nodes, "edges": edges, "truncated": truncated,
        "evidence_record": record(evidence_class, f"stored {GRAPH_FAMILIES[family].lower()}",
                                  source={"project": project}, limitations=limitations),
    }


def _filter_graph(nodes: list[dict[str, Any]], edges: list[dict[str, Any]], q: str = "",
                  kinds: set[str] | None = None, evidence: set[str] | None = None
                  ) -> tuple[list[dict[str, Any]], list[dict[str, Any]], int]:
    valid = {node["id"] for node in nodes}
    kept = [edge for edge in edges if edge["source"] in valid and edge["target"] in valid]
    invalid = len(edges) - len(kept)
    if kinds:
        kept = [edge for edge in kept if edge["kind"] in kinds]
    if evidence:
        kept = [edge for edge in kept if edge["evidence_class"] in evidence]
    if q:
        term = q.lower()
        matched = {node["id"] for node in nodes if term in node["label"].lower() or term in node["id"].lower()}
        kept = [edge for edge in kept if edge["source"] in matched or edge["target"] in matched
                or term in edge["kind"].lower() or term in edge["label"].lower()]
        matched |= {edge["source"] for edge in kept} | {edge["target"] for edge in kept}
        nodes = [node for node in nodes if node["id"] in matched]
    return nodes, kept, invalid


def _status(queries: QueryService, project: str) -> dict[str, Any]:
    capability = next(
        (item for item in capability_report(project=project)
         if "Static paths, impact, data flow, slices, architecture, and control flow" in item["feature"]),
        {"feature": "Static graph evidence", "status": "unknown", "details": ""},
    )
    return {"stale": queries.staleness(project), "capability": capability}


def graph_payload(queries: QueryService, project: str, *, family: str = "dependency",
                  target: str = "", direction: str = "dependencies", depth: int = 1,
                  offset: int = 0, limit: int = GRAPH_LIMIT, q: str = "",
                  node_offset: int = 0, node_limit: int | None = None,
                  kinds: set[str] | None = None, evidence: set[str] | None = None,
                  status: dict[str, Any] | None = None) -> dict[str, Any]:
    """Return one bounded, filtered page of the selected stored graph."""
    model = _graph_model(queries, project, family, target.strip(), direction, depth)
    nodes, edges, invalid = _filter_graph(model["nodes"], model["edges"], q.strip(), kinds, evidence)
    offset = max(0, offset)
    limit = max(1, min(limit, GRAPH_MAX_LIMIT))
    node_offset = max(0, node_offset)
    node_limit = max(1, min(node_limit if node_limit is not None else limit, GRAPH_MAX_LIMIT))
    page_edges = edges[offset:offset + limit]
    referenced = {edge["source"] for edge in page_edges} | {edge["target"] for edge in page_edges}
    connected = {edge["source"] for edge in edges} | {edge["target"] for edge in edges}
    orphans = [node["id"] for node in nodes if node["id"] not in connected]
    page_orphans = set(orphans[node_offset:node_offset + node_limit])
    page_nodes = [node for node in nodes if node["id"] in referenced or node["id"] in page_orphans]
    next_offset = offset + limit if offset + limit < len(edges) else None
    next_node_offset = node_offset + node_limit if node_offset + node_limit < len(orphans) else None
    page = {
        "offset": offset, "limit": limit, "node_offset": node_offset, "node_limit": node_limit,
        "edge_total": len(edges), "node_total": len(nodes),
        "returned_edges": len(page_edges), "returned_nodes": len(page_nodes),
        "next_offset": next_offset, "next_node_offset": next_node_offset, "invalid_edges": invalid,
        "truncated": bool(model["truncated"]) or next_offset is not None or next_node_offset is not None,
    }
    status = status or _status(queries, project)
    return {
        "family": family, "title": model["title"], "target": model["target"], "depth": model["depth"],
        "nodes": page_nodes, "edges": page_edges, "page": page,
        "kinds": sorted({edge["kind"] for edge in edges}),
        "evidence_record": model["evidence_record"],
        "capability": status["capability"],
        "stale": status["stale"],
        "truncated": page["truncated"],
        "families": GRAPH_FAMILIES,
    }


def graph_mermaid(value: dict[str, Any]) -> str:
    """Render the same graph model as Mermaid, dropping unknown endpoints."""
    from .cli import _mermaid_text

    nodes = value.get("nodes", [])
    valid = {node["id"]: node for node in nodes}
    identifiers = {node_id: f"n{index}" for index, node_id in enumerate(valid, 1)}
    lines = ["flowchart LR"]
    for node_id, node in valid.items():
        lines.append(f'  {identifiers[node_id]}["{_mermaid_text(node.get("label") or node_id)}"]')
    dropped = 0
    for edge in value.get("edges", []):
        if edge.get("source") not in valid or edge.get("target") not in valid:
            dropped += 1
            continue
        lines.append(
            f'  {identifiers[edge["source"]]} -->|{_mermaid_text(edge.get("label") or edge.get("kind") or "relationship")}| '
            f'{identifiers[edge["target"]]}'
        )
    if dropped:
        lines.insert(1, f"%% Dropped {dropped} edge(s) with unknown endpoints.")
    if len(lines) == 1:
        lines.append('  empty["No stored graph nodes match this view."]')
    return "\n".join(lines)


def source_payload(queries: QueryService, project: str, relative_path: str, line: int = 1,
                   context: int = SOURCE_CONTEXT) -> dict[str, Any]:
    """Return a bounded source window only from inside the registered project."""
    root = Path(queries.engine.get_project(project)["root_path"]).resolve()
    candidate = (root / relative_path).resolve()
    if not candidate.is_relative_to(root) or not candidate.is_file():
        raise ThreadError("Source navigation is limited to files inside the registered project.")
    if candidate.stat().st_size > _MAX_SOURCE_BYTES:
        raise ThreadError("Source navigation is limited to files smaller than 2 MB.")
    try:
        lines = candidate.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError as exc:
        raise ThreadError("ThreadTrace could not read this source file.") from exc
    line = max(1, min(int(line), max(len(lines), 1)))
    context = max(1, min(int(context), 80))
    start, end = max(1, line - context), min(len(lines), line + context)
    return {
        "path": relative_path, "line": line, "start": start, "end": end,
        "lines": [{"number": number, "text": lines[number - 1][:500]} for number in range(start, end + 1)],
        "truncated": start > 1 or end < len(lines),
    }


def search_payload(queries: QueryService, project: str, term: str, limit: int = 20) -> dict[str, Any]:
    """Find stored graph targets without reading or executing target code."""
    term = term.strip()
    limit = max(1, min(limit, 50))
    if not term:
        return {"query": term, "results": []}
    found: list[dict[str, Any]] = []
    seen: set[tuple[str, str]] = set()

    def add(kind: str, label: str, target: str, family: str, source: dict[str, Any] | None = None) -> None:
        key = (kind, target)
        if key not in seen and len(found) < limit:
            seen.add(key)
            found.append({"kind": kind, "label": label, "target": target, "family": family, "source": source})

    for symbol in queries.search(project, term):
        add(symbol["kind"], symbol["qualified_name"], symbol["qualified_name"],
            "call", {"path": symbol["relative_path"], "line": symbol["line_start"]})
    lowered = term.lower()
    for module in queries.modules(project):
        if lowered in module["module_name"].lower() or lowered in module["relative_path"].lower():
            add("module", module["module_name"], module["module_name"], "dependency",
                {"path": module["relative_path"]})
    for entry in queries.entrypoints(project):
        if lowered in entry["name"].lower():
            add(entry["kind"], entry["name"], entry["name"], "execution-flow", _source_location(entry))
    return {"query": term, "results": found}


def _payload(queries: QueryService, project: str, name: str) -> Any:
    if name == "overview":
        overview = queries.overview(project)
        value = {
            "purpose": overview["purpose"],
            "starting_points": [item for item in queries.entrypoints(project) if item["kind"] != "test"],
            "main_parts": overview["components"],
            "next_step": None,
            "stale": queries.stale(project),
            "graph_families": GRAPH_FAMILIES,
        }
        if value["starting_points"]:
            value["next_step"] = value["starting_points"][0]["name"]
        value["project"] = queries.engine.get_project(project)
        return value
    if name == "architecture":
        return queries.architecture(project)
    if name == "capabilities":
        return capability_report(project=project)
    raise KeyError(name)


class BrowserServer(HTTPServer):
    """A single-threaded server restricted to a chosen loopback address."""

    allow_reuse_address = True

    def __init__(self, host: str, port: int, queries: QueryService, project: str) -> None:
        if not is_loopback_host(host):
            raise ValueError("Browser views may bind only to 127.0.0.1 or ::1.")
        if host == "::1":
            self.address_family = socket.AF_INET6
        self.queries = queries
        self.project = project
        self._status: dict[str, Any] | None = None
        self._status_at = 0.0
        super().__init__((host, port), BrowserRequestHandler)

    @property
    def url(self) -> str:
        host, port = self.server_address[:2]
        return f"http://[{host}]:{port}/" if ":" in host else f"http://{host}:{port}/"

    def accepts_host(self, header: str) -> bool:
        host, port = self.server_address[:2]
        expected = f"[{host}]:{port}" if ":" in host else f"{host}:{port}"
        return header == expected or (port == 80 and header == host)

    def status(self) -> dict[str, Any]:
        now = time.monotonic()
        if self._status is None or now - self._status_at > 5:
            self._status = _status(self.queries, self.project)
            self._status_at = now
        return self._status


class BrowserRequestHandler(BaseHTTPRequestHandler):
    server: BrowserServer

    def do_GET(self) -> None:  # noqa: N802 - stdlib hook name
        if not self._trusted_request():
            self._send_json_error(HTTPStatus.FORBIDDEN, "This local view accepts only same-origin loopback requests.")
            return
        parsed = urlsplit(self.path)
        params = parse_qs(parsed.query, keep_blank_values=True)
        try:
            if parsed.path == "/":
                page = _PAGE.replace("__FAMILIES__", json.dumps(GRAPH_FAMILIES))
                self._send(HTTPStatus.OK, "text/html; charset=utf-8", page.encode())
                return
            if parsed.path == "/api/graph":
                family = _first(params, "family", "dependency")
                target = _first(params, "target")
                direction = _first(params, "direction", "dependencies")
                depth = _bounded_int(_first(params, "depth", "1"), 1, 1, 8)
                offset = _bounded_int(_first(params, "offset", "0"), 0, 0, 1_000_000)
                node_offset = _bounded_int(_first(params, "node_offset", "0"), 0, 0, 1_000_000)
                limit = _bounded_int(_first(params, "limit", str(GRAPH_LIMIT)), GRAPH_LIMIT, 1, GRAPH_MAX_LIMIT)
                kinds = {item for item in _first(params, "kind").split(",") if item}
                evidence = {item for item in _first(params, "evidence").split(",") if item}
                body = graph_payload(
                    self.server.queries, self.server.project, family=family, target=target,
                    direction=direction, depth=depth, offset=offset, limit=limit, node_offset=node_offset,
                    q=_first(params, "q"), kinds=kinds, evidence=evidence,
                    status=self.server.status(),
                )
                self._send_json(HTTPStatus.OK, body)
                return
            if parsed.path == "/api/graph.mmd":
                model = _graph_model(
                    self.server.queries, self.server.project, _first(params, "family", "dependency"),
                    _first(params, "target"), _first(params, "direction", "dependencies"),
                    _bounded_int(_first(params, "depth", "1"), 1, 1, 8),
                )
                nodes, edges, _ = _filter_graph(
                    model["nodes"], model["edges"], _first(params, "q"),
                    {item for item in _first(params, "kind").split(",") if item},
                    {item for item in _first(params, "evidence").split(",") if item},
                )
                self._send(HTTPStatus.OK, "text/plain; charset=utf-8",
                           graph_mermaid({"nodes": nodes, "edges": edges}).encode())
                return
            if parsed.path == "/api/source":
                body = source_payload(
                    self.server.queries, self.server.project, _first(params, "path"),
                    _bounded_int(_first(params, "line", "1"), 1, 1, 10_000_000),
                    _bounded_int(_first(params, "context", str(SOURCE_CONTEXT)), SOURCE_CONTEXT, 1, 80),
                )
                self._send_json(HTTPStatus.OK, body)
                return
            if parsed.path == "/api/search":
                self._send_json(HTTPStatus.OK, search_payload(
                    self.server.queries, self.server.project, _first(params, "q"),
                    _bounded_int(_first(params, "limit", "20"), 20, 1, 50),
                ))
                return
            if parsed.path.startswith("/api/"):
                name = parsed.path.removeprefix("/api/")
                if name not in {"overview", "architecture", "capabilities"}:
                    self._send_json_error(HTTPStatus.NOT_FOUND, "Unknown local analysis endpoint.")
                    return
                self._send_json(HTTPStatus.OK, _payload(self.server.queries, self.server.project, name))
                return
            self.send_error(HTTPStatus.NOT_FOUND)
        except ThreadError as exc:
            self._send_json_error(HTTPStatus.BAD_REQUEST, str(exc)[:200])
        except (KeyError, ValueError) as exc:
            self._send_json_error(HTTPStatus.BAD_REQUEST, str(exc)[:200])
        except Exception:
            self._send_json_error(HTTPStatus.INTERNAL_SERVER_ERROR, "Could not build local analysis response.")

    def do_POST(self) -> None:  # noqa: N802 - stdlib hook name
        self._method_not_allowed()

    def do_PUT(self) -> None:  # noqa: N802 - stdlib hook name
        self._method_not_allowed()

    def do_PATCH(self) -> None:  # noqa: N802 - stdlib hook name
        self._method_not_allowed()

    def do_DELETE(self) -> None:  # noqa: N802 - stdlib hook name
        self._method_not_allowed()

    def log_message(self, format: str, *args: Any) -> None:
        return

    def _trusted_request(self) -> bool:
        host = self.headers.get("Host", "")
        if not self.server.accepts_host(host):
            return False
        origin = self.headers.get("Origin")
        if origin and origin not in {f"http://{host}", f"https://{host}"}:
            return False
        site = self.headers.get("Sec-Fetch-Site")
        return not site or site in {"same-origin", "none"}

    def _send_json(self, status: HTTPStatus, value: Any) -> None:
        self._send(status, "application/json; charset=utf-8", json.dumps(value, default=str).encode())

    def _method_not_allowed(self) -> None:
        body = json.dumps({"error": "This local browser view is read-only.", "status": 405}).encode()
        self._send(HTTPStatus.METHOD_NOT_ALLOWED, "application/json; charset=utf-8", body, allow="GET")

    def _send(self, status: HTTPStatus, content_type: str, body: bytes, allow: str | None = None) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Security-Policy", "default-src 'self'; style-src 'unsafe-inline'; script-src 'unsafe-inline'")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        if allow:
            self.send_header("Allow", allow)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _send_json_error(self, status: HTTPStatus, message: str) -> None:
        self._send_json(status, {"error": message, "status": int(status)})
