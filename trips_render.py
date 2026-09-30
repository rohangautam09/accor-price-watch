"""Trip spending — what every trip actually cost, by category.

Kept separate from the redemption log on purpose: that one answers "what
were my points worth", this one answers "what does a trip cost me". A stay
paid with points appears in both, but as different facts — points spent
there, rupees spent here.

Categories are not a fixed list. They are read from whatever has been
entered, so adding "Trains" or "Gifts" to one trip needs no code change and
shows up in the rollups automatically.
"""
import datetime as dt
import html
import re

from render import PAGE_CSS, fmt_inr

# stable colours for the categories seen so far; anything new falls back to
# a hue derived from its name, so the palette never runs out
CAT_HUE = {
    "Stays": 205, "Flights": 265, "Eating out": 25, "Commute": 155,
    "Activities": 330, "Shopping": 45, "Souvenirs": 85,
    "Visa": 0, "Travel insurance": 120,
    "Roaming": 185, "Forex": 300, "Other": 220,
    "Not broken down": 240,
}

# offered in every form even before first use, so a category does not have
# to be typed from scratch on a new trip. Anything else you enter is kept
# and joins this list automatically.
DEFAULT_CATS = ("Stays", "Flights", "Commute", "Eating out", "Activities",
                "Shopping", "Souvenirs", "Visa", "Travel insurance",
                "Roaming", "Forex", "Other")


def cat_colour(name):
    hue = CAT_HUE.get(name)
    if hue is None:
        hue = sum(ord(c) * 7 for c in name) % 360
    return f"hsl({hue} 62% 58%)"


def enrich(t, today=None):
    """Totals, per-day cost, and whether the trip has happened yet."""
    today = today or dt.date.today()
    exp = {k: float(v) for k, v in (t.get("expenses") or {}).items() if v}
    from_cats = sum(exp.values())
    stated = t.get("total_stated_inr")
    stated = float(stated) if stated not in (None, "") else None
    # a trip with no breakdown still has a total; one with a breakdown is
    # trusted to its categories, and any gap against the stated figure is
    # surfaced rather than quietly absorbed
    total = from_cats if exp else (stated or 0)
    gap = (stated - from_cats) if (stated is not None and exp) else 0
    d1 = dt.date.fromisoformat(t["dateFrom"])
    d2 = dt.date.fromisoformat(t["dateTo"])
    nights = (d2 - d1).days
    days = nights + 1
    return {**t, "expenses": exp, "total_inr": total, "gap_inr": gap,
            "stated_inr": stated, "d1": d1, "d2": d2, "nights": nights,
            "days": days, "per_day": (total / days) if days else 0,
            "upcoming": d2 >= today,
            # a trip can be kept as a record but left out of the running
            # totals — an unoptimised one would otherwise skew every average
            "excluded": bool(t.get("exclude_from_totals")),
            # a night away at home base — still a trip, but worth telling
            # apart from one you travelled for
            "staycation": bool(t.get("staycation")),
            "reds": [], "saved_points_inr": 0.0}


MONTHS = ("Jan", "Feb", "Mar", "Apr", "May", "Jun",
          "Jul", "Aug", "Sep", "Oct", "Nov", "Dec")


def compact(n):
    """₹1,23,456 is too wide for a column label; ₹1.2L is not."""
    if n >= 1e5:
        return f"₹{n / 1e5:,.1f}L"
    if n >= 1000:
        return f"₹{n / 1000:,.0f}k"
    return f"₹{n:,.0f}" if n else ""


def monthly_split(rows):
    """Spend per calendar month, spread across the months a trip actually
    covers rather than dumped on its start month — Singapore ran 30 Apr to
    6 May, so one day of it belongs to April and six to May."""
    spent, ahead = {}, {}
    for r in rows:
        if r["excluded"]:
            continue
        span = (r["d2"] - r["d1"]).days + 1
        if not span:
            continue
        per_day = r["total_inr"] / span
        target = ahead if r["upcoming"] else spent
        d = r["d1"]
        while d <= r["d2"]:
            k = (d.year, d.month)
            target[k] = target.get(k, 0) + per_day
            d += dt.timedelta(days=1)
    return spent, ahead


