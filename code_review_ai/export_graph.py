"""Export the call graph + communities as an interactive dual-level HTML visualization.

Level 1 — Community bubbles: each community is a circle, sized by node count.
           Inter-community edges show call relationships between communities.
Level 2 — Click a bubble to drill into its internal call graph.
"""

import json
import sqlite3
import sys
from collections import defaultdict


def export(db_path: str, out_path: str, max_items: int = 50,
           mode: str = "communities") -> None:
    if mode == "graph":
        _export_graph(db_path, out_path, max_items)
    elif mode == "flow":
        _export_flow(db_path, out_path, max_items)
    else:
        _export_communities(db_path, out_path, max_items)


def _export_graph(db_path: str, out_path: str, max_nodes: int) -> None:
    """Raw function-level call graph."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    rows = conn.execute("""
        SELECT n.qualified_name, n.file_path, n.kind, n.start_line,
               COUNT(e.id) AS degree
        FROM nodes n
        JOIN edges e ON n.qualified_name IN (e.source, e.target)
        WHERE e.resolution = 'resolved' AND n.kind IN ('function','method','class')
        GROUP BY n.qualified_name
        ORDER BY degree DESC
        LIMIT ?
    """, (max_nodes,)).fetchall()

    qnames = {r["qualified_name"] for r in rows}
    edges = conn.execute("""
        SELECT source, target, COUNT(*) AS weight
        FROM edges
        WHERE resolution = 'resolved' AND source IN ({}) AND target IN ({})
        GROUP BY source, target
    """.format(
        ",".join(f"'{q}'" for q in qnames),
        ",".join(f"'{q}'" for q in qnames),
    )).fetchall() if qnames else []

    conn.close()

    nodes_json = [{"id": r["qualified_name"],
                   "label": _short(r["qualified_name"]),
                   "kind": r["kind"], "file": r["file_path"],
                   "line": r["start_line"], "degree": r["degree"]}
                  for r in rows]
    edges_json = [{"source": e["source"], "target": e["target"],
                   "weight": e["weight"]} for e in edges]

    html = _GRAPH_TEMPLATE.replace("__DATA__", json.dumps(
        {"nodes": nodes_json, "edges": edges_json}, ensure_ascii=False))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Exported {len(nodes_json)} nodes, {len(edges_json)} edges → {out_path}")


def _short(qname: str) -> str:
    return qname.split("::")[-1].split(".")[-1]


def _export_communities(db_path: str, out_path: str, max_communities: int) -> None:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    # ── communities with node counts ──
    comms = conn.execute("""
        SELECT c.id, c.label, c.modularity, COUNT(cm.node_id) AS node_count
        FROM communities c
        JOIN community_memberships cm ON cm.community_id = c.id
        GROUP BY c.id
        ORDER BY node_count DESC
        LIMIT ?
    """, (max_communities,)).fetchall()

    if not comms:
        print("No communities found. Rebuild with CRAI_COMMUNITY_DETECTION=true first.")
        return

    comm_ids = {c["id"] for c in comms}
    comm_nodes: dict[int, list[dict]] = defaultdict(list)
    node_to_comm: dict[str, int] = {}

    for c in comms:
        members = conn.execute("""
            SELECT n.qualified_name, n.file_path, n.start_line, n.signature, n.kind
            FROM nodes n
            JOIN community_memberships cm ON cm.node_id = n.id
            WHERE cm.community_id = ?
        """, (c["id"],)).fetchall()
        for m in members:
            comm_nodes[c["id"]].append({
                "qname": m["qualified_name"],
                "file": m["file_path"],
                "line": m["start_line"],
                "sig": m["signature"],
                "kind": m["kind"],
            })
            node_to_comm[m["qualified_name"]] = c["id"]

    # ── inter-community edges ──
    comm_edges: dict[tuple[int, int], int] = defaultdict(int)
    for cid in comm_ids:
        members = {m["qname"] for m in comm_nodes[cid]}
        rows = conn.execute("""
            SELECT source, target FROM edges
            WHERE resolution = 'resolved'
        """).fetchall()
        for e in rows:
            s_c = node_to_comm.get(e["source"])
            t_c = node_to_comm.get(e["target"])
            if s_c and t_c and s_c in comm_ids and t_c in comm_ids and s_c != t_c:
                key = (s_c, t_c) if s_c < t_c else (t_c, s_c)
                comm_edges[key] += 1

    conn.close()

    # ── build data ──
    communities_json = []
    for c in comms:
        communities_json.append({
            "id": c["id"],
            "label": c["label"],
            "nodeCount": c["node_count"],
        })

    edge_json = [{"source": a, "target": b, "weight": w}
                 for (a, b), w in comm_edges.items()
                 if w >= 1]

    nodes_json = {str(c["id"]): comm_nodes[c["id"]] for c in comms}

    data = json.dumps({
        "communities": communities_json,
        "edges": edge_json,
        "nodes": nodes_json,
    }, ensure_ascii=False)

    html = _COMM_TEMPLATE.replace("__DATA__", data)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    n_edges = len(edge_json)
    print(f"Exported {len(communities_json)} communities, {n_edges} inter-community edges → {out_path}")


_COMM_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Community Architecture</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0f0f1a; color:#ddd; font-family:system-ui; overflow:hidden; }
#top { position:fixed; top:0; left:0; right:0; height:48px; background:#1a1a2e;
       display:flex; align-items:center; padding:0 16px; z-index:10;
       border-bottom:1px solid #333; gap:12px; }
#top h1 { font-size:16px; font-weight:600; }
#top button { padding:6px 14px; border-radius:6px; border:1px solid #555;
              background:#252540; color:#ccc; cursor:pointer; font-size:13px; }
#top button:hover { background:#353560; }
#main { position:absolute; top:48px; left:0; right:0; bottom:0; }
svg { width:100%; height:100%; }
.bubble circle { stroke-width:2px; cursor:pointer; transition:opacity .15s; }
.bubble text { fill:#e0e0e0; font-size:11px; text-anchor:middle; pointer-events:none; }
.comm-link { stroke:#ff6b6b; stroke-width:1.5px; fill:none; opacity:0.7;
             filter:drop-shadow(0 0 3px #ff6b6b44); }
#detail { position:fixed; top:48px; right:0; bottom:0; width:420px; background:#1a1a2ebb;
          backdrop-filter:blur(12px); border-left:1px solid #333; overflow-y:auto;
          padding:16px; transform:translateX(100%); transition:transform .25s; z-index:5; }
#detail.open { transform:translateX(0); }
#detail h3 { font-size:15px; margin-bottom:8px; color:#fff; }
#detail .close { position:absolute; top:12px; right:12px; background:none; border:none;
                 color:#aaa; font-size:20px; cursor:pointer; }
#detail .member { padding:8px 0; border-bottom:1px solid #2a2a3e; font-size:12px; }
#detail .member .qname { color:#7eb8ff; font-weight:500; }
#detail .member .meta { color:#888; margin-top:2px; }
#tooltip { position:fixed; padding:10px 14px; background:#1a1a2e; color:#e0e0e0;
           border-radius:8px; font-size:12px; pointer-events:none; opacity:0;
           border:1px solid #444; z-index:20; max-width:300px; }
.legend { position:fixed; bottom:16px; left:16px; font-size:11px; color:#666; z-index:2; }
</style></head><body>
<div id="top">
  <h1>Community Architecture</h1>
  <button onclick="backToOverview()">← Overview</button>
  <span style="color:#666;font-size:12px;margin-left:auto">click bubble for detail</span>
</div>
<div id="main"><svg id="svg"></svg></div>
<div id="detail"><button class="close" onclick="closeDetail()">×</button><div id="detailContent"></div></div>
<div id="tooltip"></div>
<div class="legend">bubble size = node count · line thickness = call frequency · click = drill in</div>

<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const data = __DATA__;
const svg = d3.select("#svg"), g = svg.append("g");
const tip = d3.select("#tooltip");
const detail = d3.select("#detail"), detailContent = d3.select("#detailContent");

const W = window.innerWidth, H = window.innerHeight - 48;

let zoom = d3.zoom().scaleExtent([0.2, 3]).on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);

// ── Layout ──
const radius = d3.scaleSqrt().domain([1, d3.max(data.communities, d => d.nodeCount)])
    .range([18, 80]);

const sim = d3.forceSimulation(data.communities)
  .force("x", d3.forceX(W/2).strength(0.03))
  .force("y", d3.forceY(H/2).strength(0.03))
  .force("collide", d3.forceCollide().radius(d => radius(d.nodeCount) + 12))
  .force("link", d3.forceLink(data.edges).id(d => d.id).distance(120).strength(d => 0.2 * Math.log(d.weight + 1)))
  .force("charge", d3.forceManyBody().strength(-400));

const link = g.selectAll("line").data(data.edges).join("line")
  .attr("class", "comm-link")
  .attr("stroke-width", d => Math.min(8, Math.log(d.weight + 1) * 2.5))

const edgeLabel = g.selectAll(".edge-label").data(data.edges).join("text")
  .attr("class", "edge-label")
  .text(d => d.weight)
  .attr("font-size", 10).attr("fill", "#ff9999").attr("text-anchor", "middle")
  .attr("dy", -6)

const node = g.selectAll("g").data(data.communities).join("g")
  .attr("class", "bubble")
  .call(d3.drag().on("start", (e,d) => { if(!e.active) sim.alphaTarget(0.3).restart(); d.fx=d.x; d.fy=d.y; })
       .on("drag", (e,d) => { d.fx=e.x; d.fy=e.y; })
       .on("end", (e,d) => { if(!e.active) sim.alphaTarget(0); d.fx=null; d.fy=null; }));

const color = d3.scaleOrdinal(d3.schemeTableau10);

node.append("circle")
  .attr("r", d => radius(d.nodeCount))
  .attr("fill", (d,i) => color(i))
  .attr("stroke", (d,i) => d3.color(color(i)).darker(0.3))
  .on("click", (e,d) => showDetail(d))
  .on("mouseover", (e,d) => {
    tip.style("opacity",1).html(`<b>${d.label}</b><br>${d.nodeCount} nodes`);
  }).on("mouseout", () => tip.style("opacity",0));

node.append("text")
  .text(d => d.label)
  .attr("dy", d => -radius(d.nodeCount) - 6);

sim.on("tick", () => {
  link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
  edgeLabel.attr("x", d => (d.source.x + d.target.x) / 2)
           .attr("y", d => (d.source.y + d.target.y) / 2);
  node.attr("transform", d => `translate(${d.x},${d.y})`);
});

// ── Detail panel ──
function showDetail(d) {
  const members = data.nodes[d.id] || [];
  let html = `<h3>${d.label}</h3><p style="color:#888;margin-bottom:16px">${d.nodeCount} nodes</p>`;
  members.forEach(m => {
    const name = m.qname.split("::").pop().split(".").pop();
    html += `<div class="member">
      <div class="qname">${m.qname}</div>
      <div class="meta">${m.kind} · ${m.file}:${m.line}</div>
    </div>`;
  });
  detailContent.html(html);
  detail.classed("open", true);
}
function closeDetail() { detail.classed("open", false); }
function backToOverview() { closeDetail(); }
</script></body></html>"""


