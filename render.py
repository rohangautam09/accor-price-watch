"""Shared dashboard rendering for check.py (static file) and app.py (web UI)."""
import datetime as dt
import json
import math
import pathlib

FLOORS_FILE = pathlib.Path(__file__).parent / "floors.json"
ROOMS_FILE = pathlib.Path(__file__).parent / "rooms_cache.json"
SAVINGS_FILE = pathlib.Path(__file__).parent / "savings.json"

# reference points for "distance from the centre"
CITY_CENTERS = {
    "Amsterdam": (52.3728, 4.8936),   # Dam Square
    "Cologne": (50.9413, 6.9583),     # Cologne Cathedral
    "Brugge": (51.2093, 3.2247),      # Markt
    "Phuket": (7.8965, 98.2965),      # Patong Beach
}


def km_between(a, b):
    lat1, lon1, lat2, lon2 = map(math.radians, (*a, *b))
    h = (math.sin((lat2 - lat1) / 2) ** 2
         + math.cos(lat1) * math.cos(lat2) * math.sin((lon2 - lon1) / 2) ** 2)
    return 2 * 6371 * math.asin(math.sqrt(h))

BOOKING_URL = ("https://all.accor.com/booking/en/accor/hotel/{code}"
               "?dateIn={dateIn}&nights={nights}&compositions={adults}")


def fmt_inr(v):
    if v is None:
        return "–"
    return "₹{:,.0f}".format(v)


def fmt_dates(date_in, nights):
    d1 = dt.date.fromisoformat(date_in)
    d2 = d1 + dt.timedelta(days=int(nights))
    return f"{d1.strftime('%a %d %b')} → {d2.strftime('%a %d %b %Y')}"


def spark(series, booked):
    pts = [v for _, v in series if v is not None]
    if len(pts) < 2:
        return ""
    ref = [booked] if booked else []
    lo, hi = min(pts + ref), max(pts + ref)
    rng = (hi - lo) or 1
    w, h = 120, 28
    step = w / max(len(series) - 1, 1)
    coords = []
    for i, (_, v) in enumerate(series):
        if v is None:
            continue
        coords.append(f"{i*step:.1f},{h-2-(v-lo)/rng*(h-4):.1f}")
    booked_line = ""
    if booked:
        by = h - 2 - (booked - lo) / rng * (h - 4)
        booked_line = (f'<line x1="0" y1="{by:.1f}" x2="{w}" y2="{by:.1f}" '
                       f'stroke="var(--muted)" stroke-dasharray="3,3"/>')
    return (f'<svg width="{w}" height="{h}" style="vertical-align:middle">'
            f'{booked_line}'
            f'<polyline points="{" ".join(coords)}" fill="none" '
            f'stroke="var(--accent)" stroke-width="1.5"/></svg>')