def _monthly_html(rows):
    spent, ahead = monthly_split(rows)
    if not spent and not ahead:
        return '<p class="intro">No trips recorded yet.</p>'
    years = sorted({y for y, _ in list(spent) + list(ahead)}, reverse=True)
    peak = max((spent.get(k, 0) + ahead.get(k, 0)
                for k in set(spent) | set(ahead)), default=0) or 1

    out = ""
    for y in years:
        y_spent = sum(v for (yy, _), v in spent.items() if yy == y)
        y_ahead = sum(v for (yy, _), v in ahead.items() if yy == y)
        cols = ""
        for m in range(1, 13):
            sv = spent.get((y, m), 0)
            av = ahead.get((y, m), 0)
            tot = sv + av
            bars = ""
            if av:
                bars += (f'<div class="mbar ahead" '
                         f'style="height:{av / peak * 100:.2f}%"></div>')
            if sv:
                bars += (f'<div class="mbar spent" '
                         f'style="height:{sv / peak * 100:.2f}%"></div>')
            cols += (f'<div class="mcol{" on" if tot else ""}" '
                     f'title="{MONTHS[m - 1]} {y}: {fmt_inr(tot)}">'
                     f'<div class="mamt">{compact(tot)}</div>'
                     f'<div class="mbarwrap">{bars}</div>'
                     f'<div class="mlab">{MONTHS[m - 1][0]}</div></div>')
        # what a year of travel works out to per month, spread evenly —
        # the figure to set aside monthly rather than where it landed
        y_total = y_spent + y_ahead
        ahead_bit = (f' + {fmt_inr(y_ahead)} ahead' if y_ahead else "")
        out += f"""<div class="myear">
  <div class="myhead">
    <div><b>{y}</b>
      <div class="mysub">{fmt_inr(y_spent)}{" spent" if y_spent else ""}
        {ahead_bit}</div></div>
    <div class="mypm"><b>{fmt_inr(y_total / 12)}</b>
      <span>per month</span></div>
  </div>
  <div class="mgrid">{cols}</div>
</div>"""
    return (out + '<p class="intro">The per-month figure is that year\'s '
            'whole travel bill divided by twelve — what it costs you to '
            'travel that much, month in month out, whatever the bars say. '
            'A trip spanning two months is split across them by how many '
            'of its days fall in each; paler bars are trips still '
            'ahead.</p>')


def attach_redemptions(rows, redemptions):
    """File each redemption under the trip whose dates surround it.

    Dates are the only reliable link — a redemption knows nothing about
    trips. Anything that lands outside every trip is handed back so it can
    be shown rather than silently dropped, which is usually a sign that one
    of the two dates needs correcting.
    """
    unmatched = []
    for e in redemptions:
        try:
            d = dt.date.fromisoformat(e.get("date") or "")
        except ValueError:
            continue
        hit = next((r for r in rows if r["d1"] <= d <= r["d2"]), None)
        if hit is None:
            unmatched.append(e)
        else:
            hit["reds"].append(e)
    for r in rows:
        r["saved_points_inr"] = sum(
            float(x.get("revenue_inr") or 0) - float(x.get("cash_paid_inr") or 0)
            for x in r["reds"])
    return unmatched


def _totals(rows):
    rows = [r for r in rows if not r["excluded"]]
    cats = {}
    for r in rows:
        for k, v in r["expenses"].items():
            cats[k] = cats.get(k, 0) + v
        # a trip logged as a lump sum still spent money — show it as its own
        # slice so the shares add to the total instead of quietly missing
        if not r["expenses"] and r["total_inr"]:
            cats["Not broken down"] = (cats.get("Not broken down", 0)
                                       + r["total_inr"])
    return {"total": sum(r["total_inr"] for r in rows),
            "nights": sum(r["nights"] for r in rows),
            "days": sum(r["days"] for r in rows),
            "saved": sum(r["saved_points_inr"] for r in rows),
            "count": len(rows), "cats": cats}


def _bar(exp, total):
    """One stacked bar showing where a trip's money went."""
    if not total:
        return ""
    seg = ""
    for name, amt in sorted(exp.items(), key=lambda kv: -kv[1]):
        pct = amt / total * 100
        seg += (f'<span style="width:{pct:.3f}%;background:{cat_colour(name)}"'
                f' title="{html.escape(name)} {fmt_inr(amt)} · {pct:.0f}%">'
                f'</span>')
    return f'<div class="tbar">{seg}</div>'


def _slug(name):
    return re.sub(r"[^a-z0-9]+", "-", name.lower()).strip("-") or "x"


def _contributors(name, rows):
    """Which trips make up one category, biggest first. A trip logged as a
    lump sum has no categories, so it is credited to 'Not broken down'."""
    out = []
    for r in rows:
        amt = (r["total_inr"] if (name == "Not broken down"
                                  and not r["expenses"])
               else r["expenses"].get(name, 0))
        if amt:
            out.append((r, amt))
    return sorted(out, key=lambda x: -x[1])


