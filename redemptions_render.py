"""Redemption report — every points redemption made, hotels and flights.

One log across every programme rather than one per brand, because the
question worth answering is not "how did Accor do" but "where do my points
actually go furthest". Programmes and point currencies are read from the
data itself, so recording a new one is just typing its name — nothing here
is hardcoded to a fixed list of brands.

The model, derived from Rohan's own spreadsheet:

    saved     = revenue avoided - cash actually paid
    per point = (saved - rupee certificates) / (points + point certificates)

Certificates come in two denominations and are handled differently. A
Marriott Free Night Award is worth a fixed 15,000 POINTS, so it joins the
pool the rate is divided by. An Amex voucher is worth fixed RUPEES, so its
value is carved out before dividing. Treating either one like the other
would quietly distort every rate it touches.
"""
import datetime as dt
import html

from render import PAGE_CSS, fmt_inr

TYPES = (("hotel", "Hotels"), ("flight", "Flights"))


def enrich(e):
    """Derive saved / per-point for one redemption.

    A certificate comes in one of two denominations and they behave
    differently. A Marriott Free Night Award is worth a fixed number of
    POINTS (15,000), so it joins the pool the rate is measured across. An
    Amex voucher is worth a fixed number of RUPEES, which buys none of the
    stay with points, so it is carved out of the value first. Counting
    either one the other way would quietly distort the rate.
    """
    pts = int(e.get("points") or 0)
    cert_pts = int(e.get("cert_points") or 0)
    revenue = float(e.get("revenue_inr") or 0)
    cash = float(e.get("cash_paid_inr") or 0)
    cert_inr = float(e.get("cert_value_inr") or 0)
    saved = revenue - cash
    value_points = pts + cert_pts
    ppv = ((saved - cert_inr) / value_points) if value_points else None
    date = e.get("date") or "1970-01-01"
    return {**e, "points": pts, "cert_points": cert_pts,
            "value_points": value_points, "revenue_inr": revenue,
            "cash_paid_inr": cash, "cert_value_inr": cert_inr,
            "saved_inr": saved, "ppv": ppv,
            "d": dt.date.fromisoformat(date), "year": date[:4]}


def _totals(rows):
    return {
        "saved": sum(r["saved_inr"] for r in rows),
        "revenue": sum(r["revenue_inr"] for r in rows),
        "cash": sum(r["cash_paid_inr"] for r in rows),
        "cert": sum(r["cert_value_inr"] for r in rows),
        "cert_points": sum(r["cert_points"] for r in rows),
        "points": sum(r["points"] for r in rows),
        "value_points": sum(r["value_points"] for r in rows),
        "nights": sum(int(r.get("nights") or 0) for r in rows),
        "count": len(rows)}


def _avg_ppv(rows):
    """Weighted, not a mean of rates — a 121,000-point redemption should not
    count the same as a 3,000-point one."""
    pts = sum(r["value_points"] for r in rows)
    if not pts:
        return None
    worth = sum(r["saved_inr"] - r["cert_value_inr"]
                for r in rows if r["value_points"])
    return worth / pts


def _rollup(rows, key, targets=None):
    """Group by programme or by point currency, best value first."""
    groups = {}
    for r in rows:
        groups.setdefault(r.get(key) or "—", []).append(r)
    out = []
    for name, rs in groups.items():
        t = _totals(rs)
        out.append({"name": name, "n": t["count"], "saved": t["saved"],
                    "points": t["points"], "ppv": _avg_ppv(rs),
                    "target": (targets or {}).get(name)})
    return sorted(out, key=lambda g: g["saved"], reverse=True)


def _rollup_html(title, groups, show_target=False):
    rows = ""
    for g in groups:
        rate = ""
        if g["ppv"] is not None:
            cls = ""
            if show_target and g["target"]:
                cls = " drop" if g["ppv"] >= g["target"] else " up"
            rate = (f'<span class="rmrate{cls}">₹{g["ppv"]:,.2f}/pt</span>')
        rows += (f'<div class="covrow"><span class="covlab">{g["name"]}'
                 f' <small>· {g["n"]}</small></span>'
                 f'<span class="covval"><b>{fmt_inr(g["saved"])}</b>'
                 f'<em>{rate}</em></span></div>')
    return (f'<div class="pointsbar"><div class="ptslabel">{title}</div>'
            f'{rows}</div>')


