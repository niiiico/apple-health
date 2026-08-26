"""Generate a standalone, self-contained HTML training report from ``health.db``.

The report embeds its data as JSON and renders charts client-side with D3.js
(loaded from a CDN), so the output is a single ``.html`` file that opens in any
browser. It surfaces the headline training story — VO2max recovery, yearly and
monthly running volume, intensity distribution, running mechanics — alongside a
data-driven recommendations section.

Usage::

    uv run ah-html --db data/health.db --out reports/health-report.html
"""

from __future__ import annotations

import argparse
import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from ..derive.zones import ZONES

# Quarterly bucket expression: "YYYY-Qn" from a column holding an ISO date.
_Q = "substr({c},1,4)||'-Q'||((cast(substr({c},6,2) AS int)+2)/3)"


def _zone_case() -> str:
    """SQL `CASE` bucketing `avg_hr` into the canonical zones.

    Generated rather than written out, so this report cannot drift from
    `derive.zones` the way its hand-maintained copy did — the two disagreed on
    Z1's label for months. Bands are inclusive of `hi`, hence `< hi + 1`.
    """
    whens = " ".join(
        f"WHEN avg_hr<{int(hi) + 1} THEN '{label}'" for label, _, hi in ZONES[:-1]
    )
    return f"CASE {whens} ELSE '{ZONES[-1][0]}' END"


# Zone palette, cool → hot, positional against ZONES.
_ZONE_COLORS = ("#6ad1ff", "#37c871", "#ffd454", "#ffb454", "#ff5d5d")


def _zone_colors() -> dict[str, str]:
    """Label → colour for the donut, keyed off the canonical bands.

    Hand-keying this map is what made a label change silently un-colour an arc
    rather than fail.
    """
    return {label: c for (label, _, _), c in zip(ZONES, _ZONE_COLORS)}


def _rows(con: sqlite3.Connection, sql: str, params: tuple = ()) -> list[dict]:
    """Run a query and return a list of column-name → value dicts."""
    cur = con.execute(sql, params)
    cols = [d[0] for d in cur.description]
    return [dict(zip(cols, row)) for row in cur.fetchall()]


def _scalar(con: sqlite3.Connection, sql: str, params: tuple = ()):
    """Return the first column of the first row, or ``None``."""
    row = con.execute(sql, params).fetchone()
    return row[0] if row else None


def gather(con: sqlite3.Connection) -> dict:
    """Pull every series and headline figure the report needs from the DB."""
    data: dict = {}

    # --- Headline KPIs -----------------------------------------------------
    span_lo = _scalar(con, "SELECT min(start) FROM workouts")
    span_hi = _scalar(con, "SELECT max(start) FROM workouts")
    pr = con.execute(
        """SELECT substr(start,1,10) d, distance_km, duration_min, avg_hr
           FROM workouts
           WHERE activity='Running' AND distance_km BETWEEN 21 AND 21.6
           ORDER BY duration_min ASC LIMIT 1"""
    ).fetchone()
    data["kpis"] = {
        "workouts": _scalar(con, "SELECT count(*) FROM workouts"),
        "run_km": _scalar(
            con, "SELECT round(sum(distance_km)) FROM workouts WHERE activity='Running'"
        ),
        "vo2_now": _scalar(
            con, "SELECT round(value,1) FROM records WHERE type='VO2Max' ORDER BY start DESC LIMIT 1"
        ),
        "mass_now": _scalar(
            con,
            "SELECT round(avg,1) FROM daily_metrics WHERE type='BodyMass' ORDER BY day DESC LIMIT 1",
        ),
        "span_lo": (span_lo or "")[:10],
        "span_hi": (span_hi or "")[:10],
        "pr": None
        if pr is None
        else {
            "date": pr[0],
            "km": round(pr[1], 1),
            "min": round(pr[2]),
            "pace": _pace(pr[2] / pr[1]),
            "hr": None if pr[3] is None else round(pr[3]),
        },
    }

    # --- Trend series ------------------------------------------------------
    data["vo2"] = _rows(
        con,
        f"""SELECT {_Q.format(c='start')} AS q, round(avg(value),1) AS v
            FROM records WHERE type='VO2Max' AND start>='2021-01-01'
            GROUP BY q ORDER BY q""",
    )
    data["rhr"] = _rows(
        con,
        f"""SELECT {_Q.format(c='day')} AS q, round(avg(avg),1) AS v
            FROM daily_metrics WHERE type='RestingHeartRate' AND day>='2021-01-01'
            GROUP BY q ORDER BY q""",
    )
    data["hrv"] = _rows(
        con,
        f"""SELECT {_Q.format(c='day')} AS q, round(avg(avg),1) AS v
            FROM daily_metrics WHERE type='HeartRateVariabilitySDNN' AND day>='2021-01-01'
            GROUP BY q ORDER BY q""",
    )
    data["yearly"] = _rows(
        con,
        """SELECT substr(start,1,4) AS yr, count(*) AS runs,
                  round(sum(distance_km)) AS km, round(avg(avg_hr)) AS hr
           FROM workouts WHERE activity='Running' AND start>='2013-01-01'
           GROUP BY yr ORDER BY yr""",
    )
    data["monthly"] = _rows(
        con,
        """SELECT substr(start,1,7) AS m, round(sum(distance_km),1) AS km,
                  count(*) AS runs, round(avg(avg_hr)) AS hr
           FROM workouts WHERE activity='Running' AND start>='2025-01-01'
           GROUP BY m ORDER BY m""",
    )
    data["cadence"] = _rows(
        con,
        """SELECT substr(day,1,4) AS yr, round(avg(avg),1) AS v
           FROM daily_metrics WHERE type='RunningCadence' AND day>='2022-01-01'
           GROUP BY yr ORDER BY yr""",
    )

    # Intensity zones over the most recent full-ish year of running.
    year = (span_hi or "2026")[:4]
    data["zones"] = _rows(
        con,
        f"""SELECT {_zone_case()} AS zone,
                  count(*) AS runs
           FROM workouts
           WHERE activity='Running' AND avg_hr IS NOT NULL AND substr(start,1,4)=?
           GROUP BY zone ORDER BY zone""",
        (year,),
    )
    data["zones_year"] = year

    # Lifetime training mix (top activities by hours).
    data["mix"] = _rows(
        con,
        """SELECT activity, count(*) AS n, round(sum(duration_min)/60) AS hrs
           FROM workouts GROUP BY activity HAVING hrs>0 ORDER BY hrs DESC LIMIT 8""",
    )
    return data