def _cat_rows(exp, total, drill=None):
    """drill: the trips behind these figures. Given them, each category
    becomes clickable and opens its own breakdown."""
    out = ""
    for name, amt in sorted(exp.items(), key=lambda kv: -kv[1]):
        pct = (amt / total * 100) if total else 0
        sid = _slug(name)
        click = (f' catrow" onclick="toggleCat(\'{sid}\')' if drill is not None
                 else '')
        out += (f'<div class="covrow{click}"><span class="covlab">'
                f'<i class="tdot" style="background:{cat_colour(name)}"></i>'
                f'{name}</span><span class="covval"><b>{fmt_inr(amt)}</b>'
                f'<em>{pct:.0f}%</em></span></div>')
        if drill is not None:
            detail = ""
            for r, a in _contributors(name, drill):
                detail += (f'<div class="covrow sub"><span class="covlab">'
                           f'{html.escape(r["name"][:22])}'
                           f' <small>{r["d1"]:%b %Y}</small></span>'
                           f'<span class="covval"><b>{fmt_inr(a)}</b>'
                           f'<em>{a / amt * 100:.0f}%</em></span></div>')
            out += (f'<div class="catdetail" id="cat-{sid}" hidden>'
                    f'{detail or "<div class=covrow sub>no trips</div>"}'
                    f'</div>')
    return out


def _trip_card(r, interactive):
    tag = ('<span class="stag track">upcoming</span>' if r["upcoming"]
           else '<span class="stag booked">done</span>')
    if not r["expenses"]:
        tag += '<span class="stag cash">approx</span>'
    if r["staycation"]:
        tag += '<span class="stag ptag">staycation</span>'
    if r["excluded"]:
        tag += ('<span class="stag notcounted" title="kept as a record but '
                'left out of every total">not counted</span>')
    note = ""
    if r.get("notes"):
        note = (f'<button class="rmnote" onclick="showTNote(\'{r["id"]}\')">'
                f'⚠︎ check</button>'
                f'<div class="rmnotebox" id="tnote-{r["id"]}" hidden>'
                f'{html.escape(r["notes"])}</div>')
    acts = ""
    if interactive:
        acts = (f'<div class="rmacts">'
                f'<button class="link" onclick="editT(\'{r["id"]}\')">edit'
                f'</button><button class="link" onclick="delT(\'{r["id"]}\')">'
                f'remove</button></div>')
    # redemptions dated inside this trip: money points kept in your pocket
    saved_box = ""
    if r["saved_points_inr"]:
        lines = ""
        for e in sorted(r["reds"], key=lambda x: x.get("date", "")):
            amt = (float(e.get("revenue_inr") or 0)
                   - float(e.get("cash_paid_inr") or 0))
            pts = (f'{int(e.get("points") or 0):,} {e.get("paid_with") or ""}'
                   if e.get("points") else (e.get("cert_label") or "voucher"))
            rid = e.get("id")
            # a matched figure can be a stand-in (an unpriced award stay, a
            # rate read off an old check), so both halves are editable here
            # rather than only in the redemptions table
            if interactive and rid:
                if e.get("points"):
                    ptscell = (
                        f'<input class="rinl" type="number" min="0" step="1000"'
                        f' value="{int(e["points"])}" title="points used"'
                        f' onchange="setRed(\'{rid}\',\'points\',this.value)">'
                        f' {html.escape(e.get("paid_with") or "")}')
                else:
                    ptscell = html.escape(e.get("cert_label") or "voucher")
                amtcell = (
                    f'<b class="rinlwrap">₹<input class="rinl amt"'
                    f' type="number" step="1" value="{amt:.0f}"'
                    f' title="saved on this redemption"'
                    f' onchange="setRed(\'{rid}\',\'saved_inr\',this.value)">'
                    f'</b>')
            else:
                ptscell, amtcell = html.escape(pts), f'<b>{fmt_inr(amt)}</b>'
            lines += (f'<div class="covrow"><span class="covlab">'
                      f'{html.escape(e.get("name", ""))}'
                      f' <small>· {ptscell}</small></span>'
                      f'<span class="covval">{amtcell}<em></em>'
                      f'</span></div>')
        would = r["total_inr"] + r["saved_points_inr"]
        saved_box = f"""<div class="savedbox">
  <div class="savedhead">Saved with points
    <b>{fmt_inr(r["saved_points_inr"])}</b></div>
  {lines}
  <div class="tiny">would have cost {fmt_inr(would)} without them</div>
</div>"""

    # a zero here means no cash left your pocket, not that nothing was
    # spent — without saying so, a big ₹0 reads like an error
    if not r["total_inr"] and r["saved_points_inr"]:
        cash_sub = "nothing — paid with points"
    else:
        cash_sub = f'{fmt_inr(r["per_day"])} / day'

    gap = ""
    if abs(r["gap_inr"]) > 1:
        gap = (f'<div class="tiny up">₹{abs(r["gap_inr"]):,.2f} '
               f'{"unaccounted for" if r["gap_inr"] > 0 else "over"} '
               f'vs your noted total</div>')
    return f"""<div class="card{" dim" if r["excluded"] else ""}">
  <div class="chead">
    <div class="cinfo">
      <div class="hname">{r["name"]}{tag}</div>
      <div class="hmeta">{r["d1"]:%d %b %Y} → {r["d2"]:%d %b %Y} ·
        {r["nights"]} night(s)</div>
      {note}
    </div>
    <div class="cright">
      <div class="plabel">cash spent</div>
      <div class="tbig">{fmt_inr(r["total_inr"])}</div>
      <div class="tiny">{cash_sub}</div>
      {gap}
    </div>
  </div>
  {_bar(r["expenses"], r["total_inr"])}
  <div class="tcats">{_cat_rows(r["expenses"], r["total_inr"])}</div>
  {saved_box}
  <div class="rowacts">{acts}</div>
</div>"""