def trip_for(date_str, trips):
    """The trip whose dates contain this redemption, if any. ISO dates
    compare correctly as plain strings, so no parsing is needed."""
    if not date_str:
        return None
    for t in trips:
        if (t.get("dateFrom") or "") <= date_str <= (t.get("dateTo") or ""):
            return t
    return None


def _table(rows, interactive, trips=()):
    if not rows:
        return '<p class="intro">Nothing recorded here yet.</p>'
    body = ""
    for r in rows:
        # The points cell has to cover three different shapes without ever
        # naming the same thing twice: points alone, points plus a separate
        # certificate, and a voucher that IS the whole payment.
        paid_with = (r.get("paid_with") or "").strip()
        cert_label = (r.get("cert_label") or "").strip()
        cur = f'<small class="rmdim"> {paid_with}</small>' if paid_with else ""
        # every line in this column reads amount first, dim label after, so
        # the figures stay aligned down the right-aligned column
        if r["points"] or r["cert_points"]:
            label = cert_label or "certificate"
            if label.casefold() == paid_with.casefold():
                label = "certificate"   # the currency is already shown above
            pts = f'{r["points"]:,}{cur}' if r["points"] else "—"
            if r["cert_points"]:
                pts += (f'<div>+ {r["cert_points"]:,}'
                        f'<small class="rmdim"> pts {label}</small></div>')
            if r["cert_value_inr"]:
                pts += (f'<div>+ {fmt_inr(r["cert_value_inr"])}'
                        f'<small class="rmdim"> {label}</small></div>')
        elif r["cert_value_inr"]:
            # no points spent — the voucher or certificate is the payment,
            # so name it once instead of as a currency plus an extra line
            label = cert_label or paid_with or "certificate"
            pts = (f'{fmt_inr(r["cert_value_inr"])}'
                   f'<small class="rmdim"> {label}</small>')
        else:
            pts = f'—{cur}'
        cur = cert = ""   # both are folded into `pts` above
        # the date is what links a redemption to a trip, so it is editable
        # right here rather than buried in the edit form
        t = trip_for(r.get("date"), trips)
        if interactive:
            datecell = (f'<input type="date" class="dinline" '
                        f'value="{r.get("date", "")}" '
                        f'onchange="setDate(\'{r["id"]}\',this.value)">')
        else:
            datecell = r.get("date", "—")
        datecell += (f'<div class="rmdim triphit">{html.escape(t["name"][:24])}'
                     f'</div>' if t else
                     '<div class="rmdim tripmiss">no trip</div>')
        ppv = f'₹{r["ppv"]:,.2f}' if r["ppv"] is not None else "—"
        cash = fmt_inr(r["cash_paid_inr"]) if r["cash_paid_inr"] else "—"
        sub = []
        if int(r.get("nights") or 0):
            sub.append(f'{r["nights"]}N')
        if r.get("detail"):
            sub.append(r["detail"])
        subtitle = (f'<div class="rmdim">{" · ".join(sub)}</div>'
                    if sub else "")
        # a note means the figure was inferred while importing, not taken
        # from a confirmation — say so out loud rather than hiding it in a
        # hover tooltip, which is invisible on a phone
        note = (f'<button class="rmnote" onclick="showNote(\'{r["id"]}\')"'
                f' title="why is this flagged?">⚠︎ estimated</button>'
                if r.get("notes") else "")
        note_box = (f'<div class="rmnotebox" id="note-{r["id"]}" hidden>'
                    f'{html.escape(r["notes"])}</div>'
                    if r.get("notes") else "")
        acts = ""
        if interactive:
            acts = (f'<div class="rmacts">'
                    f'<button class="link" onclick="editR(\'{r["id"]}\')">'
                    f'edit</button>'
                    f'<button class="link" onclick="delR(\'{r["id"]}\')">'
                    f'remove</button></div>')
        body += f"""<tr data-id="{r["id"]}">
  <td><b>{r["name"]}</b>{subtitle}{note_box}{acts}</td>
  <td>{r.get("program") or "—"}</td>
  <td class="num">{pts}{cur}{cert}</td>
  <td class="num">{fmt_inr(r["revenue_inr"])}</td>
  <td class="num">{cash}</td>
  <td class="num"><b>{fmt_inr(r["saved_inr"])}</b></td>
  <td class="num">{ppv}{note}</td>
  <td class="datecol">{datecell}</td>
</tr>"""
    flagged = sum(1 for r in rows if r.get("notes"))
    legend = (f'<p class="rmlegend"><b>⚠︎ estimated</b> — {flagged} row(s) '
              f'hold a figure that was inferred when your notes were '
              f'imported, not read off a confirmation. Tap the marker for '
              f'the reason; editing the row clears it.</p>'
              if flagged else "")
    return f"""<div class="rmwrap"><table class="rmtable">
<thead><tr><th>Redemption</th><th>Programme</th><th class="num">Points</th>
<th class="num">Value</th><th class="num">Cash paid</th>
<th class="num">Saved</th><th class="num">₹/pt</th><th>Date · trip</th>
</tr></thead><tbody>{body}</tbody></table>{legend}</div>"""