def _pace(min_per_km: float) -> str:
    """Format minutes-per-km as ``m:ss``."""
    m = int(min_per_km)
    s = round((min_per_km - m) * 60)
    if s == 60:
        m, s = m + 1, 0
    return f"{m}:{s:02d}"


def render(data: dict, generated_at: str) -> str:
    """Render the full HTML document for the given data bundle."""
    return (
        _TEMPLATE.replace("__DATA__", json.dumps(data))
        .replace("__ZONE_COLORS__", json.dumps(_zone_colors()))
        .replace("__GENERATED__", generated_at)
        .replace("__SPAN__", f"{data['kpis']['span_lo']} → {data['kpis']['span_hi']}")
    )


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(description="Generate an HTML training report from health.db")
    ap.add_argument("--db", default="data/health.db", help="path to the SQLite DB")
    ap.add_argument(
        "--out", default="reports/health-report.html", help="output HTML file path"
    )
    args = ap.parse_args(argv)

    con = sqlite3.connect(args.db)
    try:
        data = gather(con)
    finally:
        con.close()

    generated = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")
    out = Path(args.out)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text(render(data, generated), encoding="utf-8")
    print(f"Wrote {out}  ({out.stat().st_size:,} bytes)")


# --------------------------------------------------------------------------
# HTML template. JS braces are literal — placeholders are __UPPER__ tokens
# substituted in render(), so no str.format()/f-string is applied here.
# --------------------------------------------------------------------------
_TEMPLATE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Apple Health — Training Report</title>
<script src="https://d3js.org/d3.v7.min.js"></script>
<style>
  :root {
    --bg:#0f1115; --panel:#181b22; --ink:#e8eaed; --muted:#9aa0aa;
    --accent:#4f8cff; --good:#37c871; --warn:#ffb454; --bad:#ff5d5d;
    --grid:#2a2f3a;
  }
  * { box-sizing:border-box; }
  body { margin:0; background:var(--bg); color:var(--ink);
    font:15px/1.5 -apple-system,BlinkMacSystemFont,"Segoe UI",Roboto,sans-serif; }
  header { padding:32px 28px 8px; }
  h1 { margin:0; font-size:26px; letter-spacing:-.02em; }
  .sub { color:var(--muted); margin-top:6px; font-size:13px; }
  main { max-width:1180px; margin:0 auto; padding:16px 24px 64px; }
  .kpis { display:grid; grid-template-columns:repeat(auto-fit,minmax(150px,1fr)); gap:14px; margin:18px 0 28px; }
  .kpi { background:var(--panel); border:1px solid var(--grid); border-radius:14px; padding:16px 18px; }
  .kpi .v { font-size:28px; font-weight:650; letter-spacing:-.02em; }
  .kpi .l { color:var(--muted); font-size:12px; text-transform:uppercase; letter-spacing:.04em; margin-top:4px; }
  .kpi .d { color:var(--muted); font-size:12px; margin-top:6px; }
  .grid { display:grid; grid-template-columns:repeat(2,1fr); gap:18px; }
  @media (max-width:860px){ .grid{ grid-template-columns:1fr; } }
  .card { background:var(--panel); border:1px solid var(--grid); border-radius:14px; padding:16px 18px 8px; }
  .card h2 { font-size:15px; margin:2px 0 2px; }
  .card .note { color:var(--muted); font-size:12px; margin:0 0 8px; }
  .full { grid-column:1 / -1; }
  svg { width:100%; height:auto; display:block; }
  .axis text { fill:var(--muted); font-size:11px; }
  .axis line, .axis path { stroke:var(--grid); }
  .gridline line { stroke:var(--grid); stroke-dasharray:2 3; }
  .tip { position:fixed; pointer-events:none; background:#000c; color:#fff; padding:6px 9px;
    border-radius:8px; font-size:12px; opacity:0; transition:opacity .08s; }
  .recs { background:var(--panel); border:1px solid var(--grid); border-radius:14px; padding:20px 24px; margin-top:24px; }
  .recs h2 { margin-top:0; }
  .recs h3 { font-size:14px; color:var(--accent); margin:18px 0 6px; }
  .recs ul { margin:6px 0; padding-left:20px; }
  .recs li { margin:4px 0; }
  .pill { display:inline-block; font-size:11px; padding:2px 8px; border-radius:99px; background:#2a2f3a; color:var(--muted); margin-left:6px; }
  table.mini { width:100%; border-collapse:collapse; font-size:13px; }
  table.mini td { padding:4px 6px; border-bottom:1px solid var(--grid); }
  table.mini td:last-child { text-align:right; color:var(--muted); }
  footer { color:var(--muted); font-size:12px; text-align:center; padding:24px; }
</style>
</head>
<body>
<header>
  <h1>Apple Health — Training Report</h1>
  <div class="sub">Workout span __SPAN__ · generated __GENERATED__</div>
</header>
<main>
  <section class="kpis" id="kpis"></section>

  <div class="grid">
    <div class="card full">
      <h2>VO2max — the recovery story</h2>
      <p class="note">Quarterly average (ml/kg/min). Peak 2023 → 2024 dip → near-full recovery.</p>
      <div id="vo2"></div>
    </div>

    <div class="card">
      <h2>Running volume per year</h2>
      <p class="note">Kilometres run. The 2024 drop, then rebuild.</p>
      <div id="yearly"></div>
    </div>
    <div class="card">
      <h2>Monthly volume (2025 →)</h2>
      <p class="note">Recent build — kilometres per month.</p>
      <div id="monthly"></div>
    </div>

    <div class="card">
      <h2>Intensity distribution <span class="pill" id="zoneYear"></span></h2>
      <p class="note">Runs by average-HR zone. Mostly easy — quality work is the gap.</p>
      <div id="zones"></div>
    </div>
    <div class="card">
      <h2>Cadence drift</h2>
      <p class="note">Steps/min by year. Target band 168–172 shaded.</p>
      <div id="cadence"></div>
    </div>

    <div class="card">
      <h2>Resting HR &amp; HRV</h2>
      <p class="note">Quarterly: resting HR (lower better) and HRV SDNN (higher better).</p>
      <div id="hr"></div>
    </div>
    <div class="card">
      <h2>Lifetime training mix</h2>
      <p class="note">Hours by activity (top 8).</p>
      <div id="mix"></div>
    </div>
  </div>

  <section class="recs">
    <h2>Recommendations</h2>
    <p class="note">Data-driven, polarised (≈80/20). 5 training days available (rest Sun/Mon for treatment).</p>

    <h3>Targets</h3>
    <ul>
      <li><b>~3 months:</b> flat half-marathon <b>sub-1:50</b> (current VO2max ≈47.6 and threshold ≈5:20–5:30/km support it; Yamanakako ~1:59 ≈ 1:48–1:50 flat).</li>
      <li><b>~12 months:</b> attack the <b>1:45 PR</b> (4:58/km) once frequency &amp; weight return to 2022 levels.</li>
      <li><b>This summer:</b> complete the Olympic-distance triathlon (swim base-building underway).</li>
    </ul>

    <h3>Process levers (these drive the targets)</h3>
    <ul>
      <li><b>Frequency 1.5 → 3 runs/week</b> — same weekly km, split smaller. The single biggest gap in the data.</li>
      <li><b>Cadence 160 → 168–172 spm</b> — highest-ROI mechanical fix; also lowers per-step impact.</li>
      <li><b>Body mass → ~76 kg</b> — roughly 6–8 s/km free at half pace.</li>
      <li><b>VO2max past 48</b>, HRV trending up.</li>
    </ul>

    <h3>Weekly structure</h3>
    <ul>
      <li><b>2× easy Z2</b> — 6:00–6:30/km, HR &lt;155. Keep them genuinely easy.</li>
      <li><b>1× quality</b> (the missing stimulus), alternating: <i>threshold</i> 3–4 × 8 min @ 5:15–5:20/km (HR 170–175) / 2 min jog; or <i>VO2</i> 5–6 × 3 min hard / 2 min easy (every 3rd week).</li>
      <li><b>1× long run</b> every 1–2 weeks, easy, building 16 → 22 km.</li>
      <li><b>Bike / swim</b> for remaining aerobic volume — low-impact, joint-friendly.</li>
      <li><b>Cadence drills</b> — metronome 170 + 4–6 × 20 s strides post-run.</li>
    </ul>

    <h3>Load guardrails</h3>
    <ul>
      <li>Cap weekly volume growth at ≈10%.</li>
      <li>Don't let HIIT crowd out run-specific quality.</li>
      <li><b>Watch HRV / resting HR</b> as an overreach &amp; RA-flare tripwire: if HRV stays depressed and/or resting HR climbs for several days, swap a quality session for easy / cross-training.</li>
    </ul>
  </section>
</main>
<footer>Generated from <code>health.db</code> · charts by D3.js</footer>

<div class="tip" id="tip"></div>
<script>
const DATA = __DATA__;
const tip = d3.select("#tip");
const show = (html, e) => tip.html(html).style("opacity",1)
  .style("left",(e.clientX+12)+"px").style("top",(e.clientY+12)+"px");
const hide = () => tip.style("opacity",0);
const W = 560, H = 240, M = {t:14,r:16,b:28,l:38};

function svg(sel){
  return d3.select(sel).append("svg")
    .attr("viewBox", `0 0 ${W} ${H}`).attr("preserveAspectRatio","xMidYMid meet");
}
function yGrid(g, y){
  g.append("g").attr("class","gridline")
    .call(d3.axisLeft(y).tickSize(-(W-M.l-M.r)).tickFormat(""))
    .select(".domain").remove();
}

// ---- KPI cards ----
(function(){
  const k = DATA.kpis;
  const cards = [
    {v:k.vo2_now, l:"VO2max now", d:"ml/kg/min · near 2023 peak"},
    {v:(k.run_km||0).toLocaleString()+" km", l:"Lifetime running", d:k.workouts+" workouts total"},
    {v:(k.mass_now||"—")+" kg", l:"Body mass", d:"≈2–3 kg over 2022 race weight"},
    k.pr ? {v:fmtHMS(k.pr.min), l:"Half-marathon PR", d:k.pr.date+" · "+k.pr.pace+"/km"} : null,
  ].filter(Boolean);
  d3.select("#kpis").selectAll("div").data(cards).join("div").attr("class","kpi")
    .html(c => `<div class="v">${c.v}</div><div class="l">${c.l}</div><div class="d">${c.d}</div>`);
})();
function fmtHMS(min){ const h=Math.floor(min/60), m=min%60; return h+":"+String(m).padStart(2,"0"); }

// ---- VO2max line ----
(function(){
  const d = DATA.vo2; if(!d.length) return;
  const s = svg("#vo2");
  const x = d3.scalePoint().domain(d.map(r=>r.q)).range([M.l, W-M.r]).padding(.4);
  const y = d3.scaleLinear().domain([d3.min(d,r=>r.v)-2, d3.max(d,r=>r.v)+2]).range([H-M.b, M.t]);
  yGrid(s, y);
  s.append("g").attr("class","axis").attr("transform",`translate(0,${H-M.b})`)
    .call(d3.axisBottom(x).tickValues(d.filter((r,i)=>i%2===0).map(r=>r.q)));
  s.append("g").attr("class","axis").attr("transform",`translate(${M.l},0)`).call(d3.axisLeft(y).ticks(5));
  const line = d3.line().x(r=>x(r.q)).y(r=>y(r.v));
  s.append("path").datum(d).attr("fill","none").attr("stroke","var(--accent)").attr("stroke-width",2.5).attr("d",line);
  const peak = d3.max(d,r=>r.v), low = d3.min(d,r=>r.v);
  s.selectAll("circle").data(d).join("circle").attr("cx",r=>x(r.q)).attr("cy",r=>y(r.v)).attr("r",3.5)
    .attr("fill",r=> r.v===peak?"var(--good)": r.v===low?"var(--bad)":"var(--accent)")
    .on("mousemove",(e,r)=>show(`<b>${r.q}</b><br>${r.v} ml/kg/min`,e)).on("mouseleave",hide);
})();

// ---- Yearly volume bars ----
(function(){
  const d = DATA.yearly; if(!d.length) return;
  const s = svg("#yearly");
  const x = d3.scaleBand().domain(d.map(r=>r.yr)).range([M.l,W-M.r]).padding(.2);
  const y = d3.scaleLinear().domain([0, d3.max(d,r=>r.km)*1.1]).range([H-M.b,M.t]);
  yGrid(s,y);
  s.append("g").attr("class","axis").attr("transform",`translate(0,${H-M.b})`)
    .call(d3.axisBottom(x).tickValues(d.filter((r,i)=>i%2===0).map(r=>r.yr)));
  s.append("g").attr("class","axis").attr("transform",`translate(${M.l},0)`).call(d3.axisLeft(y).ticks(5));
  const peak = d3.max(d,r=>r.km);
  s.selectAll("rect").data(d).join("rect").attr("x",r=>x(r.yr)).attr("width",x.bandwidth())
    .attr("y",r=>y(r.km)).attr("height",r=>H-M.b-y(r.km)).attr("rx",2)
    .attr("fill",r=> r.km===peak?"var(--good)":"var(--accent)").attr("opacity",.9)
    .on("mousemove",(e,r)=>show(`<b>${r.yr}</b><br>${r.km} km · ${r.runs} runs${r.hr?` · HR ${r.hr}`:""}`,e)).on("mouseleave",hide);
})();

// ---- Monthly volume bars ----
(function(){
  const d = DATA.monthly; if(!d.length) return;
  const s = svg("#monthly");
  const x = d3.scaleBand().domain(d.map(r=>r.m)).range([M.l,W-M.r]).padding(.15);
  const y = d3.scaleLinear().domain([0, d3.max(d,r=>r.km)*1.1]).range([H-M.b,M.t]);
  yGrid(s,y);
  s.append("g").attr("class","axis").attr("transform",`translate(0,${H-M.b})`)
    .call(d3.axisBottom(x).tickValues(d.filter((r,i)=>i%3===0).map(r=>r.m))
      .tickFormat(m=>m.slice(2)));
  s.append("g").attr("class","axis").attr("transform",`translate(${M.l},0)`).call(d3.axisLeft(y).ticks(5));
  s.selectAll("rect").data(d).join("rect").attr("x",r=>x(r.m)).attr("width",x.bandwidth())
    .attr("y",r=>y(r.km)).attr("height",r=>H-M.b-y(r.km)).attr("rx",2).attr("fill","var(--accent)").attr("opacity",.85)
    .on("mousemove",(e,r)=>show(`<b>${r.m}</b><br>${r.km} km · ${r.runs} runs`,e)).on("mouseleave",hide);
})();

// ---- Zone donut ----
(function(){
  const d = DATA.zones; d3.select("#zoneYear").text(DATA.zones_year); if(!d.length) return;
  const s = svg("#zones"); const cx=W/2, cy=H/2-4, rad=Math.min(W,H)/2-30;
  const color = __ZONE_COLORS__;
  const pie = d3.pie().value(r=>r.runs).sort(null);
  const arc = d3.arc().innerRadius(rad*.55).outerRadius(rad);
  const total = d3.sum(d,r=>r.runs);
  const g = s.append("g").attr("transform",`translate(${cx},${cy})`);
  g.selectAll("path").data(pie(d)).join("path").attr("d",arc)
    .attr("fill",p=>color[p.data.zone]||"#888")
    .on("mousemove",(e,p)=>show(`<b>${p.data.zone}</b><br>${p.data.runs} runs (${Math.round(p.data.runs/total*100)}%)`,e)).on("mouseleave",hide);
  g.append("text").attr("text-anchor","middle").attr("dy","-.1em").attr("fill","var(--ink)")
    .attr("font-size","22").attr("font-weight","650").text(total);
  g.append("text").attr("text-anchor","middle").attr("dy","1.3em").attr("fill","var(--muted)").attr("font-size","11").text("runs");
  // legend
  const lg = s.append("g").attr("transform",`translate(${W-150},${M.t})`);
  lg.selectAll("g").data(d).join("g").attr("transform",(r,i)=>`translate(0,${i*18})`).each(function(r){
    const e=d3.select(this);
    e.append("rect").attr("width",11).attr("height",11).attr("rx",2).attr("fill",color[r.zone]||"#888");
    e.append("text").attr("x",16).attr("y",10).attr("fill","var(--muted)").attr("font-size","11").text(r.zone);
  });
})();

// ---- Cadence line w/ target band ----
(function(){
  const d = DATA.cadence; if(!d.length) return;
  const s = svg("#cadence");
  const x = d3.scalePoint().domain(d.map(r=>r.yr)).range([M.l,W-M.r]).padding(.5);
  const y = d3.scaleLinear().domain([155, 175]).range([H-M.b,M.t]);
  yGrid(s,y);
  s.append("rect").attr("x",M.l).attr("width",W-M.l-M.r).attr("y",y(172)).attr("height",y(168)-y(172))
    .attr("fill","var(--good)").attr("opacity",.12);
  s.append("g").attr("class","axis").attr("transform",`translate(0,${H-M.b})`).call(d3.axisBottom(x));
  s.append("g").attr("class","axis").attr("transform",`translate(${M.l},0)`).call(d3.axisLeft(y).ticks(5));
  s.append("path").datum(d).attr("fill","none").attr("stroke","var(--warn)").attr("stroke-width",2.5)
    .attr("d",d3.line().x(r=>x(r.yr)).y(r=>y(r.v)));
  s.selectAll("circle").data(d).join("circle").attr("cx",r=>x(r.yr)).attr("cy",r=>y(r.v)).attr("r",3.5).attr("fill","var(--warn)")
    .on("mousemove",(e,r)=>show(`<b>${r.yr}</b><br>${r.v} spm`,e)).on("mouseleave",hide);
})();

// ---- Resting HR + HRV ----
(function(){
  const rhr = DATA.rhr, hrv = DATA.hrv; if(!rhr.length) return;
  const s = svg("#hr");
  const x = d3.scalePoint().domain(rhr.map(r=>r.q)).range([M.l,W-M.r]).padding(.4);
  const yl = d3.scaleLinear().domain([50,65]).range([H-M.b,M.t]);
  const yr = d3.scaleLinear().domain([30,55]).range([H-M.b,M.t]);
  yGrid(s,yl);
  s.append("g").attr("class","axis").attr("transform",`translate(0,${H-M.b})`)
    .call(d3.axisBottom(x).tickValues(rhr.filter((r,i)=>i%3===0).map(r=>r.q)));
  s.append("g").attr("class","axis").attr("transform",`translate(${M.l},0)`).call(d3.axisLeft(yl).ticks(4));
  s.append("g").attr("class","axis").attr("transform",`translate(${W-M.r},0)`).call(d3.axisRight(yr).ticks(4));
  s.append("path").datum(rhr).attr("fill","none").attr("stroke","var(--bad)").attr("stroke-width",2)
    .attr("d",d3.line().x(r=>x(r.q)).y(r=>yl(r.v)));
  s.append("path").datum(hrv).attr("fill","none").attr("stroke","var(--good)").attr("stroke-width",2)
    .attr("d",d3.line().x(r=>x(r.q)).y(r=>yr(r.v)));
  s.append("text").attr("x",M.l).attr("y",M.t-2).attr("fill","var(--bad)").attr("font-size","11").text("resting HR (L)");
  s.append("text").attr("x",W-M.r).attr("y",M.t-2).attr("text-anchor","end").attr("fill","var(--good)").attr("font-size","11").text("HRV (R)");
})();

// ---- Mix table ----
(function(){
  const d = DATA.mix; if(!d.length) return;
  const t = d3.select("#mix").append("table").attr("class","mini");
  t.selectAll("tr").data(d).join("tr").html(r=>`<td>${r.activity}</td><td>${r.hrs} h · ${r.n}×</td>`);
})();
</script>
</body>
</html>
"""