def points_line(eur, fx, tax_pct=0.0):
    """ALL Reward points option: 2,000 pts = EUR 40, in 2,000-pt steps.
    Points can only cover the total minus the city tax (verified against
    real bookings: Amsterdam 12.5%, Cologne 5%), so the max is computed on
    the tax-exclusive amount; the remainder is paid by card."""
    if eur is None:
        return ""
    eligible = eur / (1 + (tax_pct or 0) / 100)
    steps = int(eligible // 40)
    if steps < 1:
        return '<div class="tiny pts">points: below €40 minimum</div>'
    pts = steps * 2000
    rem = eur - steps * 40
    rem_inr = f" ≈ {fmt_inr(rem * fx)}" if fx else ""
    return (f'<div class="tiny pts">💠 max {pts:,} pts + '
            f'€{rem:,.2f}{rem_inr} to pay</div>')


def price_box(label, inr_m, inr_s, amount=None, currency="", eur=None,
              fx=None, extra="", empty="–", tax_pct=0.0, prev_val=None):
    if inr_m is None:
        return (f'<div class="pbox"><div class="plabel">{label}</div>'
                f'<div class="pval dim">{empty}</div>{extra}</div>')
    delta = ""
    if prev_val:
        d = inr_m - prev_val
        if d < -50:
            delta = f' <span class="d dn">▼ {fmt_inr(-d)}</span>'
        elif d > 50:
            delta = f' <span class="d rs">▲ {fmt_inr(d)}</span>'
        else:
            delta = ' <span class="d sm">＝ same</span>'
    if amount is not None and currency != "INR":
        native = f'<div class="tiny">{amount:,.2f} {currency}</div>'
    elif eur is not None:
        native = f'<div class="tiny">€{eur:,.2f}</div>'
    else:
        native = ""
    return (f'<div class="pbox"><div class="plabel">{label}</div>'
            f'<div class="pval">{fmt_inr(inr_m)}{delta}</div>'
            f'<div class="tiny">member · {fmt_inr(inr_s)} standard</div>'
            f'{native}{points_line(eur, fx, tax_pct)}{extra}</div>')


def pts_inr(pts, fx):
    """INR value of points by Accor's formula: 2,000 pts = EUR 40."""
    if not fx:
        return None
    return pts * 0.02 * fx


def max_points_for(eur, tax_pct=0.0):
    """Most points Accor will let you put on a stay: 2,000-pt steps on the
    tax-exclusive amount (taxes are never points-payable)."""
    if not eur:
        return 0
    return int((eur / (1 + (tax_pct or 0) / 100)) // 40) * 2000


def mask_ref(ref):
    """Booking refs are enough to look up a reservation, so never publish
    them in full — show the shape only."""
    if not ref or ref == "—" or len(ref) < 4:
        return ref
    return f"{ref[:2]}{'•' * (len(ref) - 4)}{ref[-2:]}"


def render_page(config, history, fx, interactive=False, public=False,
                cloud=False, repo=None, workflow=None):
    """public=True renders a shareable copy: no booking numbers,
    no points ledger, no cash-at-hotel figures.
    cloud=True renders the full dashboard for GitHub Pages: booking refs
    masked, plus a Refresh button that dispatches the checker workflow."""
    floors = (json.loads(FLOORS_FILE.read_text())
              if FLOORS_FILE.exists() else {})
    rooms_cache = (json.loads(ROOMS_FILE.read_text())
                   if ROOMS_FILE.exists() else {})
    runs = {r["date"]: r for r in history}
    dates = sorted(runs)
    latest = runs[dates[-1]] if dates else None
    threshold = config.get("drop_threshold_inr", 500)

    total_pts = int(config.get("points_balance_total", 0))
    used_pts = sum(int(b.get("points_used", 0)) for b in config["bookings"])
    remaining_pts = total_pts - used_pts

    def worth_txt(pts):
        v = pts_inr(pts, fx)
        return (f' <small class="pts">≈ {fmt_inr(v)}</small>'
                if v is not None else "")

    if interactive:
        total_widget = (f'<input id="total_pts" type="number" min="0" '
                        f'value="{total_pts}" style="width:7.5em"> '
                        f'<button class="link" onclick="savePoints()">save'
                        f'</button> &nbsp;'
                        f'<input id="add_pts" type="number" '
                        f'placeholder="earned pts" style="width:6.5em"> '
                        f'<button class="link" onclick="addPoints()">+ add'
                        f'</button>')
    else:
        total_widget = f"<strong>{total_pts:,}</strong>"
    # can the points balance cover the stays actually booked?
    need_pts = cash_left_eur = flat_tax_eur = 0
    booked_n = 0
    for b in config["bookings"]:
        if b.get("status", "booked") != "booked" or not b.get("booked_eur"):
            continue
        booked_n += 1
        cap = max_points_for(b["booked_eur"], b.get("city_tax_pct"))
        need_pts += max(cap - int(b.get("points_used", 0)), 0)
        cash_left_eur += b["booked_eur"] - cap * 0.02
        flat_tax_eur += (float(b.get("city_tax_flat_eur") or 0)
                         * config["adults"] * int(b["nights"]))
    flat_note = (f'<br><small>Plus <strong>€{flat_tax_eur:,.2f}'
                 f'{f" ≈ {fmt_inr(flat_tax_eur * fx)}" if fx else ""}</strong>'
                 f' of flat city tax (Belgium) collected at the hotel — not '
                 f'in the Accor total, never payable with points.</small>'
                 if flat_tax_eur else "")
    gap = need_pts - remaining_pts
    if not booked_n:
        coverage = ""
    elif gap <= 0:
        coverage = (f'<div class="cov ok">✅ Your points cover every booked '
                    f'stay — {need_pts:,} pts needed, {remaining_pts:,} '
                    f'available ({abs(gap):,} to spare). Cash still due at '
                    f'the hotels (taxes + sub-€40 remainders): '
                    f'<strong>€{cash_left_eur:,.2f}'
                    f'{f" ≈ {fmt_inr(cash_left_eur * fx)}" if fx else ""}'
                    f'</strong>{flat_note}</div>')
    else:
        coverage = (f'<div class="cov short">⚠️ Short by <strong>{gap:,} pts'
                    f'</strong>{worth_txt(gap)} to fully cover your '
                    f'{booked_n} booked stay(s) — {need_pts:,} pts needed, '
                    f'{remaining_pts:,} available. Cash still due regardless '
                    f'(taxes + sub-€40 remainders): '
                    f'<strong>€{cash_left_eur:,.2f}'
                    f'{f" ≈ {fmt_inr(cash_left_eur * fx)}" if fx else ""}'
                    f'</strong>{flat_note}</div>')

    points_bar = "" if public else f"""
<div class="pointsbar">
  <span>💠 ALL points — total: {total_widget}{worth_txt(total_pts)}</span>
  <span>used in bookings: <strong>{used_pts:,}</strong>{worth_txt(used_pts)}</span>
  <span>remaining: <strong>{remaining_pts:,}</strong>{worth_txt(remaining_pts)}</span>
  <br><small>2,000 pts = €40{f" ≈ {fmt_inr(40 * fx)}" if fx else ""}
  {f" · 1 pt ≈ ₹{0.02 * fx:.2f}" if fx else ""}
  (rate refreshes on every price check)</small>
  {coverage}
</div>"""

    # savings ledger: every rebooking logged, oldest first
    ledger = (json.loads(SAVINGS_FILE.read_text())
              if SAVINGS_FILE.exists() else [])
    savings_bar = ""
    if ledger and not public:
        total_eur = sum(e["saved_eur"] for e in ledger)
        wins = [e for e in ledger if e["saved_eur"] > 0]
        lines = "".join(
            f'<tr><td>{dt.date.fromisoformat(e["date"]).strftime("%d %b")}</td>'
            f'<td>{e["name"][:38]}</td>'
            f'<td><small>{e["from"]} → {e["to"]}</small></td>'
            f'<td>€{e["old_eur"]:,.2f} → €{e["new_eur"]:,.2f}</td>'
            f'<td class="{"savepos" if e["saved_eur"] > 0 else ""}">'
            f'{"−" if e["saved_eur"] > 0 else ""}'
            f'€{abs(e["saved_eur"]):,.2f}'
            f'{f" ({fmt_inr(e['saved_eur'] * fx)})" if fx and e["saved_eur"] else ""}'
            f'</td></tr>'
            for e in reversed(ledger))
        savings_bar = f"""
<div class="savingsbar">
  <span>💰 Saved by rebooking: <strong>€{total_eur:,.2f}</strong>
    {f"≈ <strong>{fmt_inr(total_eur * fx)}</strong>" if fx else ""}</span>
  <span><small>{len(wins)} successful rebooking(s) ·
    {len(ledger)} tracked</small></span>
  <details><summary>see every rebooking</summary>
    <table class="ledger"><thead><tr><th>Date</th><th>Hotel</th>
    <th>Booking</th><th>Price</th><th>Saved</th></tr></thead>
    <tbody>{lines}</tbody></table>
  </details>
</div>"""

    body_rows = []
    summary_drops = []
    summary_moves = []
    for b in config["bookings"]:
        uid = f'{b["code"]}:{b["dateIn"]}:{int(b["nights"])}'
        pinned = bool(b.get("pinned"))
        is_booked = b.get("status", "booked") == "booked"
        status_tag = (f'<span class="stag booked">✅ booked</span>'
                      if is_booked else
                      f'<span class="stag track">👀 tracking</span>')
        cur = None
        if latest:
            cur = next((h for h in latest["hotels"]
                        if h.get("uid", h["code"]) == uid), None)
        wants_bb = bool(b.get("breakfast"))
        cmp_key = "inr_bb_member" if wants_bb else "inr_member"
        # manual app-discount: member prices shown as the app would charge
        app_pct = float(b.get("app_discount_pct") or 0)
        disc = 1 - app_pct / 100

        def ap(v):
            return v * disc if v is not None else None

        series = []
        for d in dates:
            h = next((x for x in runs[d]["hotels"]
                      if x.get("uid", x["code"]) == uid
                      and "inr_member" in x), None)
            series.append((d, ap(h.get(cmp_key, h.get("inr_member")))
                           if h else None))

        # booked amounts are fixed in EUR; the ₹ figure re-converts at the
        # current rate on every fetch (mirrors Accor's own account page)
        eff_booked = (b["booked_eur"] * fx
                      if b.get("booked_eur") and fx else b["booked_inr"])
        gps = rooms_cache.get(b["code"], {}).get("_gps")
        center = CITY_CENTERS.get(b.get("city", ""))
        dist_km = km_between(gps, center) if gps and center else None
        insights = []
        room_name = ""
        score = None
        score_title = ""
        inr_m = None
        diff = None
        if cur and "inr_member" in cur:
            inr_m = ap(cur.get(cmp_key, cur["inr_member"]))
            diff = inr_m - eff_booked
            if not b["booked_inr"]:
                badge = '<span class="badge same">watching</span>'
            elif diff < -threshold:
                badge = (f'<span class="badge drop">▼ {fmt_inr(-diff)} '
                         f'cheaper</span>')
            elif diff > threshold:
                badge = (f'<span class="badge up">▲ {fmt_inr(diff)} '
                         f'costlier</span>')
            else:
                badge = '<span class="badge same">≈ same</span>'
            prev = cur.get("prev") or {}
            if prev:
                at = prev["at"][5:16].replace("T", " ")
                badge += (f'<div class="prevrun same">▲▼ vs last run '
                          f'({at})</div>')
            anchor = 'h' + uid.replace(':', '-')
            if b["booked_inr"] and diff < -threshold:
                cancel = ("" if public or b["booking_no"] in ("—", "")
                          else f' — rebook, then cancel {b["booking_no"]}')
                summary_drops.append(
                    f'<a class="ci cidrop" href="#{anchor}">🔥 {b["name"]}: '
                    f'{fmt_inr(-diff)} below your booked price '
                    f'(now {fmt_inr(inr_m)}){cancel}</a>')
            pv0 = ap(prev.get(cmp_key) or prev.get("inr_member"))
            if pv0:
                pd0 = inr_m - pv0
                if pd0 < -threshold:
                    summary_moves.append(
                        f'<a class="ci cidn" href="#{anchor}">▼ {b["name"]}: '
                        f'{fmt_inr(-pd0)} down since last run</a>')
                elif pd0 > threshold:
                    summary_moves.append(
                        f'<a class="ci ciup" href="#{anchor}">▲ {b["name"]}: '
                        f'{fmt_inr(pd0)} up since last run</a>')
            currency = cur.get("currency") or ""
            eur_m = cur.get("eur_member")
            eur_bb = cur.get("eur_bb_member")
            if currency == "EUR":  # older history entries lack eur_* fields
                eur_m = eur_m or cur.get("member_amount")
                eur_bb = eur_bb or cur.get("bb_member_amount")
            tax_pct = float(b.get("city_tax_pct") or 0)
            flex_box = price_box("Flexible", ap(cur["inr_member"]),
                                 cur.get("inr_standard"),
                                 cur.get("member_amount"), currency,
                                 ap(eur_m), fx, tax_pct=tax_pct,
                                 prev_val=ap(prev.get("inr_member")))
            bb_box = price_box("Flexible + breakfast",
                               ap(cur.get("inr_bb_member")),
                               cur.get("inr_bb_standard"),
                               cur.get("bb_member_amount"), currency,
                               ap(eur_bb), fx, empty="not offered",
                               tax_pct=tax_pct,
                               prev_val=ap(prev.get("inr_bb_member")))
            eur_nf = cur.get("eur_nf_member")
            eur_nf_bb = cur.get("eur_nf_bb_member")
            if currency == "EUR":
                eur_nf = eur_nf or cur.get("nf_member_amount")
                eur_nf_bb = eur_nf_bb or cur.get("nf_bb_member_amount")
            nf_box = price_box("Non-flexible", ap(cur.get("inr_nf_member")),
                               cur.get("inr_nf_standard"),
                               cur.get("nf_member_amount"), currency,
                               ap(eur_nf), fx, empty="not offered",
                               tax_pct=tax_pct,
                               prev_val=ap(prev.get("inr_nf_member")))
            nf_bb_box = price_box("Non-flex + breakfast",
                                  ap(cur.get("inr_nf_bb_member")),
                                  cur.get("inr_nf_bb_standard"),
                                  cur.get("nf_bb_member_amount"), currency,
                                  ap(eur_nf_bb), fx, empty="not offered",
                                  tax_pct=tax_pct,
                                  prev_val=ap(prev.get("inr_nf_bb_member")))
            room_name = cur.get("room_name", cur.get("room", ""))
            if app_pct:
                insights.append(f'📱 {app_pct:g}% app discount applied '
                                f'to member prices')
            if cur.get("alt_scan_days"):
                alt = cur.get("alt_best")
                if alt:
                    alt_d = dt.date.fromisoformat(alt["dateIn"])
                    gain = ap(cur["inr_member"]) - ap(alt["inr_member"])
                    insights.append(
                        f'📅 cheaper start: {alt_d.strftime("%a %d %b")} · '
                        f'{fmt_inr(ap(alt["inr_member"]))} '
                        f'<b>(−{fmt_inr(gain)})</b>')
                else:
                    insights.append(
                        f'📅 your start date is the cheapest '
                        f'within ±{cur["alt_scan_days"]} days')
            vals = [(d, v) for d, v in series if v is not None]
            if len(vals) > 1:
                low_d, low_v = min(vals, key=lambda x: x[1])
                insights.append(
                    f'📉 lowest tracked: {fmt_inr(low_v)} '
                    f'({dt.date.fromisoformat(low_d).strftime("%d %b")})')
            floor = floors.get(f'{b["code"]}:{b["dateIn"]}')
            over_floor = None
            if floor:
                over_floor = (cur["inr_member"] / floor["inr"] - 1) * 100
                fd = dt.date.fromisoformat(floor["dateIn"])
                insights.append(
                    f'🧭 6-mo floor: {fmt_inr(ap(floor["inr"]))} '
                    f'(start {fd.strftime("%d %b")}) — your dates '
                    f'<b>+{over_floor:.0f}%</b>')

            # recommendation score (0–100): affordability, location,
            # breakfast economics, value vs floor, and beating the booking
            per_night = inr_m / max(b["nights"], 1)
            score = 70.0
            parts = []
            pen = min(per_night / 1500, 20)
            score -= pen
            parts.append(f"price/night ₹{per_night:,.0f}: −{pen:.0f}")
            if dist_km is not None:
                pen = min(dist_km * 4, 25)
                score -= pen
                parts.append(f"{dist_km:.1f} km from centre: −{pen:.0f}")
            bb_now = ap(cur.get("inr_bb_member"))
            if bb_now and cur.get("inr_member"):
                prem = (bb_now - ap(cur["inr_member"])) / max(b["nights"], 1)
                if prem < 1200:
                    score += 6
                    parts.append(f"cheap breakfast (+₹{prem:,.0f}/night): +6")
            if b["booked_inr"]:
                if diff < -threshold:
                    score += 20
                    parts.append("cheaper than your booking: +20")
                elif diff < threshold:
                    score += 8
                    parts.append("holding your booked price: +8")
            if over_floor is not None and over_floor > 0:
                pen = min(over_floor / 4, 18)
                score -= pen
                parts.append(f"+{over_floor:.0f}% over 6-mo floor: −{pen:.0f}")
            score = max(5, min(100, score))
            score_title = " · ".join(parts)
        else:
            badge = '<span class="badge err">no data</span>'
            err = (cur or {}).get("error", "not yet checked")
            flex_box = price_box("Flexible", None, None, empty=err)
            bb_box = price_box("Flexible + breakfast", None, None)
            nf_box = price_box("Non-flexible", None, None)
            nf_bb_box = price_box("Non-flex + breakfast", None, None)

        open_url = BOOKING_URL.format(code=b["code"], dateIn=b["dateIn"],
                                      nights=b["nights"],
                                      adults=config["adults"])
        b_pts = int(b.get("points_used", 0))
        booked_extra = ""
        if b["booked_inr"]:
            booked_extra = (f'<div class="tiny">{"🍳 incl. breakfast"
                            if wants_bb else "room only"}</div>')
        if b["booked_inr"] and b_pts and not public:
            pv = pts_inr(b_pts, fx)
            if pv:
                cash = max(eff_booked - pv, 0)
                booked_extra += (f'<div class="tiny pts">− {b_pts:,} pts '
                                 f'(≈ {fmt_inr(pv)})</div>'
                                 f'<div class="tiny">cash at hotel ≈ '
                                 f'<b>{fmt_inr(cash)}</b></div>')
        if b["booked_inr"]:
            eur_line = (f'<div class="tiny">€{b["booked_eur"]:,.2f} fixed · '
                        f'₹ at today\'s rate</div>'
                        if b.get("booked_eur") else "")
            if is_booked and b.get("booked_eur") and not public:
                cap = max_points_for(b["booked_eur"], b.get("city_tax_pct"))
                short = max(cap - b_pts, 0)
                cash_after = b["booked_eur"] - cap * 0.02
                flat = (float(b.get("city_tax_flat_eur") or 0)
                        * config["adults"] * int(b["nights"]))
                flat_txt = (f' + €{flat:,.2f} city tax at hotel'
                            if flat else "")
                if short:
                    eur_line += (
                        f'<div class="tiny pts">if paid with points: '
                        f'<b>{cap:,} pts</b> + €{cash_after:,.2f} cash'
                        f'{flat_txt}'
                        + (f' · {b_pts:,} applied so far' if b_pts else
                           ' · booked with cash — would need rebooking')
                        + '</div>')
                else:
                    eur_line += (
                        f'<div class="tiny pts">💠 {b_pts:,} pts applied — '
                        f'max reached · €{cash_after:,.2f} cash{flat_txt}'
                        f'</div>')
            booked_box = (f'<div class="pbox"><div class="plabel">Booked'
                          f'</div><div class="pval">'
                          f'{fmt_inr(eff_booked)}</div>'
                          f'{eur_line}{booked_extra}</div>')
        else:
            booked_box = price_box("Booked", None, None,
                                   extra=booked_extra, empty="not booked")
        remove_btn = ""
        edit_box = ""
        if interactive:
            safe_name = b["name"].replace("'", "\\'")
            move_btns = ""
            if pinned:
                move_btns = (
                    f'<button class="link" title="move up among pinned" '
                    f"onclick=\"movePin('{b['code']}','{b['dateIn']}',"
                    f'{int(b["nights"])},-1)">▲</button>'
                    f'<button class="link" title="move down among pinned" '
                    f"onclick=\"movePin('{b['code']}','{b['dateIn']}',"
                    f'{int(b["nights"])},1)">▼</button>')
            remove_btn = (move_btns
                          + f'<button class="link" onclick="'
                          f"togglePin('{b['code']}','{b['dateIn']}',{int(b['nights'])})\">"
                          f'{"📌 unpin" if pinned else "📌 pin to top"}</button>'
                          f'<button class="link" onclick="'
                          f"floorScan('{b['code']}','{b['dateIn']}',{int(b['nights'])})\">"
                          f'🧭 6-mo floor</button>'
                          f'<button class="link danger" onclick="'
                          f"removeHotel('{b['code']}','{b['dateIn']}',{int(b['nights'])},"
                          f"'{safe_name}')\">remove</button>")
            edit_box = f"""<details class="rowedit"><summary>edit</summary>
              <form onsubmit="return updateBooking(event,'{b["code"]}','{b["dateIn"]}',{int(b["nights"])})">
                <label>Booked total ₹
                  <input type="number" name="booked_inr" min="0" step="0.01"
                         value="{b["booked_inr"]}"></label>
                <label>Booking number
                  <input name="booking_no" value="{b["booking_no"]}"></label>
                <label>Points used on this booking
                  <input type="number" name="points_used" min="0" step="1000"
                         value="{b_pts}"></label>
                <label>City tax % (for points max calc)
                  <input type="number" name="city_tax_pct" min="0" max="30"
                         step="0.1" value="{b.get("city_tax_pct", 0)}"></label>
                <label>Status
                  <select name="status">
                    <option value="booked" {"selected" if is_booked else ""}>
                      ✅ booked</option>
                    <option value="tracking" {"" if is_booked else "selected"}>
                      👀 just tracking</option>
                  </select></label>
                <label>App discount % (as seen in the Accor app)
                  <input type="number" name="app_discount_pct" min="0"
                         max="50" step="0.5"
                         value="{b.get("app_discount_pct", 0)}"></label>
                <label style="grid-auto-flow:column; justify-content:start;
                              align-items:center; gap:.4rem">
                  <input type="checkbox" name="breakfast"
                         {"checked" if wants_bb else ""}
                         style="width:auto"> Breakfast included</label>
                <button type="submit" class="primary">Save</button>
              </form></details>"""
        booking_line = ("booked" if public else
                        f'booking {mask_ref(b["booking_no"]) if cloud else b["booking_no"]}'
                        ) if b["booked_inr"] else "not booked — watching"
        city_part = f'{b["city"]} · ' if b.get("city") else ""
        room_part = (f'<div class="hmeta">room: {room_name}</div>'
                     if room_name else "")
        insights_html = ("" if not insights else
                         '<div class="insights">'
                         + "".join(f"<span>{s}</span>" for s in insights)
                         + "</div>")
        trend = spark(series, eff_booked)
        trend_html = f'<div class="trend">{trend}</div>' if trend else ""
        body_rows.append(f"""<div class="card{' pinned' if pinned else ''}"
  id="h{uid.replace(':', '-')}" data-pin="{1 if pinned else 0}"
  data-pinrank="{int(b.get('pin_rank', 0) or 0)}"
  data-score="{(score or 0):.1f}" data-price="{(inr_m or 0):.0f}"
  data-diff="{diff if diff is not None and b["booked_inr"] else 9e12:.0f}">
  <div class="chead">
    <div class="cinfo">
      <div class="hname">{'📌 ' if pinned else ''}{b["name"]}{status_tag}{f'<span class="apptag">📱 {app_pct:g}% app discount</span>' if app_pct else ""}</div>
      <div class="hmeta">{city_part}{fmt_dates(b["dateIn"], b["nights"])} ·
        {b["nights"]} night(s) · {booking_line}{f' · 📍 {dist_km:.1f} km from centre' if dist_km is not None else ''}</div>
      {room_part}
    </div>
    <div class="cright">{badge}{f'<div class="score" title="{score_title}">★ {score:.0f}/100</div>' if score is not None else ''}{trend_html}</div>
  </div>
  <div class="prices">{booked_box}{flex_box}{bb_box}{nf_box}{nf_bb_box}</div>
  {insights_html}
  <div class="cactions">
    <a href="{open_url}" target="_blank">open on Accor ↗</a>
    {remove_btn}{edit_box}
  </div>
</div>""")

    if latest:
        checked = latest.get("checked_at", latest["date"]).replace("T", " ")
        dur = latest.get("duration_seconds")
        if dur:
            checked += (f" (fetched in {dur // 60}m {dur % 60}s)"
                        if dur >= 60 else f" (fetched in {dur}s)")
    else:
        checked = "never"

    subtitle = (f"Watching {len(config['bookings'])} hotel(s) · "
                f"{config['adults']} adults · flexible rates only")

    if summary_drops or summary_moves:
        changebar = ('<div class="changebar">'
                     + "".join(summary_drops + summary_moves) + "</div>")
    elif latest:
        changebar = (f'<div class="changebar quiet">😴 nothing moved more '
                     f'than {fmt_inr(threshold)} since the last run</div>')
    else:
        changebar = ""
    fx_note = (f"1 EUR ≈ ₹{fx:,.2f} (live rate; INR figures are approximate "
               f"conversions of logged-out member prices)" if fx else
               "FX rate unavailable — INR conversion skipped")

    controls = ""
    script = ""
    if cloud:
        controls = f"""
<div class="controls">
  <button id="checkbtn" class="primary" onclick="cloudRefresh()">↻ Refresh prices</button>
  <span id="status"></span>
  <details><summary>set up the refresh button (once)</summary>
    <div style="max-width:520px;font-size:.9em;color:var(--muted);
                padding:.8rem 0">
      Refreshing runs the checker on GitHub, which needs a token from your
      account. Create a fine-grained token with <b>Actions: read &amp; write</b>
      on <code>{repo}</code>, then paste it here — it is stored only in this
      browser, never published.
      <div style="margin-top:.6rem">
        <input id="pat" type="password" placeholder="github_pat_…"
               style="padding:.45rem .6rem;border:1px solid var(--line);
                      border-radius:8px;background:var(--bg);color:var(--fg);
                      min-width:260px">
        <button class="link" onclick="savePat()">save token</button>
        <button class="link danger" onclick="clearPat()">forget</button>
      </div>
    </div>
  </details>
</div>"""
        script = f"""
<script>
const REPO="{repo}", WF="{workflow}";
function savePat(){{
  const v=document.getElementById('pat').value.trim();
  if(v){{ localStorage.setItem('accor_pat', v);
    document.getElementById('pat').value='';
    alert('Token saved on this device. The Refresh button is ready.'); }}
}}
function clearPat(){{ localStorage.removeItem('accor_pat');
  alert('Token removed from this device.'); }}
async function cloudRefresh(){{
  const t=localStorage.getItem('accor_pat');
  const st=document.getElementById('status');
  if(!t){{ st.textContent='Add your token once — see the setup link below.';
    return; }}
  const btn=document.getElementById('checkbtn'); btn.disabled=true;
  const t0=Date.now();
  st.textContent='Asking GitHub to fetch fresh prices…';
  const r=await fetch(
    'https://api.github.com/repos/'+REPO+'/actions/workflows/'+WF+'/dispatches',
    {{method:'POST',headers:{{'Authorization':'Bearer '+t,
      'Accept':'application/vnd.github+json'}},
      body:JSON.stringify({{ref:'main'}})}});
  if(!r.ok){{ btn.disabled=false;
    st.textContent='GitHub refused the token ('+r.status+'). Check it has Actions write access.';
    return; }}
  const started=document.getElementById('stamp').textContent;
  const poll=setInterval(async()=>{{
    const secs=Math.round((Date.now()-t0)/1000);
    st.textContent='Fetching prices… '+secs+'s (page reloads when ready)';
    if(secs>25){{
      const html=await (await fetch(location.pathname+'?t='+Date.now())).text();
      if(html.indexOf(started)<0){{ clearInterval(poll); location.reload(); }}
    }}
    if(secs>180){{ clearInterval(poll); btn.disabled=false;
      st.textContent='Taking longer than usual — reload in a minute.'; }}
  }},5000);
}}
</script>"""
    if interactive:
        controls = """
<div class="controls">
  <button id="checkbtn" class="primary" onclick="runCheck()">↻ Check prices now</button>
  <span id="status"></span>
  <details id="addbox"><summary>＋ Add a hotel</summary>
    <form onsubmit="return addHotel(event)">
      <label>Hotel page link (from all.accor.com) or hotel code
        <input name="code_or_url" required oninput="prefillFromUrl(this)"
               placeholder="paste any all.accor.com hotel link, e.g. …/hotel/2783?dateIn=2026-09-16…"></label>
      <small id="prefill_note" hidden>✓ dates filled in from the link — adjust if needed</small>
      <label>Check-in <input type="date" name="dateIn" required></label>
      <label>Nights <input type="number" name="nights" min="1" value="1" required></label>
      <label>City (optional) <input name="city"></label>
      <label>Booked total ₹ (leave 0 if not booked yet)
        <input type="number" name="booked_inr" min="0" step="0.01" value="0"></label>
      <label>Booking number (if booked) <input name="booking_no"></label>
      <button type="submit" class="primary">Add to watch list</button>
    </form>
  </details>
  <details><summary>⚙ Settings</summary>
    <form onsubmit="return saveSettings(event)">
      <label>Scan ± days around each check-in for cheaper start dates
        (0 = off, max 14)
        <input type="number" name="date_scan_days" min="0" max="14"
               value="__SCAN_DAYS__" style="max-width:6em"></label>
      <button type="submit" class="primary">Save</button>
    </form>
  </details>
</div>"""
        script = """
<script>
function prefillFromUrl(inp){
  const v=inp.value, f=inp.form;
  const d=v.match(/dateIn=(\\d{4}-\\d{2}-\\d{2})/);
  const dOut=v.match(/dateOut=(\\d{4}-\\d{2}-\\d{2})/);
  const n=v.match(/nights=(\\d+)/);
  let filled=false;
  if(d){ f.dateIn.value=d[1]; filled=true; }
  if(n){ f.nights.value=n[1]; filled=true; }
  else if(d&&dOut){
    const ms=new Date(dOut[1])-new Date(d[1]);
    if(ms>0){ f.nights.value=Math.round(ms/864e5); filled=true; }
  }
  document.getElementById('prefill_note').hidden=!filled;
}
async function runCheck(){
  const btn=document.getElementById('checkbtn');
  btn.disabled=true;
  const st=document.getElementById('status');
  const t0=Date.now();
  const secs=()=>Math.round((Date.now()-t0)/1000)+'s';
  st.textContent='Checking prices…';
  await fetch('/api/check',{method:'POST'});
  const t=setInterval(async()=>{
    try{
      const s=await (await fetch('/api/status')).json();
      if(s.running){
        st.textContent='Checking… '+(s.progress||'starting')+' · '+secs()+' elapsed';
      }else{
        clearInterval(t);
        st.textContent='Done in '+secs()+' — refreshing…';
        setTimeout(()=>location.reload(),800);
      }
    }catch(e){}
  },1000);
}
async function addHotel(ev){
  ev.preventDefault();
  const data=Object.fromEntries(new FormData(ev.target));
  const r=await fetch('/api/hotels',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const j=await r.json();
  if(!r.ok){ alert(j.error||'Could not add hotel'); return false; }
  document.getElementById('addbox').removeAttribute('open');
  alert('Added: '+j.name+'\\nFetching its latest price now…');
  runCheck();
  return false;
}
async function saveSettings(ev){
  ev.preventDefault();
  const f=ev.target;
  const r=await fetch('/api/settings',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({date_scan_days:f.date_scan_days.value})});
  const j=await r.json();
  if(!r.ok){ alert(j.error||'Could not save'); return false; }
  location.reload();
  return false;
}
async function savePoints(){
  const v=document.getElementById('total_pts').value;
  const r=await fetch('/api/points',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({total_points:v})});
  if(!r.ok){ alert('Could not save'); return; }
  location.reload();
}
async function addPoints(){
  const v=parseInt(document.getElementById('add_pts').value||'0',10);
  if(!v){ alert('Enter how many points to add'); return; }
  const r=await fetch('/api/points',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({add_points:v})});
  if(!r.ok){ alert('Could not add'); return; }
  location.reload();
}
async function updateBooking(ev,code,dateIn,nights){
  ev.preventDefault();
  const data=Object.fromEntries(new FormData(ev.target));
  data.code=code; data.dateIn=dateIn; data.nights=nights;
  const r=await fetch('/api/hotels/update',{method:'POST',
    headers:{'Content-Type':'application/json'},body:JSON.stringify(data)});
  const j=await r.json();
  if(!r.ok){ alert(j.error||'Could not update'); return false; }
  location.reload();
  return false;
}
async function movePin(code,dateIn,nights,dir){
  await fetch('/api/hotels/movepin',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:code,dateIn:dateIn,nights:nights,dir:dir})});
  location.reload();
}
async function togglePin(code,dateIn,nights){
  await fetch('/api/hotels/pin',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:code,dateIn:dateIn,nights:nights})});
  location.reload();
}
async function floorScan(code,dateIn,nights){
  if(!confirm('Scan the next 6 months for this hotel\\'s cheapest flexible price? Takes ~30 seconds.'))return;
  const st=document.getElementById('status');
  const t0=Date.now();
  st.textContent='Scanning 6 months of dates…';
  await fetch('/api/floor',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:code,dateIn:dateIn,nights:nights})});
  const t=setInterval(async()=>{
    try{
      const s=await (await fetch('/api/status')).json();
      if(!s.running){
        clearInterval(t);
        st.textContent='Done in '+Math.round((Date.now()-t0)/1000)+'s — refreshing…';
        setTimeout(()=>location.reload(),800);
      }
    }catch(e){}
  },1000);
}
async function removeHotel(code,dateIn,nights,name){
  if(!confirm('Remove "'+name+'" from the watch list?\\n(This does NOT touch any real booking.)'))return;
  await fetch('/api/hotels/delete',{method:'POST',
    headers:{'Content-Type':'application/json'},
    body:JSON.stringify({code:code,dateIn:dateIn,nights:nights})});
  location.reload();
}
</script>"""
        controls = controls.replace(
            "__SCAN_DAYS__", str(config.get("date_scan_days", 7)))

    return f"""<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Accor price watch</title>
<style>
:root {{ --bg:#f6f7f9; --card:#ffffff; --box:#f8f9fb; --fg:#16181d;
  --muted:#7a8089; --line:#e4e6ea; --accent:#0a6cdf;
  --drop:#0a8f3c; --up:#c0392b; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0e0f12; --card:#17181c; --box:#1d1f24; --fg:#e8e9eb;
    --muted:#9aa0a8; --line:#2a2c32; --accent:#4f9cf0; }} }}
* {{ box-sizing:border-box; }}
body {{ font:15px/1.55 -apple-system, "Segoe UI", sans-serif;
  background:var(--bg); color:var(--fg); max-width:1360px;
  margin:0 auto; padding:2rem 1.4rem 4rem; }}
h1 {{ font-size:1.55rem; margin:0 0 .1rem; letter-spacing:-.02em; }}
small {{ color:var(--muted); }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.pts {{ color:color-mix(in srgb, var(--accent) 80%, var(--muted)); }}
.card {{ background:var(--card); border:1px solid var(--line);
  border-radius:16px; padding:1.05rem 1.2rem; margin-bottom:.85rem;
  box-shadow:0 1px 2px rgba(0,0,0,.04); }}
.card.pinned {{ border-color:color-mix(in srgb, var(--accent) 45%,
  var(--line)); box-shadow:0 1px 3px color-mix(in srgb, var(--accent) 18%,
  transparent); }}
.chead {{ display:flex; justify-content:space-between; gap:1rem;
  align-items:flex-start; }}
.hname {{ font-size:1.06rem; font-weight:650; letter-spacing:-.01em; }}
.hmeta {{ color:var(--muted); font-size:.84rem; margin-top:.1rem; }}
.stag {{ margin-left:.5rem; padding:.14rem .55rem; border-radius:99px;
  font-size:.72rem; font-weight:600; vertical-align:2px; white-space:nowrap; }}
.stag.booked {{ background:color-mix(in srgb, var(--drop) 13%, transparent);
  color:var(--drop); }}
.stag.track {{ background:color-mix(in srgb, var(--muted) 16%, transparent);
  color:var(--muted); }}
.cov {{ margin-top:.6rem; padding:.5rem .8rem; border-radius:10px;
  font-size:.9rem; }}
.cov.ok {{ background:color-mix(in srgb, var(--drop) 12%, transparent);
  color:var(--drop); }}
.cov.short {{ background:color-mix(in srgb, var(--amber, #b8860b) 14%,
  transparent); color:color-mix(in srgb, var(--up) 75%, var(--fg)); }}
details select {{ padding:.45rem .55rem; border:1px solid var(--line);
  border-radius:7px; background:var(--bg); color:var(--fg); font-size:1em; }}
.apptag {{ margin-left:.5rem; padding:.14rem .55rem; border-radius:99px;
  font-size:.72rem; font-weight:600; vertical-align:2px; white-space:nowrap;
  background:color-mix(in srgb, var(--accent) 12%, transparent);
  color:var(--accent); }}
.cright {{ text-align:right; flex:none; }}
.trend {{ margin-top:.4rem; opacity:.9; }}
.prices {{ display:grid;
  grid-template-columns:repeat(auto-fit, minmax(185px, 1fr));
  gap:.55rem; margin:.85rem 0 .15rem; }}
.pbox {{ background:var(--box); border:1px solid var(--line);
  border-radius:11px; padding:.6rem .8rem .65rem; }}
.plabel {{ font-size:.68rem; font-weight:600; text-transform:uppercase;
  letter-spacing:.07em; color:var(--muted); margin-bottom:.2rem; }}
.pval {{ font-size:1.22rem; font-weight:680; letter-spacing:-.01em; }}
.pval.dim {{ color:var(--muted); font-size:.95rem; font-weight:500; }}
.tiny {{ font-size:.79rem; color:var(--muted); margin-top:.12rem; }}
.insights {{ display:flex; flex-wrap:wrap; gap:.4rem; margin:.6rem 0 .1rem; }}
.insights span {{ font-size:.8rem; padding:.28rem .65rem; border-radius:8px;
  background:color-mix(in srgb, var(--accent) 8%, transparent);
  color:color-mix(in srgb, var(--fg) 82%, var(--accent)); }}
.insights b {{ color:var(--accent); }}
.cactions {{ display:flex; flex-wrap:wrap; gap:1.1rem; align-items:baseline;
  margin-top:.65rem; font-size:.86rem; }}
.badge {{ padding:.22rem .65rem; border-radius:99px; font-size:.84em;
  white-space:nowrap; font-weight:600; }}
.prevrun {{ display:inline-block; margin-top:.35rem; padding:.15rem .55rem;
  border-radius:8px; font-size:.76rem; white-space:nowrap; }}
.changebar {{ display:flex; flex-direction:column; gap:.35rem;
  margin:.4rem 0 .8rem; }}
.changebar.quiet {{ color:var(--muted); font-size:.86rem; }}
.ci {{ display:block; padding:.5rem .9rem; border-radius:10px;
  font-size:.92rem; text-decoration:none; }}
.ci:hover {{ text-decoration:none; filter:brightness(1.05); }}
.ci.cidrop {{ background:color-mix(in srgb, var(--drop) 13%, transparent);
  color:var(--drop); font-weight:650; }}
.ci.cidn {{ background:color-mix(in srgb, var(--drop) 7%, transparent);
  color:var(--drop); }}
.ci.ciup {{ background:color-mix(in srgb, var(--up) 7%, transparent);
  color:color-mix(in srgb, var(--up) 85%, var(--fg)); }}
.card {{ scroll-margin-top:1rem; }}
.d {{ font-size:.74rem; font-weight:650; vertical-align:2px;
  white-space:nowrap; }}
.d.dn {{ color:var(--drop); }}
.d.rs {{ color:var(--up); }}
.d.sm {{ color:var(--muted); font-weight:500; }}
.drop {{ background:color-mix(in srgb, var(--drop) 14%, transparent);
  color:var(--drop); }}
.up {{ background:color-mix(in srgb, var(--up) 11%, transparent);
  color:color-mix(in srgb, var(--up) 85%, var(--fg)); }}
.same, .err {{ background:color-mix(in srgb, var(--muted) 14%, transparent);
  color:var(--muted); font-weight:500; }}
.controls {{ margin:1rem 0 .4rem; }}
.savingsbar {{ margin:1rem 0 .4rem; padding:.85rem 1.1rem; border-radius:14px;
  background:color-mix(in srgb, var(--drop) 9%, var(--card));
  border:1px solid color-mix(in srgb, var(--drop) 30%, var(--line)); }}
.savingsbar > span {{ margin-right:1.4rem; }}
.savingsbar summary {{ color:var(--drop); }}
table.ledger {{ border-collapse:collapse; width:100%; margin-top:.7rem;
  font-size:.86rem; }}
table.ledger th, table.ledger td {{ text-align:left; padding:.35rem .6rem;
  border-bottom:1px solid var(--line); white-space:nowrap; }}
table.ledger th {{ color:var(--muted); font-weight:600; font-size:.78rem;
  text-transform:uppercase; letter-spacing:.05em; }}
td.savepos {{ color:var(--drop); font-weight:650; }}
.pointsbar {{ margin:1rem 0 .4rem; padding:.85rem 1.1rem;
  border:1px solid var(--line); border-radius:14px; background:var(--card); }}
.pointsbar > span {{ margin-right:1.5rem; white-space:nowrap; }}
.pointsbar input {{ padding:.3rem .45rem; border:1px solid var(--line);
  border-radius:7px; background:var(--bg); color:var(--fg); font-size:1em; }}
.chip {{ display:inline-block; padding:.12rem .6rem; margin:.15rem .25rem 0 0;
  border:1px solid var(--line); border-radius:99px; white-space:nowrap;
  background:var(--box); }}
.lastcheck {{ display:inline-block; margin:.5rem 0 .3rem;
  padding:.45rem 1rem; border-radius:99px; font-weight:600; font-size:.92rem;
  background:color-mix(in srgb, var(--accent) 11%, transparent);
  color:var(--accent); }}
.searchbar {{ margin:.9rem 0 1.1rem; display:flex; align-items:center;
  gap:.8rem; }}
.searchbar input {{ flex:1; max-width:430px; padding:.55rem .85rem;
  border:1px solid var(--line); border-radius:10px; background:var(--card);
  color:var(--fg); font-size:1em; }}
.searchbar input:focus {{ outline:2px solid
  color-mix(in srgb, var(--accent) 45%, transparent); border-color:transparent; }}
.searchbar select {{ padding:.5rem .7rem; border:1px solid var(--line);
  border-radius:10px; background:var(--card); color:var(--fg);
  font-size:.92em; cursor:pointer; }}
.score {{ display:inline-block; margin:.35rem 0 0 .5rem; padding:.15rem .6rem;
  border-radius:99px; font-size:.78rem; font-weight:650; cursor:help;
  background:color-mix(in srgb, var(--accent) 10%, transparent);
  color:var(--accent); }}
details.rowedit {{ margin:0; }}
details.rowedit summary {{ font-size:.86rem; }}
details.rowedit form {{ max-width:250px; padding:.75rem; }}
button.primary {{ background:var(--accent); color:#fff; border:none;
  padding:.55rem 1.1rem; border-radius:9px; font-size:.95em; font-weight:600;
  cursor:pointer; }}
button.primary:hover {{ filter:brightness(1.08); }}
button.primary:disabled {{ opacity:.5; cursor:wait; }}
button.link {{ background:none; border:none; padding:0; cursor:pointer;
  font-size:.86rem; color:var(--accent); }}
button.link:hover {{ text-decoration:underline; }}
button.link.danger {{ color:var(--up); }}
#status {{ margin-left:.8rem; color:var(--muted); font-size:.9rem; }}
.controls details {{ margin-top:.7rem; }}
details summary {{ cursor:pointer; color:var(--accent); }}
details form {{ display:grid; gap:.6rem; max-width:460px; margin-top:.8rem;
  padding:1rem 1.1rem; border:1px solid var(--line); border-radius:12px;
  background:var(--card); }}
details label {{ display:grid; gap:.18rem; font-size:.88em; }}
details input {{ padding:.45rem .55rem; border:1px solid var(--line);
  border-radius:7px; background:var(--bg); color:var(--fg); font-size:1em; }}
.intro {{ color:var(--muted); font-size:.86rem; max-width:70ch; }}
</style></head><body>
<h1>Accor price watch</h1>
<div class="hmeta">{subtitle}</div>
<div class="lastcheck">🕑 Last checked: <span id="stamp">{checked}</span></div>
{changebar}
<p class="intro">{fx_note}<br>
On a drop: <b>book the new rate first, then cancel the old booking</b>.
Dashed line in the trend = your booked price. 💠 lines show the max points
usable (2,000-pt steps) + the cash left to pay.</p>
{savings_bar}
{points_bar}
{controls}
<div class="searchbar">
  <input id="hotelsearch" type="search" placeholder="Search hotel, city, booking number…"
         oninput="filterRows(this.value)">
  <select id="sortsel" onchange="sortCards(this.value)">
    <option value="rec">Sort: ★ recommended</option>
    <option value="plow">Sort: price low → high</option>
    <option value="phigh">Sort: price high → low</option>
    <option value="drop">Sort: best vs booked</option>
    <option value="orig">Sort: my order</option>
  </select>
  <small><span id="rowcount">{len(config["bookings"])}</span> shown</small>
</div>
<script>
function filterRows(q){{q=q.trim().toLowerCase();let n=0;
document.querySelectorAll('.card').forEach(function(c){{
  var show=!q||c.textContent.toLowerCase().indexOf(q)>=0;
  c.style.display=show?'':'none'; if(show)n++;}});
document.getElementById('rowcount').textContent=n;}}
function sortCards(m){{
  var box=document.getElementById('cards');
  var cards=Array.prototype.slice.call(box.children);
  var key={{
    rec:function(c){{return -parseFloat(c.dataset.score||0);}},
    plow:function(c){{return parseFloat(c.dataset.price||9e12)||9e12;}},
    phigh:function(c){{return -(parseFloat(c.dataset.price||0));}},
    drop:function(c){{return parseFloat(c.dataset.diff||9e12);}},
    orig:function(c){{return parseInt(c.dataset.idx||0,10);}}
  }}[m];
  cards.sort(function(a,b){{
    var pa=parseInt(b.dataset.pin||0,10)-parseInt(a.dataset.pin||0,10);
    if(pa) return pa;
    if(a.dataset.pin==='1'){{
      return parseInt(a.dataset.pinrank||0,10)-parseInt(b.dataset.pinrank||0,10);
    }}
    return key(a)-key(b);}});
  cards.forEach(function(c){{box.appendChild(c);}});
}}
document.addEventListener('DOMContentLoaded',function(){{
  var i=0;
  document.querySelectorAll('#cards > .card').forEach(function(c){{
    c.dataset.idx=i++;}});
  sortCards(document.getElementById('sortsel').value);
}});
</script>
<div id="cards">{"".join(body_rows)}</div>
{script}
</body></html>"""