def _pane(rows, interactive, trips=()):
    t = _totals(rows)
    avg = _avg_ppv(rows)
    bits = [f'<b>{fmt_inr(t["saved"])}</b> saved',
            f'<b>{t["count"]}</b> redemption(s)']
    if t["nights"]:
        bits.append(f'<b>{t["nights"]}</b> night(s)')
    if t["points"]:
        bits.append(f'<b>{t["points"]:,}</b> points')
    if avg is not None:
        bits.append(f'<b>₹{avg:,.2f}</b> per point')
    strip = ('<div class="staytotals">'
             + "".join(f"<span>{b}</span>" for b in bits) + "</div>")
    return strip + _table(rows, interactive, trips)


def _form(entry=None, entries=None):
    """One form used for both add and edit. The programme and currency
    fields are free text backed by a datalist built from what has already
    been recorded — so existing names autocomplete, and a brand new
    programme needs no code change."""
    e = entry or {}
    progs = sorted({x.get("program") for x in (entries or []) if x.get("program")})
    curs = sorted({x.get("paid_with") for x in (entries or []) if x.get("paid_with")})
    opts = lambda vals: "".join(f'<option value="{v}">' for v in vals)
    is_edit = bool(entry)
    hid = (f'<input type="hidden" name="id" value="{e.get("id","")}">'
           if is_edit else "")
    sel = lambda v, want: " selected" if v == want else ""
    return f"""
<form onsubmit="return saveR(event,{'true' if is_edit else 'false'})">
  {hid}
  <label>What was it<input name="name" required
    value="{e.get("name","")}" placeholder="Aloft Singapore, or BOM → AMS"></label>
  <label>Type<select name="type">
    <option value="hotel"{sel(e.get("type"),"hotel")}>Hotel</option>
    <option value="flight"{sel(e.get("type"),"flight")}>Flight</option>
  </select></label>
  <label>Programme redeemed in
    <input name="program" list="rm-progs" value="{e.get("program","")}"
     placeholder="Marriott, Accor, Taj, Virgin Atlantic…"></label>
  <datalist id="rm-progs">{opts(progs)}</datalist>
  <label>Points came from
    <input name="paid_with" list="rm-curs" value="{e.get("paid_with","")}"
     placeholder="Amex MR, ALL, Bonvoy, voucher…"></label>
  <datalist id="rm-curs">{opts(curs)}</datalist>
  <label>Points used (0 if none)
    <input type="number" name="points" min="0" value="{e.get("points",0)}"></label>
  <label>Certificate used — what it was
    <input name="cert_label" value="{e.get("cert_label","")}"
     placeholder="FNA, Amex voucher…"></label>
  <label>Certificate worth in points (e.g. 15000 for a Marriott FNA)
    <input type="number" name="cert_points" min="0"
     value="{e.get("cert_points",0)}"></label>
  <label>…or its value in ₹ (for a cash voucher — leave 0 if you used points)
    <input type="number" step="0.01" name="cert_value_inr"
     value="{e.get("cert_value_inr",0)}"></label>
  <label>Cash value you avoided (₹)
    <input type="number" step="0.01" name="revenue_inr" required
     value="{e.get("revenue_inr",0)}"></label>
  <label>Cash you actually paid (₹ — taxes, surcharges, co-pay)
    <input type="number" step="0.01" name="cash_paid_inr"
     value="{e.get("cash_paid_inr",0)}"></label>
  <label>Date<input type="date" name="date" value="{e.get("date","")}"></label>
  <label>Nights (0 for flights)
    <input type="number" name="nights" min="0" value="{e.get("nights",0)}"></label>
  <label>Detail / note<input name="detail" value="{e.get("detail","")}"
    placeholder="cabin, route, anything worth remembering"></label>
  <button type="submit" class="primary">
    {"Save changes" if is_edit else "Add redemption"}</button>
  {'<button type="button" class="link" onclick="closeEdit()">cancel</button>'
   if is_edit else ''}
</form>"""