def _grouped(rows, interactive, empty="No trips recorded."):
    """Trip cards under a heading per year, so a list of many trips still
    reads as 'what did I do in 2025'. A trip is filed under the year it
    started, even if it ran over into January."""
    if not rows:
        return f'<p class="intro">{empty}</p>'
    out = ""
    for year in sorted({r["d1"].year for r in rows}, reverse=True):
        yr = [r for r in rows if r["d1"].year == year]
        t = _totals(yr)
        out += (f'<div class="yhead"><b>{year}</b>'
                f'<span>{t["count"]} trip(s) · {t["nights"]} night(s) · '
                f'<b>{fmt_inr(t["total"])}</b></span></div>'
                + "".join(_trip_card(r, interactive) for r in yr))
    return out


def _form(t=None, known_cats=()):
    e = t or {}
    exp = e.get("expenses") or {}
    cats = sorted(set(known_cats) | set(exp))
    fields = ""
    for c in cats:
        fields += (
            f'<label>{c}<span class="amtrow">'
            f'<input type="number" step="0.01" name="cat::{c}" '
            f'value="{exp.get(c, "")}">'
            f'<button type="button" class="addbtn" onclick="openAdd(this)" '
            f'title="add an amount to this">+</button>'
            f'<span class="addwrap" hidden>'
            f'<input type="number" step="0.01" class="addbox" '
            f'placeholder="amount to add" '
            f'onkeydown="if(event.key===\'Enter\')'
            f'{{event.preventDefault();doAdd(this);}}'
            f'else if(event.key===\'Escape\')cancelAdd(this);">'
            f'<button type="button" class="addgo" onclick="doAdd(this)">'
            f'add</button>'
            f'<button type="button" class="link addcancel" '
            f'onclick="cancelAdd(this)">cancel</button>'
            f'</span></span></label>')
    hid = (f'<input type="hidden" name="id" value="{e.get("id","")}">'
           if t else "")
    return f"""
<form onsubmit="return saveT(event,{'true' if t else 'false'})">
  {hid}
  <label>Where<input name="name" required value="{e.get("name","")}"
    placeholder="Goa, or Amsterdam · Brugge · Cologne"></label>
  <label>From<input type="date" name="dateFrom" required
    value="{e.get("dateFrom","")}"></label>
  <label>To<input type="date" name="dateTo" required
    value="{e.get("dateTo","")}"></label>
  <fieldset class="tfields"><legend>What it cost</legend>{fields}
    <label>Add a new category<input name="new_cat"
      placeholder="Trains, Gifts, Tips…"></label>
    <label>…and its amount<input type="number" step="0.01"
      name="new_cat_amount"></label>
  </fieldset>
  <label class="tcheck"><input type="checkbox" name="staycation"
    {"checked" if e.get("staycation") else ""}>
    Staycation (a night away without really travelling)</label>
  <label class="tcheck"><input type="checkbox" name="exclude_from_totals"
    {"checked" if e.get("exclude_from_totals") else ""}>
    Leave this trip out of all totals</label>
  <label>Total, if you have no breakdown (₹)
    <input type="number" step="0.01" name="total_stated_inr"
     value="{e.get("total_stated_inr") or ""}"></label>
  <button type="submit" class="primary">
    {"Save changes" if t else "Add trip"}</button>
  {'<button type="button" class="link" onclick="closeTEdit()">cancel</button>'
   if t else ''}
</form>"""