def _export_flow(db_path: str, out_path: str, max_flows: int) -> None:
    """Flow view: BFS call chains from entry points."""
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row

    flows = conn.execute("""
        SELECT f.id, f.name, f.entry_point_id, f.node_count, f.path_json,
               n.qualified_name AS entry_qname, n.file_path AS entry_file
        FROM flows f
        JOIN nodes n ON n.id = f.entry_point_id
        ORDER BY f.node_count DESC
        LIMIT ?
    """, (max_flows,)).fetchall()

    # Build node lookup
    node_rows = conn.execute(
        "SELECT id, qualified_name, file_path, start_line, kind FROM nodes"
    ).fetchall()
    node_map = {r["id"]: dict(r) for r in node_rows}

    flows_json = []
    nodes_in_flows: set[int] = set()
    for f in flows:
        path = json.loads(f["path_json"])
        nodes_in_flows.update(path)
        flows_json.append({
            "id": f["id"],
            "name": f["name"],
            "entryQname": f["entry_qname"],
            "entryFile": f["entry_file"],
            "nodeCount": f["node_count"],
            "path": path,
        })

    # Only include nodes that appear in the selected flows
    nodes_json = [{"id": node_map[nid]["qualified_name"],
                   "nid": nid,
                   "label": _short(node_map[nid]["qualified_name"]),
                   "kind": node_map[nid]["kind"],
                   "file": node_map[nid]["file_path"],
                   "line": node_map[nid]["start_line"]}
                  for nid in nodes_in_flows if nid in node_map]

    conn.close()

    html = _FLOW_TEMPLATE.replace("__DATA__", json.dumps({
        "flows": flows_json, "nodes": nodes_json,
    }, ensure_ascii=False))
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"Exported {len(flows_json)} flows, {len(nodes_json)} nodes → {out_path}")