def render_redemptions(data, nav="", interactive=False, trips=()):
    raw = data.get("entries", [])
    targets = data.get("currency_targets", {})
    rows = sorted((enrich(e) for e in raw), key=lambda r: r["d"], reverse=True)

    hotels = [r for r in rows if r.get("type") == "hotel"]
    flights = [r for r in rows if r.get("type") == "flight"]
    tot = _totals(rows)
    avg = _avg_ppv(rows)

    years = sorted({r["year"] for r in rows}, reverse=True)
    year_rows = ""
    for y in years:
        yr = [r for r in rows if r["year"] == y]
        t = _totals(yr)
        year_rows += (f'<div class="covrow"><span class="covlab">{y}'
                      f' <small>· {t["count"]}</small></span>'
                      f'<span class="covval"><b>{fmt_inr(t["saved"])}</b>'
                      f'<em></em></span></div>')

    sidebar = f"""
<div class="pointsbar">
  <div class="ptshero">
    <div class="ptslabel">Saved by redeeming</div>
    <div class="ptsbig">{fmt_inr(tot["saved"])}</div>
    <div class="ptsworth">{tot["count"]} redemption(s) ·
      {tot["points"]:,} points · {tot["nights"]} night(s)</div>
  </div>
  <div class="covrow"><span class="covlab">hotels</span>
    <span class="covval"><b>{fmt_inr(_totals(hotels)["saved"])}</b>
    <em>{len(hotels)}</em></span></div>
  <div class="covrow"><span class="covlab">flights</span>
    <span class="covval"><b>{fmt_inr(_totals(flights)["saved"])}</b>
    <em>{len(flights)}</em></span></div>
  <div class="covrow"><span class="covlab">cash you still paid</span>
    <span class="covval"><b>{fmt_inr(tot["cash"])}</b><em></em></span></div>
  {f'<div class="covrow"><span class="covlab">certificates used</span>'
   f'<span class="covval"><b>'
   f'{f"{tot["cert_points"]:,} pts" if tot["cert_points"] else ""}'
   f'{" + " if tot["cert_points"] and tot["cert"] else ""}'
   f'{fmt_inr(tot["cert"]) if tot["cert"] else ""}'
   f'</b><em></em></span></div>' if tot["cert_points"] or tot["cert"] else ""}
  {f'<div class="covrow"><span class="covlab">average per point</span>'
   f'<span class="covval"><b>₹{avg:,.2f}</b><em></em></span></div>'
   if avg is not None else ""}
</div>
{_rollup_html("By point currency", _rollup(rows, "paid_with", targets), True)}
{_rollup_html("By programme", _rollup(rows, "program"))}
<div class="pointsbar"><div class="ptslabel">By year</div>{year_rows}</div>
<p class="intro">Value per point is measured after carving out certificates
and the cash you paid, so a Free Night Award or an Amex voucher never
flatters the rate your points actually earned.</p>"""

    controls = ""
    script = ""
    if not interactive:
        # the published copy: same figures and the same three views, with
        # nothing that writes. Switching panes is pure client-side toggling,
        # so it works on a static page.
        controls = f"""
<div class="subtabs" role="tablist">
  <button class="on" onclick="showPaneR(this,'all')">All</button>
  <button onclick="showPaneR(this,'hotel')">Hotels</button>
  <button onclick="showPaneR(this,'flight')">Flights</button>
</div>
<div class="pane on" id="rpane-all">{_pane(rows, False, trips)}</div>
<div class="pane" id="rpane-hotel">{_pane(hotels, False, trips)}</div>
<div class="pane" id="rpane-flight">{_pane(flights, False, trips)}</div>"""
        script = """<script>
function showPaneR(btn,name){
  document.querySelectorAll('.subtabs button').forEach(function(b){
    b.classList.toggle('on', b===btn); });
  ['all','hotel','flight'].forEach(function(n){
    var el=document.getElementById('rpane-'+n);
    if(el) el.classList.toggle('on', n===name); });
}
</script>"""
    if interactive:
        controls = f"""
<div class="subtabs" role="tablist">
  <button class="on" onclick="showPaneR(this,'all')">All</button>
  <button onclick="showPaneR(this,'hotel')">Hotels</button>
  <button onclick="showPaneR(this,'flight')">Flights</button>
  <button onclick="showPaneR(this,'add')">Add a redemption</button>
</div>
<div class="pane on" id="rpane-all">{_pane(rows, True, trips)}</div>
<div class="pane" id="rpane-hotel">{_pane(hotels, True, trips)}</div>
<div class="pane" id="rpane-flight">{_pane(flights, True, trips)}</div>
<div class="pane" id="rpane-add">{_form(entries=raw)}</div>
<div id="editbox" class="editbox" hidden></div>"""
        forms = {e["id"]: _form(e, raw) for e in raw}
        import json as _json
        script = f"""<script>
const FORMS = {_json.dumps(forms)};
async function post(u,d){{const r=await fetch(u,{{method:'POST',
  headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}});
  if(!r.ok){{alert((await r.json()).error||'failed');return null;}}return r.json();}}
async function saveR(e,isEdit){{e.preventDefault();
  const f=new FormData(e.target);const d={{}};f.forEach((v,k)=>d[k]=v);
  const url=isEdit?'/api/redemptions/update':'/api/redemptions';
  if(await post(url,d)) location.reload(); return false;}}
function editR(id){{
  const box=document.getElementById('editbox');
  box.innerHTML='<h3 class="rmedithead">Editing</h3>'+FORMS[id];
  box.hidden=false; box.scrollIntoView({{block:'center'}});}}
function closeEdit(){{document.getElementById('editbox').hidden=true;}}
function showNote(id){{
  var b=document.getElementById('note-'+id);
  if(b) b.hidden=!b.hidden;}}
async function setDate(id,v){{
  if(!v) return;
  if(await post('/api/redemptions/date',{{id:id,date:v}})) location.reload();}}
async function delR(id){{if(!confirm('Remove this redemption?'))return;
  if(await post('/api/redemptions/delete',{{id:id}})) location.reload();}}
function showPaneR(btn,name){{
  document.querySelectorAll('.subtabs button').forEach(function(b){{
    b.classList.toggle('on', b===btn); }});
  ['all','hotel','flight','add'].forEach(function(n){{
    var el=document.getElementById('rpane-'+n);
    if(el) el.classList.toggle('on', n===name); }});
  closeEdit();
  try{{ sessionStorage.setItem('rmPane', name); }}catch(e){{}}
}}
document.addEventListener('DOMContentLoaded',function(){{
  var want='all';
  try{{ want=sessionStorage.getItem('rmPane')||'all'; }}catch(e){{}}
  var btns=document.querySelectorAll('.subtabs button');
  var idx={{all:0,hotel:1,flight:2,add:3}}[want]||0;
  if(btns[idx]) showPaneR(btns[idx],want);
}});
</script>"""

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Points Ledger — Redemptions</title>
<style>{PAGE_CSS}
.rmwrap {{ overflow-x:auto; margin-top:.2rem; }}
.rmtable {{ border-collapse:collapse; width:100%; font-size:.9rem; }}
.rmtable th, .rmtable td {{ text-align:left; padding:.55rem .7rem;
  border-bottom:1px solid var(--line); vertical-align:top; }}