def render_trips(data, nav="", interactive=False, redemptions=()):
    raw = data.get("trips", [])
    rows = sorted((enrich(t) for t in raw), key=lambda r: r["d1"],
                  reverse=True)
    orphans = attach_redemptions(rows, redemptions)
    done = [r for r in rows if not r["upcoming"]]
    ahead = [r for r in rows if r["upcoming"]]
    td, ta = _totals(done), _totals(ahead)
    counted = [r for r in done if not r["excluded"]]
    known = sorted(set(DEFAULT_CATS) | {c for r in rows
                                        for c in r["expenses"]})

    years = {}
    for r in done:
        years.setdefault(r["d1"].year, []).append(r)
    year_rows = ""
    for y in sorted(years, reverse=True):
        t = _totals(years[y])
        year_rows += (f'<div class="covrow"><span class="covlab">{y}'
                      f' <small>· {t["count"]} trip(s)</small></span>'
                      f'<span class="covval"><b>{fmt_inr(t["total"])}</b>'
                      f'<em>{t["nights"]}N</em></span></div>')

    sidebar = f"""
<div class="pointsbar">
  <div class="ptshero">
    <div class="ptslabel">Spent on trips</div>
    <div class="ptsbig">{fmt_inr(td["total"])}</div>
    <div class="ptsworth">{td["count"]} trip(s) · {td["nights"]} night(s) ·
      {fmt_inr(td["total"] / td["days"]) if td["days"] else "–"} / day</div>
  </div>
  {f'<div class="covrow"><span class="covlab">committed on trips ahead'
   f'</span><span class="covval"><b>{fmt_inr(ta["total"])}</b>'
   f'<em>{ta["count"]}</em></span></div>' if ahead else ""}
  {f'<div class="covrow"><span class="covlab">saved with points</span>'
   f'<span class="covval"><b>{fmt_inr(td["saved"])}</b><em></em></span>'
   f'</div>' if td["saved"] else ""}
  {f'<div class="covrow"><span class="covlab">left out of totals</span>'
   f'<span class="covval"><b>{sum(1 for r in rows if r["excluded"])}'
   f' trip(s)</b><em></em></span></div>'
   if any(r["excluded"] for r in rows) else ""}
</div>
<div class="pointsbar"><div class="ptslabel">Where the money goes
  <small>· tap a row</small></div>
  {_bar(td["cats"], td["total"])}
  {_cat_rows(td["cats"], td["total"], drill=counted)}
</div>
<div class="pointsbar"><div class="ptslabel">By year</div>{year_rows}</div>
<p class="intro">Trips you have taken are counted in the total; ones still
ahead are kept separate so they do not inflate what you have actually
spent.</p>"""

    controls = script = ""
    years = sorted({r["d1"].year for r in rows}, reverse=True)
    # years filter within the trip list rather than sitting alongside
    # "By month" and "Add a trip" — they are a different kind of choice
    chips = ('<button data-y="all" class="on" onclick="showYear(this)">'
             'All</button>')
    for y in years:
        chips += (f'<button data-y="{y}" onclick="showYear(this)">'
                  f'{y}</button>')
    ypanes = (f'<div class="ypane on" id="ypane-all">'
              f'{_grouped(rows, interactive)}</div>')
    for y in years:
        yr = [r for r in rows if r["d1"].year == y]
        ypanes += (f'<div class="ypane" id="ypane-{y}">'
                   f'{_grouped(yr, interactive)}</div>')

    if not interactive:
        # the published copy: the same trips, the same two views, nothing
        # that writes. Pane and year switching is client-side only.
        controls = f"""
<div class="subtabs" role="tablist">
  <button data-pane="all" class="on" onclick="showPaneT(this)">All trips</button>
  <button data-pane="month" onclick="showPaneT(this)">By month</button>
</div>
<div class="pane on" id="tpane-all">
  <div class="yearchips">{chips}</div>
  {ypanes}
</div>
<div class="pane" id="tpane-month">{_monthly_html(rows)}</div>"""
        script = """<script>
function showYear(btn){
  document.querySelectorAll('.yearchips button').forEach(function(b){
    b.classList.toggle('on', b===btn); });
  document.querySelectorAll('.ypane').forEach(function(p){
    p.classList.toggle('on', p.id==='ypane-'+btn.dataset.y); });
}
function showPaneT(btn){
  var name=btn.dataset.pane;
  document.querySelectorAll('.subtabs button').forEach(function(b){
    b.classList.toggle('on', b===btn); });
  ['all','month'].forEach(function(n){
    var el=document.getElementById('tpane-'+n);
    if(el) el.classList.toggle('on', n===name); });
}
function toggleCat(id){
  var e=document.getElementById('cat-'+id);
  if(e) e.hidden=!e.hidden;
}
</script>"""

    if interactive:
        import json as _json
        forms = {r["id"]: _form(r, known) for r in rows}
        orphan_note = ""
        if orphans:
            names = ", ".join(html.escape(o.get("name", "?"))
                              for o in orphans[:6])
            more = f" and {len(orphans) - 6} more" if len(orphans) > 6 else ""
            orphan_note = (
                f'<p class="orphan">{len(orphans)} redemption(s) could not '
                f'be matched to a trip because their date falls outside '
                f'every one: <b>{names}{more}</b>. Fix the date on either '
                f'side and the saving will attach itself.</p>')
        controls = f"""
<div class="subtabs" role="tablist">
  <button data-pane="all" class="on" onclick="showPaneT(this)">All trips</button>
  <button data-pane="month" onclick="showPaneT(this)">By month</button>
  <button data-pane="add" onclick="showPaneT(this)">Add a trip</button>
</div>
<div class="pane on" id="tpane-all">
  <div class="yearchips">{chips}</div>
  {orphan_note}
  {ypanes}
</div>
<div class="pane" id="tpane-month">{_monthly_html(rows)}</div>
<div class="pane" id="tpane-add">{_form(known_cats=known)}</div>
<div id="teditbox" class="editbox" hidden></div>"""
        script = f"""<script>
const TFORMS = {_json.dumps(forms)};
function openAdd(btn){{
  var row=btn.closest('.amtrow');
  btn.hidden=true;
  var w=row.querySelector('.addwrap');
  w.hidden=false; w.querySelector('.addbox').focus();
}}
function closeAdd(row){{
  row.querySelector('.addbox').value='';
  row.querySelector('.addwrap').hidden=true;
  row.querySelector('.addbtn').hidden=false;
}}
function doAdd(el){{
  var row=el.closest('.amtrow');
  var add=parseFloat(row.querySelector('.addbox').value);
  var main=row.querySelector('input[name^="cat::"]');
  if(add) main.value=((parseFloat(main.value)||0)+add).toFixed(2);
  closeAdd(row); main.focus();
}}
function cancelAdd(el){{ closeAdd(el.closest('.amtrow')); }}
async function tpost(u,d){{const r=await fetch(u,{{method:'POST',
  headers:{{'Content-Type':'application/json'}},body:JSON.stringify(d)}});
  if(!r.ok){{alert((await r.json()).error||'failed');return null;}}return r.json();}}
async function saveT(e,isEdit){{e.preventDefault();
  const f=new FormData(e.target);const d={{}};f.forEach((v,k)=>d[k]=v);
  const u=isEdit?'/api/trips/update':'/api/trips';
  if(await tpost(u,d)) location.reload(); return false;}}
function editT(id){{const b=document.getElementById('teditbox');
  b.innerHTML='<h3 class="rmedithead">Editing</h3>'+TFORMS[id];
  b.hidden=false; b.scrollIntoView({{block:'center'}});}}
function closeTEdit(){{document.getElementById('teditbox').hidden=true;}}
async function setRed(id,field,v){{
  if(v===''||v===null) return;
  const d={{id:id}}; d[field]=v;
  if(await tpost('/api/redemptions/saved',d)) location.reload();}}
async function delT(id){{if(!confirm('Remove this trip?'))return;
  if(await tpost('/api/trips/delete',{{id:id}})) location.reload();}}
function toggleCat(id){{
  var e=document.getElementById('cat-'+id);
  if(e) e.hidden=!e.hidden;}}
function showTNote(id){{var b=document.getElementById('tnote-'+id);
  if(b) b.hidden=!b.hidden;}}
const TPANES = ["all","month","add"];
function showYear(btn){{
  document.querySelectorAll('.yearchips button').forEach(function(b){{
    b.classList.toggle('on', b===btn); }});
  document.querySelectorAll('.ypane').forEach(function(p){{
    p.classList.toggle('on', p.id==='ypane-'+btn.dataset.y); }});
  closeTEdit();
  try{{ sessionStorage.setItem('tripYear', btn.dataset.y); }}catch(e){{}}
}}
function showPaneT(btn){{
  var name=btn.dataset.pane;
  document.querySelectorAll('.subtabs button').forEach(function(b){{
    b.classList.toggle('on', b===btn); }});
  TPANES.forEach(function(n){{
    var el=document.getElementById('tpane-'+n);
    if(el) el.classList.toggle('on', n===name); }});
  closeTEdit();
  try{{ sessionStorage.setItem('tripPane', name); }}catch(e){{}}
}}
document.addEventListener('DOMContentLoaded',function(){{
  var want='all';
  try{{ want=sessionStorage.getItem('tripPane')||'all'; }}catch(e){{}}
  var b=document.querySelector('.subtabs button[data-pane="'+want+'"]')
      || document.querySelector('.subtabs button');
  if(b) showPaneT(b);
  var wy='all';
  try{{ wy=sessionStorage.getItem('tripYear')||'all'; }}catch(e){{}}
  var yb=document.querySelector('.yearchips button[data-y="'+wy+'"]');
  if(yb) showYear(yb);
}});
</script>"""

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Points Ledger — Trips</title>
<style>{PAGE_CSS}
.tbar {{ display:flex; height:.5rem; border-radius:99px; overflow:hidden;
  margin:.7rem 0 .6rem; background:var(--box); }}
