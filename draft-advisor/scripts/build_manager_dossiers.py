#!/usr/bin/env python3
"""Build per-manager dossiers (JSON + HTML) from enriched sales.

For each manager: per-season squads with price and points actually scored
that season, plus aggregate tendencies the bidding model will use —
spend concentration, position splits, club favourites, value record.
"""
import json
from collections import defaultdict
from html import escape
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DATA = ROOT / "data"
SEASONS = ["2023-24", "2024-25", "2025-26"]


def build_profiles():
    sales = json.loads((DATA / "enriched_sales.json").read_text())
    mgrs = defaultdict(lambda: {"seasons": defaultdict(list)})
    for s in sales:
        mgrs[s["manager"]]["seasons"][s["season"]].append(s)

    profiles = []
    for name, m in mgrs.items():
        seasons_out = {}
        agg_spend = agg_pts = 0.0
        priced_spend = priced_pts = 0.0  # only rows with a known price
        priced_seasons = 0
        pos_spend = defaultdict(float)
        club_count = defaultdict(int)
        all_buys = []
        for season, rows in m["seasons"].items():
            spend = sum(r["cost"] or 0 for r in rows)
            pts = sum(r["points_that_season"] or 0 for r in rows)
            matched = sum(1 for r in rows if r["points_that_season"] is not None)
            for r in rows:
                if r["cost"] is not None:
                    pos_spend[r["position"]] += r["cost"]
                    all_buys.append(r)
                if r["club"]:
                    club_count[r["club"]] += 1
            squad = sorted(
                rows, key=lambda r: -(r["cost"] if r["cost"] is not None else -1)
            )
            best = worst = None
            priced = [
                r for r in rows
                if r["cost"] is not None and r["points_that_season"] is not None
            ]
            bargains = [r for r in priced if r["points_that_season"] >= 60]
            if bargains:
                best = min(bargains, key=lambda r: r["cost"] / r["points_that_season"])
            busts = [r for r in priced if r["cost"] >= 4]
            if busts:
                worst = max(
                    busts, key=lambda r: r["cost"] / max(r["points_that_season"], 1)
                )
            seasons_out[season] = {
                "squad": squad,
                "spend": round(spend, 2),
                "points_bought": pts,
                "points_matched_players": matched,
                "best_value": best,
                "biggest_bust": worst,
            }
            agg_spend += spend
            agg_pts += pts
            has_prices = any(r["cost"] is not None for r in rows)
            if has_prices:
                priced_seasons += 1
                priced_spend += spend
                priced_pts += sum(
                    r["points_that_season"] or 0
                    for r in rows
                    if r["cost"] is not None
                )

        prices = sorted(
            (r["cost"] for r in all_buys if r["cost"] is not None), reverse=True
        )
        top3_share = (
            round(sum(prices[:3]) / agg_spend * 100, 1) if agg_spend else None
        )
        total_pos = sum(pos_spend.values()) or 1
        profiles.append(
            {
                "name": name,
                "years": sorted(m["seasons"].keys()),
                "avg_spend": round(priced_spend / priced_seasons, 2)
                if priced_seasons
                else None,
                "top3_share": top3_share,
                "pos_split": {
                    p: round(v / total_pos * 100, 1) for p, v in pos_spend.items()
                },
                "fav_clubs": sorted(
                    club_count.items(), key=lambda kv: -kv[1]
                )[:3],
                "pts_per_million": round(priced_pts / priced_spend, 1)
                if priced_spend
                else None,
                "total_points_bought": int(agg_pts),
                "max_ever_bid": prices[0] if prices else None,
                "seasons": seasons_out,
            }
        )

    profiles.sort(key=lambda p: -(p["pts_per_million"] or 0))
    (DATA / "manager_dossiers.json").write_text(json.dumps(profiles, indent=1))
    return profiles