.rmtable th {{ color:var(--muted); font-weight:600; font-size:.75rem;
  text-transform:uppercase; letter-spacing:.05em; white-space:nowrap; }}
.rmtable td.num, .rmtable th.num {{ text-align:right;
  font-variant-numeric:tabular-nums; white-space:nowrap; }}
.rmtable tbody tr:hover {{ background:var(--box); }}
.rmdim {{ color:var(--muted); font-size:.78rem; font-weight:400; }}
.datecol {{ white-space:nowrap; }}
.dinline {{ padding:.2rem .35rem; border:1px solid var(--line);
  border-radius:6px; background:var(--bg); color:var(--fg);
  font-size:.78rem; font-family:inherit; }}
.dinline:hover {{ border-color:var(--fg); }}
.triphit {{ color:var(--drop); }}
.tripmiss {{ opacity:.6; }}
.rmnote {{ color:var(--up); font-size:.7rem; cursor:pointer; background:none;
  border:none; padding:0; font-family:inherit; text-align:right;
  display:block; margin-left:auto; }}
.rmnote:hover {{ text-decoration:underline; }}
.rmnotebox {{ margin-top:.3rem; padding:.4rem .55rem; border-radius:7px;
  background:var(--box); border:1px solid var(--line); color:var(--muted);
  font-size:.76rem; font-weight:400; white-space:normal; max-width:34ch; }}