.tbar span {{ display:block; height:100%; }}
.tdot {{ display:inline-block; width:.5rem; height:.5rem; border-radius:50%;
  margin-right:.45rem; vertical-align:1px; }}
.tbig {{ font-size:1.35rem; font-weight:650; letter-spacing:-.015em;
  font-variant-numeric:tabular-nums; }}
.tcats {{ margin-top:.2rem; }}
.tfields {{ border:1px solid var(--line); border-radius:10px;
  padding:.8rem .9rem; display:grid; gap:.5rem; }}
.tfields legend {{ font-size:.78rem; color:var(--muted); padding:0 .35rem; }}
.card.dim {{ opacity:.6; }}
.stag.notcounted {{ color:var(--up); border-style:dashed;
  border-color:color-mix(in srgb, var(--up) 40%, var(--line)); }}
.savedbox {{ margin-top:.7rem; padding:.7rem .85rem; border-radius:10px;
  background:color-mix(in srgb, var(--drop) 8%, transparent);
  border:1px solid color-mix(in srgb, var(--drop) 25%, var(--line)); }}
.savedhead {{ display:flex; justify-content:space-between; gap:1rem;
  font-size:.86rem; color:var(--drop); margin-bottom:.3rem; }}
.savedhead b {{ font-variant-numeric:tabular-nums; }}
.rinl {{ width:6.5em; padding:.1rem .3rem; font:inherit; font-size:.82rem;
  text-align:right; font-variant-numeric:tabular-nums; border-radius:6px;
  border:1px solid var(--line); background:var(--card); color:var(--fg); }}