def render_html(profiles):
    payload = json.dumps(profiles, separators=(",", ":"))
    # note in the template: {{ }} are literal braces (no str.format used)
    html = """<title>OMTFFL Draft Dossiers</title>
<style>
:root{
  --bg:#0d0f12; --surface:#141720; --surface2:#1c2030;
  --border:rgba(255,255,255,.07); --border2:rgba(255,255,255,.13);
  --text:#e8eaf0; --muted:#6b7280; --accent:#7fffb2; --hot:#ff6b6b; --gold:#ffd166; --blue:#60a5fa;
  --mono:ui-monospace,'Cascadia Code','JetBrains Mono',Menlo,Consolas,monospace;
  --sans:'Avenir Next','Futura','Segoe UI',system-ui,sans-serif;
}
*{box-sizing:border-box;margin:0;padding:0}
body{background:var(--bg);color:var(--text);font-family:var(--mono);font-size:13px;padding:0 0 6rem}
header{padding:2.2rem 2rem 1.4rem;border-bottom:1px solid var(--border)}
h1{font-family:var(--sans);font-weight:800;font-size:26px;letter-spacing:-.5px}
h1 span{color:var(--accent)}
.sub{color:var(--muted);margin-top:.4rem;font-size:12px}
.controls{display:flex;gap:.6rem;flex-wrap:wrap;padding:1.1rem 2rem;position:sticky;top:0;background:rgba(13,15,18,.95);backdrop-filter:blur(8px);z-index:5;border-bottom:1px solid var(--border)}
input[type=search]{background:var(--surface);border:1px solid var(--border2);border-radius:8px;color:var(--text);font-family:var(--mono);font-size:13px;padding:.55rem .9rem;width:230px}
input[type=search]:focus{outline:2px solid var(--accent);outline-offset:1px}
select{background:var(--surface);border:1px solid var(--border2);border-radius:8px;color:var(--text);font-family:var(--mono);font-size:12px;padding:.55rem .7rem}
.grid{display:grid;grid-template-columns:repeat(auto-fill,minmax(340px,1fr));gap:1rem;padding:1.4rem 2rem}
.card{background:var(--surface);border:1px solid var(--border);border-radius:12px;padding:1.1rem 1.2rem;display:flex;flex-direction:column;gap:.8rem}
.card h2{font-family:var(--sans);font-size:18px;font-weight:700;display:flex;align-items:baseline;gap:.6rem}
.rank{color:var(--muted);font-family:var(--mono);font-size:11px}
.statrow{display:grid;grid-template-columns:repeat(3,1fr);gap:.5rem}
.stat{background:var(--surface2);border-radius:8px;padding:.5rem .6rem}
.stat b{display:block;font-size:15px;font-variant-numeric:tabular-nums;color:var(--accent)}
.stat.warn b{color:var(--gold)}
.stat span{color:var(--muted);font-size:10px;letter-spacing:.05em;text-transform:uppercase}
.bar{display:flex;height:9px;border-radius:5px;overflow:hidden;border:1px solid var(--border2)}
.bar i{display:block;height:100%}
.bar .gk{background:var(--muted)} .bar .def{background:var(--blue)} .bar .mid{background:var(--accent)} .bar .fwd{background:var(--hot)}
.legend{display:flex;gap:.9rem;color:var(--muted);font-size:10px;text-transform:uppercase;letter-spacing:.05em}
.legend i{display:inline-block;width:8px;height:8px;border-radius:2px;margin-right:4px;vertical-align:middle}
.chips{display:flex;gap:.4rem;flex-wrap:wrap}
.chip{border:1px solid var(--border2);border-radius:20px;padding:2px 9px;font-size:11px;color:var(--muted)}
.chip b{color:var(--text)}
details{border-top:1px solid var(--border);padding-top:.6rem}
summary{cursor:pointer;color:var(--muted);font-size:11px;text-transform:uppercase;letter-spacing:.06em}
summary:hover{color:var(--text)}
summary:focus-visible{outline:2px solid var(--accent);border-radius:4px}
table{width:100%;border-collapse:collapse;margin-top:.5rem;font-variant-numeric:tabular-nums}
th{color:var(--muted);font-weight:400;font-size:10px;text-transform:uppercase;letter-spacing:.05em;text-align:right;padding:3px 6px;border-bottom:1px solid var(--border)}
th:first-child,td:first-child{text-align:left}
td{padding:3px 6px;font-size:12px;text-align:right;border-bottom:1px solid var(--border)}
td.pos{color:var(--muted);font-size:10px}
tr.hit td:first-child{color:var(--accent)}
tr.miss td:first-child{color:var(--hot)}
.tag{font-size:11px;color:var(--muted);margin-top:.3rem}
.tag b.good{color:var(--accent)} .tag b.bad{color:var(--hot)}
.tablewrap{overflow-x:auto}
@media (prefers-reduced-motion:no-preference){.card{transition:border-color .15s}.card:hover{border-color:var(--border2)}}
</style>
<header>
  <h1>OMTFFL <span>Draft Dossiers</span></h1>
  <div class="sub">Every manager's auction record, 2023-24 &rarr; 2025-26 &middot; points = FPL points the player scored that same season &middot; ranked by points bought per &pound;m spent</div>
</header>
<div class="controls">
  <input type="search" id="q" placeholder="Find manager..." aria-label="Find manager">
  <select id="sort" aria-label="Sort managers">
    <option value="value">Sort: value (pts/&pound;m)</option>
    <option value="spend">Sort: avg spend</option>
    <option value="top3">Sort: top-3 concentration</option>
    <option value="name">Sort: name</option>
  </select>
</div>
<div class="grid" id="grid"></div>
<script>
const P = __DATA__;
const fmt = n => n==null ? "?" : "£"+n+"m";
function seasonBlock(season, s){
  const rows = s.squad.map(r=>{
    const pts = r.points_that_season;
    const cls = pts==null ? "" : (pts>=100 ? "hit" : (r.cost>=4 && pts<60 ? "miss" : ""));
    return `<tr class="${cls}"><td>${esc(r.player)}</td><td class="pos">${r.position}</td><td>${r.cost==null?"?":r.cost}</td><td>${pts==null?"–":pts}</td></tr>`;
  }).join("");
  const bv = s.best_value, bb = s.biggest_bust;
  return `<details><summary>${season} · spent £${s.spend}m · bought ${s.points_bought} pts</summary>
  ${bv?`<div class="tag">best value <b class="good">${esc(bv.player)}</b> £${bv.cost}m → ${bv.points_that_season} pts</div>`:""}
  ${bb?`<div class="tag">priciest miss <b class="bad">${esc(bb.player)}</b> £${bb.cost}m → ${bb.points_that_season} pts</div>`:""}
  <div class="tablewrap"><table><tr><th>player</th><th>pos</th><th>£m</th><th>pts</th></tr>${rows}</table></div></details>`;
}
function esc(s){return String(s).replace(/[&<>"]/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;"}[c]))}
function card(p, i){
  const ps = p.pos_split, seg = k => ps[k]?`<i class="${k.toLowerCase()}" style="width:${ps[k]}%" title="${k} ${ps[k]}%"></i>`:"";
  const clubs = p.fav_clubs.map(c=>`<span class="chip"><b>${esc(c[0])}</b> ×${c[1]}</span>`).join("");
  const seasons = p.years.map(y=>seasonBlock(y,p.seasons[y])).join("");
  return `<div class="card" data-name="${esc(p.name.toLowerCase())}">
    <h2>${esc(p.name)} <span class="rank">#${i+1} · ${p.years.length} season${p.years.length>1?"s":""}</span></h2>
    <div class="statrow">
      <div class="stat"><b>${p.pts_per_million??"–"}</b><span>pts per £m</span></div>
      <div class="stat"><b>£${p.avg_spend}m</b><span>avg spend</span></div>
      <div class="stat warn"><b>${p.top3_share??"–"}%</b><span>in top 3 buys</span></div>
    </div>
    <div class="bar">${seg("GK")}${seg("DEF")}${seg("MID")}${seg("FWD")}</div>
    <div class="legend"><span><i style="background:var(--muted)"></i>GK</span><span><i style="background:var(--blue)"></i>DEF</span><span><i style="background:var(--accent)"></i>MID</span><span><i style="background:var(--hot)"></i>FWD</span></div>
    <div class="chips">${clubs}<span class="chip">max bid <b>${fmt(p.max_ever_bid)}</b></span></div>
    ${seasons}
  </div>`;
}
const grid = document.getElementById("grid");
function render(){
  const q = document.getElementById("q").value.toLowerCase();
  const mode = document.getElementById("sort").value;
  let list = [...P];
  if(mode==="spend") list.sort((a,b)=>b.avg_spend-a.avg_spend);
  if(mode==="top3") list.sort((a,b)=>(b.top3_share||0)-(a.top3_share||0));
  if(mode==="name") list.sort((a,b)=>a.name.localeCompare(b.name));
  grid.innerHTML = list.map((p)=>card(p,P.indexOf(p))).join("");
  for(const c of grid.children) c.style.display = c.dataset.name.includes(q) ? "" : "none";
}
document.getElementById("q").addEventListener("input",render);
document.getElementById("sort").addEventListener("change",render);
render();
</script>
"""
    (DATA.parent / "dossiers.html").write_text(html.replace("__DATA__", payload))


if __name__ == "__main__":
    profiles = build_profiles()
    render_html(profiles)
    print(f"{len(profiles)} manager dossiers -> draft-advisor/dossiers.html")
    for p in profiles[:5]:
        print(f"  {p['name']:10s} {p['pts_per_million']} pts/£m, avg £{p['avg_spend']}m")