_FLOW_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Flow View</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0f0f1a; color:#ddd; font-family:system-ui; display:flex; }
#sidebar { width:300px; min-width:200px; height:100vh; overflow-y:auto;
           background:#1a1a2e; padding:12px; flex-shrink:0; position:relative; }
#resizer { width:5px; height:100vh; background:transparent; cursor:col-resize;
           position:fixed; top:0; z-index:10; transition:background .2s; }
#resizer:hover, #resizer.active { background:#4ecdc488; }
#sidebar h2 { font-size:14px; margin-bottom:8px; color:#fff; }
#sidebar .flow { padding:8px 10px; margin-bottom:4px; border-radius:6px;
                  cursor:pointer; font-size:12px; transition:background .15s; }
#sidebar .flow:hover { background:#2a2a4e; }
#sidebar .flow.active { background:#353560; border-left:3px solid #4ecdc4; }
#sidebar .flow .name { color:#4ecdc4; font-weight:600; }
#sidebar .flow .meta { color:#888; font-size:11px; }
#main { flex:1; overflow:auto; padding:24px; height:100vh; }
body { overflow:hidden; }
svg { min-width:100%; }
.node rect { fill:#1a3a3a; stroke:#4ecdc488; rx:6; }
.node text { fill:#ddd; font-size:12px; }
.edge path { stroke:#4ecdc488; stroke-width:2px; fill:none; }
.edge marker { fill:#4ecdc4; }
.entry rect { fill:#1a3a5a; stroke:#7eb8ff88; }
#tooltip { position:fixed; padding:8px 12px; background:#1a1a2e; color:#e0e0e0;
           border-radius:6px; font-size:12px; pointer-events:none; opacity:0;
           border:1px solid #444; max-width:360px; z-index:20; }
</style></head><body>
<div id="sidebar"><h2>Flows</h2><div id="flowList"></div></div>
<div id="resizer"></div>
<div id="main"><svg id="svg"></svg></div>
<div id="tooltip"></div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const data = __DATA__;
const tip = d3.select("#tooltip");
const main = d3.select("#main");

// ── Resizable sidebar ──
const sidebar = d3.select("#sidebar"), resizer = d3.select("#resizer");
let resizing = false, startX, startW;
resizer.style("left", sidebar.node().offsetWidth + "px");
resizer.on("mousedown", function(event) {
  resizing = true; resizer.classed("active", true);
  startX = event.clientX; startW = sidebar.node().offsetWidth;
  event.preventDefault();
});
d3.select(window).on("mousemove", function(event) {
  if (!resizing) return;
  const w = Math.max(180, Math.min(700, startW + (event.clientX - startX)));
  sidebar.style("width", w + "px");
  resizer.style("left", w + "px");
}).on("mouseup", function() { resizing = false; resizer.classed("active", false); });

// nid -> node data
const nidMap = {};
data.nodes.forEach(n => nidMap[n.nid] = n);

// Render flow list
const flowList = d3.select("#flowList");
data.flows.forEach(f => {
  flowList.append("div").attr("class","flow")
    .html("<div class='name'>"+(f.name||f.entryQname.split("::").pop())
         +" <span style='color:#f0a040;font-weight:700;font-size:13px'>"+f.nodeCount+"</span></div>"
         +"<div class='meta'>"+f.entryFile+"</div>")
    .on("click", () => renderFlow(f));
});

const SVG_W = 1100, H_STEP = 66, RECT_H = 44;
const arrow = d3.select("#svg").append("defs").append("marker")
  .attr("id","arrow").attr("viewBox","0 0 10 10").attr("refX",5).attr("refY",10)
  .attr("markerWidth",6).attr("markerHeight",6).attr("orient","auto")
  .append("path").attr("d","M0,0L5,10L10,0").attr("fill","#4ecdc488");

function renderFlow(f) {
  flowList.selectAll(".flow").classed("active", function() { return d3.select(this).datum() === f; });
  main.select("svg").remove();
  const svg = main.append("svg");

  // Build flat layout: BFS order, horizontal flow with branching
  const nodes = [];
  const edges = [];
  const seen = new Set();
  const nodePos = new Map(); // nid -> {col, row}

  // Build node list first (to measure text)
  let y = 40;
  f.path.forEach((nid, idx) => {
    if (!nidMap[nid]) return;
    const n = nidMap[nid];
    if (seen.has(nid)) return;
    seen.add(nid);
    nodes.push({x: 0, y: y, label: n.label, qname: n.id, kind: n.kind, file: n.file, line: n.line, nid: nid, pos: idx});
    nodePos.set(nid, {x: 0, y: y});
    y += H_STEP;
  });

  const totalH = Math.max(y + 40, 600);
  svg.attr("width", SVG_W).attr("height", totalH);

  // Auto-size box width to longest text
  const tmp = svg.append("text").attr("font-size",13).style("opacity",0);
  const maxLabelW = d3.max(nodes, d => {
    tmp.text((d.pos+1)+". "+d.label);
    return tmp.node().getComputedTextLength();
  }) + 24;
  const maxFileW = d3.max(nodes, d => {
    tmp.text(d.file.split("/").pop()+":"+d.line+"  "+d.kind);
    return tmp.node().getComputedTextLength();
  }) + 24;
  const RECT_W = Math.max(200, Math.min(700, Math.max(maxLabelW, maxFileW)));
  const x = (SVG_W - RECT_W) / 2;
  tmp.remove();

  // Apply computed width + build edges
  nodes.forEach(n => { n.x = x; });
  nodePos.forEach(v => { v.x = x; });
  for (let i = 0; i < f.path.length - 1; i++) {
    const a = nodePos.get(f.path[i]), b = nodePos.get(f.path[i+1]);
    if (a && b) edges.push({x1: a.x + RECT_W/2, y1: a.y + RECT_H, x2: b.x + RECT_W/2, y2: b.y});
  }

  // Edges
  svg.selectAll(".edge").data(edges).join("line")
    .attr("class","edge")
    .attr("x1",d=>d.x1).attr("y1",d=>d.y1)
    .attr("x2",d=>d.x2).attr("y2",d=>d.y2)
    .attr("marker-end","url(#arrow)");

  // Nodes
  const node = svg.selectAll(".node").data(nodes).join("g")
    .attr("class", d => d.pos === 0 ? "node entry" : "node")
    .attr("transform", d => "translate("+d.x+","+d.y+")")
    .on("mouseover",(e,d)=>{
      tip.style("opacity",1).html("<b>"+d.qname+"</b><br>"+d.kind+" · "+d.file+":"+d.line);
    }).on("mouseout",()=>tip.style("opacity",0));

  node.append("rect")
    .attr("width", RECT_W).attr("height", RECT_H)
    .attr("stroke-width", d => d.pos === 0 ? 2 : 1);

  node.append("text")
    .text(d => (d.pos + 1) + ". " + d.label)
    .attr("x", 10).attr("y", 14)
    .attr("font-size", 13).attr("fill", "#fff");
  node.append("text")
    .text(d => d.file.split("/").pop() + ":" + d.line + "  " + d.kind)
    .attr("x", 10).attr("y", 30)
    .attr("font-size", 10).attr("fill", "#888");
}
</script></body></html>"""


_GRAPH_TEMPLATE = """<!DOCTYPE html>
<html><head><meta charset="utf-8"><title>Call Graph</title>
<style>
* { margin:0; padding:0; box-sizing:border-box; }
body { background:#0f0f1a; color:#ddd; font-family:system-ui; overflow:hidden; }
svg { width:100vw; height:100vh; }
.node circle { stroke-width:1.5px; cursor:pointer; }
.node text { fill:#ccc; font-size:9px; pointer-events:none; }
.link { stroke:#ffffff18; stroke-width:1px; }
#tooltip { position:fixed; padding:8px 12px; background:#1a1a2e; color:#e0e0e0;
           border-radius:6px; font-size:12px; pointer-events:none; opacity:0;
           border:1px solid #444; max-width:360px; z-index:20; }
</style></head><body>
<div id="tooltip"></div>
<script src="https://d3js.org/d3.v7.min.js"></script>
<script>
const data = __DATA__;
const svg = d3.select("body").append("svg"), g = svg.append("g");
const tip = d3.select("#tooltip");
const W = window.innerWidth, H = window.innerHeight;

const zoom = d3.zoom().scaleExtent([0.1, 4]).on("zoom", e => g.attr("transform", e.transform));
svg.call(zoom);

const radius = d3.scaleSqrt().domain([1, d3.max(data.nodes, d=>d.degree)]).range([4, 18]);
const color = d3.scaleOrdinal().domain(["function","method","class"])
    .range(["#4ecdc4","#7eb8ff","#ff6b6b"]);

const sim = d3.forceSimulation(data.nodes)
  .force("link", d3.forceLink(data.edges).id(d=>d.id).distance(60))
  .force("charge", d3.forceManyBody().strength(-300))
  .force("center", d3.forceCenter(W/2, H/2))
  .force("collide", d3.forceCollide().radius(d=>radius(d.degree)+6));

const link = g.selectAll("line").data(data.edges).join("line")
  .attr("class", "link");

const node = g.selectAll("g").data(data.nodes).join("g").attr("class", "node")
  .call(d3.drag().on("start",(e,d)=>{if(!e.active)sim.alphaTarget(0.3).restart();d.fx=d.x;d.fy=d.y;})
       .on("drag",(e,d)=>{d.fx=e.x;d.fy=e.y;})
       .on("end",(e,d)=>{if(!e.active)sim.alphaTarget(0);d.fx=null;d.fy=null;}));

node.append("circle").attr("r",d=>radius(d.degree))
  .attr("fill",d=>color(d.kind)).attr("stroke",d=>d3.color(color(d.kind)).darker(0.3))
  .on("mouseover",(e,d)=>{tip.style("opacity",1).html(`<b>${d.id}</b><br>${d.kind} · ${d.file}:${d.line}<br>degree=${d.degree}`);})
  .on("mouseout",()=>tip.style("opacity",0));

node.append("text").text(d=>d.label).attr("dx",12).attr("dy",3);

sim.on("tick",()=>{
  link.attr("x1",d=>d.source.x).attr("y1",d=>d.source.y)
      .attr("x2",d=>d.target.x).attr("y2",d=>d.target.y);
  node.attr("transform",d=>`translate(${d.x},${d.y})`);
});
</script></body></html>"""


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else ".code-review-ai/frontend.db"
    out = sys.argv[2] if len(sys.argv) > 2 else "graph.html"
    export(db, out)