.rinl.amt {{ width:7.5em; font-weight:600; }}
.rinl:focus {{ outline:none; border-color:var(--accent); }}
.rinlwrap {{ display:inline-flex; align-items:center; gap:.2rem; }}
.orphan {{ margin:.2rem 0 .8rem; padding:.6rem .8rem; border-radius:10px;
  font-size:.82rem; color:var(--muted);
  background:color-mix(in srgb, var(--up) 8%, transparent);
  border:1px solid color-mix(in srgb, var(--up) 22%, var(--line)); }}
.orphan b {{ color:var(--fg); font-weight:550; }}
.tcheck {{ display:flex !important; align-items:center; gap:.45rem;
  font-size:.86rem; }}
.tcheck input {{ width:auto; flex:0 0 auto; }}
.catrow {{ cursor:pointer; border-radius:6px; }}
.catrow:hover {{ background:var(--box); }}
.catrow .covlab::after {{ content:"›"; margin-left:.35rem; opacity:.45; }}
.catdetail {{ margin:.1rem 0 .5rem 1rem; padding-left:.7rem;
  border-left:1px solid var(--line); }}
.catdetail[hidden] {{ display:none; }}
.covrow.sub {{ font-size:.8rem; padding:.18rem 0; }}
.covrow.sub .covlab {{ color:var(--muted); }}
.yearchips {{ display:flex; gap:.35rem; flex-wrap:wrap; margin:0 0 .4rem; }}
.yearchips button {{ height:1.9rem; padding:0 .85rem; border-radius:99px;
  border:1px solid var(--line); background:none; color:var(--muted);
  font-size:.84rem; cursor:pointer; font-family:inherit;
  font-variant-numeric:tabular-nums; }}
.yearchips button:hover {{ color:var(--fg); }}
.yearchips button.on {{ background:var(--box); color:var(--fg);
  border-color:color-mix(in srgb, var(--fg) 45%, var(--line)); }}
