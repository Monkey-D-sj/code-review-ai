"""Export the call graph + communities as an interactive dual-level HTML visualization.

Level 1 — Community bubbles: each community is a circle, sized by node count.
           Inter-community edges show call relationships between communities.
Level 2 — Click a bubble to drill into its internal call graph.
"""

import json
import sqlite3
import sys
from collections import defaultdict


def export(db_path: str, out_path: str, max_communities: int = 50) -> None:
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
                 if w >= 1]  # show all cross-community calls

    nodes_json = {str(c["id"]): comm_nodes[c["id"]] for c in comms}

    data = json.dumps({
        "communities": communities_json,
        "edges": edge_json,
        "nodes": nodes_json,
    }, ensure_ascii=False)

    html = _TEMPLATE.replace("__DATA__", data)
    with open(out_path, "w", encoding="utf-8") as f:
        f.write(html)

    n_edges = len(edge_json)
    print(f"Exported {len(comms)} communities, {n_edges} inter-community edges → {out_path}")


_TEMPLATE = """<!DOCTYPE html>
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
.comm-link { stroke:#ffffff15; stroke-width:1px; fill:none; }
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
  .attr("stroke-width", d => Math.min(6, Math.log(d.weight + 1) * 1.8));

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
  .text(d => d.label.length > 18 ? d.label.slice(0,17)+"…" : d.label)
  .attr("dy", d => -radius(d.nodeCount) - 6);

sim.on("tick", () => {
  link.attr("x1", d => d.source.x).attr("y1", d => d.source.y)
      .attr("x2", d => d.target.x).attr("y2", d => d.target.y);
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


if __name__ == "__main__":
    db = sys.argv[1] if len(sys.argv) > 1 else ".code-review-ai/frontend.db"
    out = sys.argv[2] if len(sys.argv) > 2 else "graph.html"
    export(db, out)