.rmnotebox[hidden] {{ display:none; }}
.rmlegend {{ color:var(--muted); font-size:.78rem; margin:.7rem 0 0;
  max-width:70ch; }}
.rmacts {{ display:flex; gap:.6rem; margin-top:.25rem; }}
.rmacts button {{ font-size:.78rem; }}
.rmrate {{ font-variant-numeric:tabular-nums; }}
.rmrate.drop {{ color:var(--drop); }}
.rmrate.up {{ color:var(--up); }}
.editbox {{ margin-top:1rem; padding:1rem 1.1rem; border-radius:12px;
  background:var(--card); border:1px solid var(--line); }}
.editbox[hidden] {{ display:none; }}
.rmedithead {{ margin:0 0 .5rem; font-size:.95rem; }}
.pane > form, .editbox form {{ display:grid; gap:.6rem; max-width:460px;
  margin-top:.8rem; padding:1rem 1.1rem; border:1px solid var(--line);
  border-radius:12px; background:var(--card); }}
.editbox form {{ border:none; padding:0; background:none; }}
.pane > form label, .editbox form label {{ display:grid; gap:.18rem;
  font-size:.88em; }}
.pane > form input, .pane > form select,
.editbox form input, .editbox form select {{ padding:.45rem .55rem;
  border:1px solid var(--line); border-radius:7px; background:var(--bg);
  color:var(--fg); font-size:1em; }}
</style></head><body>
<div class="brand">Points Ledger</div>
{nav}<h1>Redemption report</h1>
<div class="hmeta">Every points redemption — hotels and flights, across
every programme</div>
<div class="layout">
<aside class="side">{sidebar}</aside>
<main class="maincol">{controls}</main>
</div>
{script}
</body></html>"""