.ypane {{ display:none; }}
.ypane.on {{ display:block; }}
.yhead {{ display:flex; justify-content:space-between; align-items:baseline;
  gap:1rem; margin:1.5rem 0 .7rem; padding-bottom:.45rem;
  border-bottom:1px solid var(--line); flex-wrap:wrap; }}
.yhead > b {{ font-size:1.05rem; letter-spacing:-.01em; }}
.yhead span {{ color:var(--muted); font-size:.84rem;
  font-variant-numeric:tabular-nums; }}
.yhead span b {{ color:var(--fg); }}
.pane > .yhead:first-child {{ margin-top:.2rem; }}
.myear {{ padding:1rem 1.1rem; margin-bottom:.7rem; border-radius:12px;
  background:var(--card); border:1px solid var(--line); }}
.myhead {{ display:flex; justify-content:space-between; align-items:start;
  gap:1rem; margin-bottom:.8rem; font-size:.95rem; }}
.mysub {{ color:var(--muted); font-size:.84rem; margin-top:.15rem;
  font-variant-numeric:tabular-nums; }}
.mypm {{ text-align:right; }}
.mypm b {{ display:block; font-size:1.35rem; font-weight:650;
  letter-spacing:-.015em; font-variant-numeric:tabular-nums; }}
.mypm span {{ color:var(--muted); font-size:.76rem; text-transform:uppercase;
  letter-spacing:.05em; }}
.mgrid {{ display:grid; grid-template-columns:repeat(12,1fr); gap:.25rem; }}
.mcol {{ display:flex; flex-direction:column; align-items:center;
  gap:.25rem; opacity:.45; }}
.mcol.on {{ opacity:1; }}
.mbarwrap {{ width:100%; height:82px; display:flex; flex-direction:column;
  justify-content:flex-end; background:var(--box); border-radius:4px;
  overflow:hidden; }}
.mbar {{ width:100%; }}
.mbar.spent {{ background:var(--accent); }}
.mbar.ahead {{ background:color-mix(in srgb, var(--accent) 35%,
  transparent); }}
.mamt {{ font-size:.6rem; color:var(--muted); height:.9rem;
  font-variant-numeric:tabular-nums; }}
.mlab {{ font-size:.65rem; color:var(--muted); }}
.amtrow {{ display:flex; gap:.4rem; flex-wrap:wrap; align-items:center; }}
.amtrow input {{ min-width:0; }}
.amtrow input[name^="cat::"] {{ flex:1; }}
.addbtn {{ flex:0 0 2rem; height:2rem; border-radius:7px; cursor:pointer;
  border:1px solid var(--line); background:var(--box); color:var(--muted);
  font-size:1rem; line-height:1; font-family:inherit; }}
.addbtn:hover {{ color:var(--fg); border-color:var(--fg); }}
.addbtn[hidden] {{ display:none; }}
.addwrap {{ display:flex; gap:.4rem; align-items:center;
  flex:1 1 100%; }}
.addwrap[hidden] {{ display:none; }}
.addwrap .addbox {{ flex:1; }}
.addgo {{ flex:0 0 auto; height:2rem; padding:0 .8rem; border-radius:7px;
  border:none; background:var(--fg); color:var(--bg); font-size:.85rem;
  font-weight:550; cursor:pointer; font-family:inherit; }}
.addcancel {{ flex:0 0 auto; font-size:.8rem; }}
.rmnote {{ color:var(--up); font-size:.7rem; cursor:pointer; background:none;
  border:none; padding:0; font-family:inherit; }}
.rmnote:hover {{ text-decoration:underline; }}
.rmnotebox {{ margin-top:.3rem; padding:.4rem .55rem; border-radius:7px;
  background:var(--box); border:1px solid var(--line); color:var(--muted);
  font-size:.76rem; max-width:46ch; }}
.rmnotebox[hidden] {{ display:none; }}
.rmacts {{ display:flex; gap:.7rem; }}
.rowacts {{ margin-top:.6rem; font-size:.9rem; }}
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
.pane > form input, .editbox form input {{ padding:.45rem .55rem;
  border:1px solid var(--line); border-radius:7px; background:var(--bg);
  color:var(--fg); font-size:1em; }}
</style></head><body>
<div class="brand">Points Ledger</div>
{nav}<h1>Trip expenses</h1>
<div class="hmeta">What every trip cost, by category</div>
<div class="layout">
<aside class="side">{sidebar}</aside>
<main class="maincol">{controls}</main>
</div>
{script}
</body></html>"""
