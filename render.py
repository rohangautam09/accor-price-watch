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
    return (f'<div class="tiny pts">{pts:,} pts + '
            f'{rem_inr.replace(" ≈ ", "") if rem_inr else f"€{rem:,.2f}"}'
            f'</div>')


# ALL Accor+ Explorer subscriber rate.
#
# We fetch logged out, so Accor never shows us the subscriber price — only
# the public rate and the member rate. Comparing a logged-in search against
# an incognito one (same 20 Amsterdam hotels, same dates, 16 Aug 2026) the
# rule was exact at 16 of 17 participating hotels: the price you see is the
# PUBLIC rate less 15%, regardless of whether the member discount on that
# hotel was 5%, 8% or 10%. So the member rate is the wrong benchmark — at a
# 10%-member hotel it sits 5.6% above what you would actually pay.
PLUS_PCT = 15.0
MEMBER_STEPS = (10.0, 8.0, 5.0)   # Accor's published member discounts
# member price key -> the public price key beside it
PAIR = {"inr_member": "inr_standard",
        "inr_bb_member": "inr_bb_standard",
        "inr_nf_member": "inr_nf_standard",
        "inr_nf_bb_member": "inr_nf_bb_standard"}


def split_flat(member, standard):
    """Split a displayed price into (member discount %, non-discountable part).

    Flat city taxes ride inside the total but are never discounted, so
    member = (1-d)·(standard-flat) + flat. Only one of Accor's published
    member discounts leaves `flat` both non-negative and small, and that
    is what identifies d. Verified: ibis budget Brugge resolves to exactly
    EUR 4.20 per person per night, its real Belgian city tax.
    """
    if not member or not standard or member >= standard:
        return None, 0.0
    for d in MEMBER_STEPS:
        flat = (member - (1 - d / 100) * standard) / (d / 100)
        if -0.02 * standard <= flat <= 0.12 * standard:
            return d, max(flat, 0.0)
    return None, 0.0


def accor_plus_price(member, standard, pct=PLUS_PCT):
    """Subscriber price implied by a logged-out member/public pair.

    None when the pair cannot be modelled (no member rate at all, or a
    discount structure we do not recognise) — better to fall back to the
    member rate than to invent a drop.
    """
    d, flat = split_flat(member, standard)
    if d is None or pct <= d:
        return None
    return flat + (standard - flat) * (1 - pct / 100)


def plus_note(cur, b, key="inr_member"):
    """Sub-label under a price: says which rate the headline figure is."""
    if b.get("accor_plus", True) is False:
        return "member"
    m = cur.get(key)
    p = accor_plus_price(m, cur.get(PAIR[key]),
                         float(b.get("accor_plus_pct") or PLUS_PCT))
    if not p or p >= m:
        return "member"
    return f"Accor+ est. · {fmt_inr(m)} member"


def price_box(label, inr_m, inr_s, amount=None, currency="", eur=None,
              fx=None, extra="", empty="–", tax_pct=0.0, prev_val=None,
              note=None):
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
            f'<div class="tiny">{note or "member"} · '
            f'{fmt_inr(inr_s)} standard</div>'
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
                cloud=False, repo=None, workflow=None, failure=None):
    """public=True renders a shareable copy: no booking numbers,
    no points ledger, no cash-at-hotel figures.
    cloud=True renders the full dashboard for GitHub Pages: booking refs
    masked, plus a Refresh button that dispatches the checker workflow."""
    floors = (json.loads(FLOORS_FILE.read_text())
              if FLOORS_FILE.exists() else {})
    rooms_cache = (json.loads(ROOMS_FILE.read_text())
                   if ROOMS_FILE.exists() else {})
    # key by the run timestamp, not the date — several checks can happen in
    # one day and each is its own point on the trend line
    runs = {r.get("checked_at", r["date"]): r for r in history}
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
    # what you will actually pay, and whether points could cover more
    need_pts = 0
    due_eur = pts_on_bookings = 0        # actual, given points already applied
    cash_group = {"eur": 0.0, "n": 0}    # paid entirely at the hotel
    pts_group = {"eur": 0.0, "n": 0}     # part points, rest at the hotel
    booked_n = 0
    for b in config["bookings"]:
        if b.get("status", "booked") != "booked" or not b.get("booked_eur"):
            continue
        booked_n += 1
        used = int(b.get("points_used", 0))
        cap = max_points_for(b["booked_eur"], b.get("city_tax_pct"))
        need_pts += max(cap - used, 0)
        flat = (float(b.get("city_tax_flat_eur") or 0)
                * config["adults"] * int(b["nights"]))
        pts_on_bookings += used
        stay_due = max(b["booked_eur"] - used * 0.02, 0) + flat
        due_eur += stay_due
        if used:
            pts_group["eur"] += stay_due
            pts_group["n"] += 1
        else:
            cash_group["eur"] += stay_due
            cash_group["n"] += 1
    gap = need_pts - remaining_pts

    def covrow(label, pts=None, eur=None, strong=False):
        if pts is not None:
            left, right = f"{pts:,} pts", fmt_inr(pts_inr(pts, fx)) if fx \
                else f"€{pts * 0.02:,.2f}"
        else:
            left = f"€{eur:,.2f}"
            right = fmt_inr(eur * fx) if fx else ""
        val = f"<b>{right}</b>" if strong else right
        return (f'<div class="covrow"><span>{label}</span>'
                f'<span class="covpts">{left}</span><span>{val}</span></div>')

    if not booked_n:
        coverage = ""
    else:
        head = ("Your points cover every booked stay" if gap <= 0 else
                f"Short of covering all {booked_n} booked stay(s)")
        rows = covrow("points needed, on top of the "
                      f"{pts_on_bookings:,} already applied", need_pts)
        if gap <= 0:
            rows += covrow("points left over afterwards",
                           remaining_pts - need_pts, strong=True)
        else:
            rows += covrow("you have", remaining_pts)
            rows += covrow("still short by", gap, strong=True)
        coverage = (f'<div class="cov {"ok" if gap <= 0 else "short"}">'
                    f'<div class="covhead">{head}<small> — if you put the '
                    f'most points Accor allows on all {booked_n} booked '
                    f'stay(s)</small></div>{rows}</div>')

    def money(e):
        return fmt_inr(e * fx) if fx else f"€{e:,.2f}"

    due_rows = ""
    if cash_group["n"]:
        due_rows += (f'<div class="bline duerow"><span>pay in full at the '
                     f'hotel · {cash_group["n"]} stay(s)</span>'
                     f'<span><b>{money(cash_group["eur"])}</b></span></div>')
    if pts_group["n"]:
        due_rows += (f'<div class="bline duerow"><span>points + cash · '
                     f'{pts_group["n"]} stay(s)'
                     f'<br><small>{pts_on_bookings:,} pts applied'
                     f'{f" — worth {fmt_inr(pts_inr(pts_on_bookings, fx))}" if fx else ""}'
                     f'</small></span>'
                     f'<span><b>{money(pts_group["eur"])}</b></span></div>')
    due_bar = "" if (public or not booked_n) else f"""
<div class="duebar">
  <div class="duehead">Cash still to pay at the hotels
    <strong>{money(due_eur)}</strong></div>
  {due_rows}
  <small>across {booked_n} booked stay(s)</small>
</div>"""

    points_bar = "" if public else f"""
<div class="pointsbar">
  <span>ALL points — total: {total_widget}{worth_txt(total_pts)}</span>
  <span>used in bookings: <strong>{used_pts:,}</strong>{worth_txt(used_pts)}</span>
  <span>remaining: <strong>{remaining_pts:,}</strong>{worth_txt(remaining_pts)}</span>
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
            f'<td><small>{mask_ref(e["from"]) if cloud else e["from"]} → '
            f'{mask_ref(e["to"]) if cloud else e["to"]}</small></td>'
            f'<td>€{e["old_eur"]:,.2f} → €{e["new_eur"]:,.2f}</td>'
            f'<td class="{"savepos" if e["saved_eur"] > 0 else ""}">'
            f'{"−" if e["saved_eur"] > 0 else ""}'
            f'€{abs(e["saved_eur"]):,.2f}'
            f'{f" ({fmt_inr(e['saved_eur'] * fx)})" if fx and e["saved_eur"] else ""}'
            f'</td></tr>'
            for e in reversed(ledger))
        savings_bar = f"""
<div class="savingsbar">
  <span>Saved by rebooking: <strong>€{total_eur:,.2f}</strong>
    {f"≈ <strong>{fmt_inr(total_eur * fx)}</strong>" if fx else ""}</span>
  <span><small>{len(wins)} successful rebooking(s) ·
    {len(ledger)} tracked</small></span>
  <details><summary>every rebooking</summary>
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
        status_tag = (f'<span class="stag booked">booked</span>'
                      if is_booked else
                      f'<span class="stag track">tracking</span>')
        pts_on_booking = int(b.get("points_used", 0))
        if b.get("booked_inr") and not public:
            status_tag += (
                f'<span class="stag ptag">{pts_on_booking:,} pts used</span>'
                if pts_on_booking else
                '<span class="stag cash">cash</span>')
            if b.get("points_at_hotel"):
                status_tag += ('<span class="stag hotelpts" title="this hotel '
                               'accepts ALL points at the desk">points '
                               'accepted at hotel</span>')
        cur = None
        if latest:
            cur = next((h for h in latest["hotels"]
                        if h.get("uid", h["code"]) == uid), None)
        wants_bb = bool(b.get("breakfast"))
        cmp_key = "inr_bb_member" if wants_bb else "inr_member"
        # manual app-discount: member prices shown as the app would charge
        app_pct = float(b.get("app_discount_pct") or 0)
        disc = 1 - app_pct / 100
        plus_pct = float(b.get("accor_plus_pct") or PLUS_PCT)
        plus_on = b.get("accor_plus", True) is not False
        plus_seen = [False]

        def ap(v, std=None):
            """A logged-out member price -> what this account actually pays."""
            if v is None:
                return None
            if plus_on and std:
                p = accor_plus_price(v, std, plus_pct)
                if p and p < v:
                    v, plus_seen[0] = p, True
            return v * disc

        def apk(d, key):
            """Adjust one price of a stored run, using its public rate."""
            return ap(d.get(key), d.get(PAIR.get(key)))

        series = []
        for d in dates:
            h = next((x for x in runs[d]["hotels"]
                      if x.get("uid", x["code"]) == uid
                      and "inr_member" in x), None)
            series.append((d, apk(h, cmp_key if cmp_key in h else "inr_member")
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
            inr_m = apk(cur, cmp_key if cmp_key in cur else "inr_member")
            diff = inr_m - eff_booked
            if not b["booked_inr"]:
                badge = '<span class="badge same">watching</span>'
            elif diff < -threshold:
                badge = (f'<span class="badge drop">−{fmt_inr(-diff)} '
                         f'cheaper</span>')
            elif diff > threshold:
                badge = (f'<span class="badge up">+{fmt_inr(diff)} '
                         f'costlier</span>')
            else:
                badge = '<span class="badge same">≈ same</span>'
            prev = cur.get("prev") or {}
            if prev:
                # the stored blob has no public rates, so read the run it
                # points at — the Accor+ estimate needs both halves
                run = runs.get(prev["at"])
                full = next((x for x in (run or {}).get("hotels", [])
                             if x.get("uid", x["code"]) == uid), None)
                if full:
                    prev = {**prev, **full}
                else:
                    # that run was merged away by a later partial check; a
                    # hotel's discount structure holds, so price the old
                    # member rates off today's member/public ratio
                    prev = dict(prev)
                    for mk, sk in PAIR.items():
                        if prev.get(mk) and cur.get(mk) and cur.get(sk):
                            prev[sk] = prev[mk] * cur[sk] / cur[mk]
                at = prev["at"][5:16].replace("T", " ")
                badge += (f'<div class="prevrun same">vs last run '
                          f'({at})</div>')
            anchor = 'h' + uid.replace(':', '-')
            if b["booked_inr"] and diff < -threshold:
                ref = b["booking_no"]
                cancel = ("" if public or ref in ("—", "") else
                          f' — rebook, then cancel '
                          f'{mask_ref(ref) if cloud else ref}')
                summary_drops.append(
                    f'<a class="ci cidrop" href="#{anchor}">{b["name"]}: '
                    f'{fmt_inr(-diff)} below your booked price '
                    f'(now {fmt_inr(inr_m)}){cancel}</a>')
            pv0 = apk(prev, cmp_key if cmp_key in prev else "inr_member")
            if pv0:
                pd0 = inr_m - pv0
                if pd0 < -threshold:
                    summary_moves.append(
                        f'<a class="ci cidn" href="#{anchor}">{b["name"]}: '
                        f'{fmt_inr(-pd0)} down since last run</a>')
                elif pd0 > threshold:
                    summary_moves.append(
                        f'<a class="ci ciup" href="#{anchor}">{b["name"]}: '
                        f'{fmt_inr(pd0)} up since last run</a>')
            currency = cur.get("currency") or ""
            eur_m = cur.get("eur_member")
            eur_bb = cur.get("eur_bb_member")
            if currency == "EUR":  # older history entries lack eur_* fields
                eur_m = eur_m or cur.get("member_amount")
                eur_bb = eur_bb or cur.get("bb_member_amount")
            tax_pct = float(b.get("city_tax_pct") or 0)

            def shrink(raw, key):
                """Scale a native-currency amount the same way its INR
                twin was scaled, so EUR and points stay consistent."""
                base = cur.get(key)
                adj = apk(cur, key)
                return raw * adj / base if raw and base and adj else raw

            flex_box = price_box("Flexible", apk(cur, "inr_member"),
                                 cur.get("inr_standard"),
                                 cur.get("member_amount"), currency,
                                 shrink(eur_m, "inr_member"), fx,
                                 tax_pct=tax_pct, note=plus_note(cur, b),
                                 prev_val=apk(prev, "inr_member"))
            bb_box = price_box("Flexible + breakfast",
                               apk(cur, "inr_bb_member"),
                               cur.get("inr_bb_standard"),
                               cur.get("bb_member_amount"), currency,
                               shrink(eur_bb, "inr_bb_member"), fx,
                               empty="not offered", tax_pct=tax_pct,
                               note=plus_note(cur, b, "inr_bb_member"),
                               prev_val=apk(prev, "inr_bb_member"))
            eur_nf = cur.get("eur_nf_member")
            eur_nf_bb = cur.get("eur_nf_bb_member")
            if currency == "EUR":
                eur_nf = eur_nf or cur.get("nf_member_amount")
                eur_nf_bb = eur_nf_bb or cur.get("nf_bb_member_amount")
            nf_box = price_box("Non-flexible", apk(cur, "inr_nf_member"),
                               cur.get("inr_nf_standard"),
                               cur.get("nf_member_amount"), currency,
                               shrink(eur_nf, "inr_nf_member"), fx,
                               empty="not offered", tax_pct=tax_pct,
                               note=plus_note(cur, b, "inr_nf_member"),
                               prev_val=apk(prev, "inr_nf_member"))
            nf_bb_box = price_box("Non-flex + breakfast",
                                  apk(cur, "inr_nf_bb_member"),
                                  cur.get("inr_nf_bb_standard"),
                                  cur.get("nf_bb_member_amount"), currency,
                                  shrink(eur_nf_bb, "inr_nf_bb_member"), fx,
                                  empty="not offered", tax_pct=tax_pct,
                                  note=plus_note(cur, b, "inr_nf_bb_member"),
                                  prev_val=apk(prev, "inr_nf_bb_member"))
            room_name = cur.get("room_name", cur.get("room", ""))

            # recommendation score (0–100): affordability, location,
            # breakfast economics, value vs floor, and beating the booking
            floor = floors.get(f'{b["code"]}:{b["dateIn"]}')
            over_floor = ((cur["inr_member"] / floor["inr"] - 1) * 100
                          if floor else None)
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
            bb_now = apk(cur, "inr_bb_member")
            if bb_now and cur.get("inr_member"):
                prem = (bb_now - apk(cur, "inr_member")) / max(b["nights"], 1)
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
        meal_txt = "breakfast included" if wants_bb else "room only"
        if b["booked_inr"]:
            flat_eur = (float(b.get("city_tax_flat_eur") or 0)
                        * config["adults"] * int(b["nights"]))
            cap = max_points_for(b["booked_eur"] or 0, b.get("city_tax_pct"))
            rows = [f'<div class="tiny">€{b["booked_eur"]:,.2f} · {meal_txt}'
                    f'</div>' if b.get("booked_eur") else
                    f'<div class="tiny">{meal_txt}</div>']
            if not public and b.get("booked_eur"):
                cash_eur = max(b["booked_eur"] - b_pts * 0.02, 0) + flat_eur
                tax_note = (f' <span class="tiny">incl. '
                            f'{fmt_inr(flat_eur * fx) if fx else f"€{flat_eur:,.2f}"}'
                            f' city tax</span>' if flat_eur else "")
                if is_booked and b_pts:
                    rows.append(
                        f'<div class="bline"><span>{b_pts:,} pts</span>'
                        f'<span class="pts">−{fmt_inr(pts_inr(b_pts, fx))}'
                        f'</span></div>')
                if is_booked:
                    rows.append(
                        f'<div class="bline"><span>pay at hotel</span>'
                        f'<span><b>{fmt_inr(cash_eur * fx) if fx else f"€{cash_eur:,.2f}"}'
                        f'</b></span></div>{tax_note}')
                if not b_pts and cap:
                    with_cash = (b["booked_eur"] - cap * 0.02 + flat_eur)
                    hint = ("with points: " if is_booked
                            else "if booked with points: ")
                    rows.append(
                        f'<div class="tiny pts">{hint}{cap:,} pts + '
                        f'{fmt_inr(with_cash * fx) if fx else f"€{with_cash:,.2f}"}'
                        + (" — needs rebooking</div>" if is_booked
                           else "</div>"))
            booked_box = (f'<div class="pbox"><div class="plabel">'
                          f'{"Booked" if is_booked else "Noted price"}</div>'
                          f'<div class="pval">{fmt_inr(eff_booked)}</div>'
                          f'{"".join(rows)}</div>')
        else:
            booked_box = price_box("Booked", None, None, empty="not booked")
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
                          f'{"unpin" if pinned else "pin"}</button>'
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
                <label style="grid-auto-flow:column; justify-content:start;
                              align-items:center; gap:.4rem">
                  <input type="checkbox" name="points_at_hotel"
                         {"checked" if b.get("points_at_hotel") else ""}
                         style="width:auto"> Hotel accepts points at the
                  desk</label>
                <label style="grid-auto-flow:column; justify-content:start;
                              align-items:center; gap:.4rem">
                  <input type="checkbox" name="accor_plus"
                         {"" if b.get("accor_plus") is False else "checked"}
                         style="width:auto"> Accor+ Explorer rate here
                  (public −{plus_pct:g}%)</label>
                <button type="submit" class="primary">Save</button>
              </form></details>"""
        booking_line = ("booked" if public else
                        f'booking {mask_ref(b["booking_no"]) if cloud else b["booking_no"]}'
                        ) if b["booked_inr"] else "not booked — watching"
        city_part = f'{b["city"]} · ' if b.get("city") else ""
        room_part = (f'<div class="hmeta">room: {room_name}</div>'
                     if room_name else "")
        insights_html = ""
        trend = spark(series, eff_booked)
        trend_html = f'<div class="trend">{trend}</div>' if trend else ""
        body_rows.append(f"""<div class="card{' pinned' if pinned else ''}"
  id="h{uid.replace(':', '-')}" data-pin="{1 if pinned else 0}"
  data-pinrank="{int(b.get('pin_rank', 0) or 0)}"
  data-score="{(score or 0):.1f}" data-price="{(inr_m or 0):.0f}"
  data-diff="{diff if diff is not None and b["booked_inr"] else 9e12:.0f}">
  <div class="chead">
    <div class="cinfo">
      <div class="hname">{'<span class="pinmark">◆</span> ' if pinned else ''}{b["name"]}{status_tag}{f'<span class="stag plustag" title="you are not shown the subscriber rate logged out — this is the public rate less {plus_pct:g}%">Accor+ est.</span>' if plus_seen[0] else ""}{f'<span class="stag apptag">{app_pct:g}% app rate</span>' if app_pct else ""}</div>
      <div class="hmeta">{city_part}{fmt_dates(b["dateIn"], b["nights"])} ·
        {b["nights"]} night(s) · {booking_line}{f' · {dist_km:.1f} km from centre' if dist_km is not None else ''}</div>
      {room_part}
    </div>
    <div class="cright">{badge}{f'<div class="score" title="{score_title}">{score:.0f}</div>' if score is not None else ''}{trend_html}</div>
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

    failbar = (f'<div class="failbar">⚠️ The last automatic check could '
               f'not fetch prices ({failure}). The figures below are from '
               f'the previous successful check.</div>' if failure else "")
    if summary_drops or summary_moves:
        changebar = ('<div class="changebar">'
                     + "".join(summary_drops + summary_moves) + "</div>")
    elif latest:
        changebar = (f'<div class="nochange"><span class="dot"></span>'
                     f'<b>No change</b>'
                     f'<span>nothing moved more than {fmt_inr(threshold)} '
                     f'since the last run</span></div>')
    else:
        changebar = ""
    # the two rates every figure on this page is built from — kept big and
    # near the top, because everything else is meaningless without them
    fx_src = (latest or {}).get("fx_source")
    if fx:
        fx_cap = ("Accor's own conversion rate, so every ₹ here matches "
                  "their pages" if fx_src == "accor" else
                  "market rate — Accor converts at its own, slightly "
                  "different rate")
        rate_bar = (f'<div class="ratebar">'
                    f'<div class="rate"><b>₹{fx:,.4f}</b>'
                    f'<span>per €1</span></div>'
                    f'<div class="rate"><b>₹{0.02 * fx:,.2f}</b>'
                    f'<span>per ALL point</span></div>'
                    f'<div class="rcap">2,000 pts = €40 ≈ {fmt_inr(40 * fx)} '
                    f'· {fx_cap} · refreshed on every price check</div></div>')
    else:
        rate_bar = ('<div class="ratebar"><div class="rcap">FX rate '
                    'unavailable — ₹ conversion skipped</div></div>')

    controls = ""
    script = ""
    if cloud:
        controls = f"""
<div class="controls">
  <button id="checkbtn" class="primary" onclick="cloudRefresh()">refresh prices</button>
  <span id="status"></span>
  <div id="setup" class="setupbox" hidden>
    <b>One-time setup for the refresh button</b>
    <p style="margin:.4rem 0 .6rem">Refreshing runs the price checker on
    GitHub's servers. GitHub needs a key to confirm it is really you, so:</p>
    <ol style="margin:0 0 .7rem 1.1rem;padding:0">
      <li><a href="https://github.com/settings/tokens/new?scopes=repo,workflow&amp;description=Accor%20price%20watch"
             target="_blank">tap here to create the key</a> (the right
          options are pre-ticked) — scroll down, tap
          <b>Generate token</b>, then copy it</li>
      <li>paste it below and tap <b>save</b></li>
    </ol>
    <input id="pat" type="password" placeholder="paste the key here"
           autocomplete="off">
    <button class="primary" onclick="savePat()">save</button>
    <button class="link" onclick="document.getElementById('setup').hidden=true">
      close</button>
    <p style="margin:.7rem 0 0"><small>The key is stored only in this
    browser and never published. Prefer not to?
    <a href="https://github.com/{repo}/actions/workflows/{workflow}"
       target="_blank">run the check on GitHub instead</a>.</small></p>
  </div>
</div>"""
        script = f"""
<script>
const REPO="{repo}", WF="{workflow}";
function savePat(){{
  const v=document.getElementById('pat').value.trim();
  if(!v){{ return; }}
  localStorage.setItem('accor_pat', v);
  document.getElementById('pat').value='';
  document.getElementById('setup').hidden=true;
  document.getElementById('status').textContent='Key saved — tap Refresh prices.';
}}
async function cloudRefresh(){{
  const t=localStorage.getItem('accor_pat');
  const st=document.getElementById('status');
  if(!t){{ document.getElementById('setup').hidden=false;
    st.textContent='One-time setup needed \u2193'; return; }}
  const btn=document.getElementById('checkbtn'); btn.disabled=true;
  const t0=Date.now();
  st.textContent='Asking GitHub to fetch fresh prices\u2026';
  let r;
  try{{
    r=await fetch('https://api.github.com/repos/'+REPO+'/actions/workflows/'+WF+'/dispatches',
      {{method:'POST',headers:{{'Authorization':'Bearer '+t,
        'Accept':'application/vnd.github+json'}},
        body:JSON.stringify({{ref:'main'}})}});
  }}catch(e){{ btn.disabled=false; st.textContent='Network error — try again.'; return; }}
  if(r.status===401||r.status===403){{ btn.disabled=false;
    localStorage.removeItem('accor_pat');
    document.getElementById('setup').hidden=false;
    st.textContent='That key was rejected — create a new one below.'; return; }}
  if(!r.ok){{ btn.disabled=false;
    st.textContent='GitHub said '+r.status+' — try again in a minute.'; return; }}
  const started=document.getElementById('stamp').textContent;
  const poll=setInterval(async()=>{{
    const secs=Math.round((Date.now()-t0)/1000);
    st.textContent='Fetching live prices\u2026 '+secs+'s';
    if(secs>25){{
      try{{
        const html=await (await fetch(location.pathname+'?t='+Date.now())).text();
        if(html.indexOf(started)<0){{ clearInterval(poll); location.reload(); }}
      }}catch(e){{}}
    }}
    if(secs>240){{ clearInterval(poll); btn.disabled=false;
      st.textContent='Still working — reload the page in a minute.'; }}
  }},5000);
}}
</script>"""
    if interactive:
        controls = """
<div class="controls">
  <button id="checkbtn" class="primary" onclick="runCheck()">check prices</button>
  <details id="addbox"><summary>add a hotel</summary>
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
  <details><summary>settings</summary>
    <form onsubmit="return saveSettings(event)">
      <label>Scan ± days around each check-in for cheaper start dates
        (0 = off, max 14)
        <input type="number" name="date_scan_days" min="0" max="14"
               value="__SCAN_DAYS__" style="max-width:6em"></label>
      <button type="submit" class="primary">Save</button>
    </form>
  </details>
  <span id="status"></span>
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
:root {{ --bg:#fbfbfc; --card:#ffffff; --box:#fafafb; --fg:#17181b;
  --muted:#8a8f98; --line:#ebecef; --accent:#2f6df6;
  --drop:#177245; --up:#a5372c; }}
@media (prefers-color-scheme: dark) {{
  :root {{ --bg:#0c0d0f; --card:#141518; --box:#191a1e; --fg:#e9eaec;
    --muted:#8b9099; --line:#25272c; --accent:#6ea8fe;
    --drop:#4ec07f; --up:#e0776a; }} }}
* {{ box-sizing:border-box; }}
body {{ font:15px/1.55 -apple-system, system-ui, "Segoe UI", sans-serif;
  background:var(--bg); color:var(--fg); max-width:1360px;
  margin:0 auto; padding:2rem 1.4rem 4rem; letter-spacing:0;
  font-optical-sizing:auto; -webkit-font-smoothing:antialiased; }}
/* larger type reads too loose: tighten tracking and leading as size grows */
h1 {{ font-size:1.6rem; margin:0 0 .15rem; letter-spacing:-.021em;
  line-height:1.1; font-weight:680; }}
.hname {{ letter-spacing:-.012em; line-height:1.25; }}
.pval {{ letter-spacing:-.015em; font-variant-numeric:tabular-nums; }}
.tiny, .hmeta {{ letter-spacing:.004em; }}   /* small text wants air */
small {{ color:var(--muted); }}
a {{ color:var(--accent); text-decoration:none; }}
a:hover {{ text-decoration:underline; }}
.pts {{ color:color-mix(in srgb, var(--accent) 80%, var(--muted)); }}
.card {{ background:var(--card); border:1px solid var(--line);
  border-radius:12px; padding:1.1rem 1.15rem; margin-bottom:.6rem; }}
.card:hover {{ border-color:color-mix(in srgb, var(--fg) 16%, var(--line)); }}
.card.pinned {{ border-color:color-mix(in srgb, var(--fg) 22%, var(--line)); }}
.pinmark {{ color:var(--muted); font-size:.7em; vertical-align:2px; }}
.chead {{ display:flex; justify-content:space-between; gap:1rem;
  align-items:flex-start; flex-wrap:wrap; }}
.cinfo {{ min-width:0; flex:1 1 260px; }}
.hname {{ overflow-wrap:anywhere; }}
.hname {{ font-size:1.06rem; font-weight:650; letter-spacing:-.01em; }}
.hmeta {{ color:var(--muted); font-size:.84rem; margin-top:.1rem; }}
.stag {{ margin-left:.5rem; padding:.1rem .45rem; border-radius:5px;
  font-size:.7rem; font-weight:600; vertical-align:2px; white-space:nowrap;
  letter-spacing:.02em; text-transform:lowercase;
  border:1px solid var(--line); color:var(--muted); background:none; }}
.stag.booked {{ color:var(--drop);
  border-color:color-mix(in srgb, var(--drop) 35%, var(--line)); }}
.stag.track {{ color:var(--muted); }}
.stag.ptag {{ color:var(--accent);
  border-color:color-mix(in srgb, var(--accent) 35%, var(--line)); }}
.stag.hotelpts {{ color:var(--accent); border-style:dashed;
  border-color:color-mix(in srgb, var(--accent) 45%, var(--line)); }}
.stag.cash {{ color:var(--muted); }}
.stag.plustag {{ color:var(--drop); border-style:dashed;
  border-color:color-mix(in srgb, var(--drop) 40%, var(--line)); }}
.cov {{ margin-top:.6rem; padding-top:.5rem; font-size:.88rem;
  border-top:1px solid var(--line); }}
.cov.ok .covhead {{ color:var(--drop); }}
.cov.short .covhead {{ color:var(--up); }}
.covhead {{ margin-bottom:.35rem; }}
.covhead small {{ color:var(--muted); }}
.covrow {{ display:grid; grid-template-columns:1fr auto auto;
  gap:.4rem 1.1rem; padding:.18rem 0; color:var(--muted); }}
.covrow .covpts {{ font-variant-numeric:tabular-nums; color:var(--fg);
  text-align:right; }}
.covrow > span:last-child {{ font-variant-numeric:tabular-nums;
  text-align:right; min-width:5.5em; }}
.covrow b {{ color:var(--fg); }}
details select {{ padding:.45rem .55rem; border:1px solid var(--line);
  border-radius:7px; background:var(--bg); color:var(--fg); font-size:1em; }}

.cright {{ text-align:right; flex:0 0 auto; max-width:100%;
  display:flex; flex-direction:column; align-items:flex-end; gap:.15rem; }}
.trend {{ margin-top:.25rem; opacity:.55; }}
.cright {{ gap:.1rem; }}
.prices {{ display:grid;
  grid-template-columns:repeat(auto-fit, minmax(185px, 1fr));
  gap:.55rem; margin:.85rem 0 .15rem; }}
.pbox {{ background:none; border:1px solid var(--line); border-radius:9px;
  padding:.55rem .75rem .6rem; }}
.plabel {{ font-size:.67rem; font-weight:500; text-transform:uppercase;
  letter-spacing:.08em; color:var(--muted); margin-bottom:.25rem; }}
.pval {{ font-size:1.22rem; font-weight:680; letter-spacing:-.01em; }}
.pval.dim {{ color:var(--muted); font-size:.95rem; font-weight:500; }}
.tiny {{ font-size:.79rem; color:var(--muted); margin-top:.12rem; }}
.bline {{ display:flex; justify-content:space-between; gap:.6rem;
  font-size:.84rem; margin-top:.28rem; }}
.bline span:first-child {{ color:var(--muted); }}
.insights {{ display:flex; flex-wrap:wrap; gap:.4rem; margin:.6rem 0 .1rem; }}
.insights span {{ font-size:.8rem; padding:.28rem .65rem; border-radius:8px;
  background:color-mix(in srgb, var(--accent) 8%, transparent);
  color:color-mix(in srgb, var(--fg) 82%, var(--accent)); }}
.insights b {{ color:var(--accent); }}
.cactions {{ display:flex; flex-wrap:wrap; gap:1.1rem; align-items:baseline;
  margin-top:.65rem; font-size:.86rem; }}
.badge {{ padding:.15rem .5rem; border-radius:6px; font-size:.82em;
  white-space:nowrap; font-weight:600; background:none;
  border:1px solid var(--line); }}
.prevrun {{ display:inline-block; margin-top:.3rem; font-size:.74rem;
  white-space:nowrap; color:var(--muted); }}
.failbar {{ margin:.5rem 0 .8rem; padding:.6rem .9rem;
  border-radius:10px; font-size:.9rem;
  background:color-mix(in srgb, var(--up) 12%, transparent);
  color:color-mix(in srgb, var(--up) 85%, var(--fg)); }}
.changebar {{ display:flex; flex-direction:column; gap:.35rem;
  margin:.4rem 0 .8rem; }}
/* the answer to "did anything move?" — readable at a glance, but calm,
   because "nothing happened" should never look like an alert */
.nochange {{ display:flex; align-items:center; flex-wrap:wrap; gap:.25rem .6rem;
  margin:.4rem 0 .8rem; padding:.75rem 1.05rem; border-radius:12px;
  background:var(--card); border:1px solid var(--line); font-size:.98rem; }}
.nochange b {{ font-weight:600; }}
.nochange > span:last-child {{ color:var(--muted); font-size:.9rem; }}
.nochange .dot {{ width:.5rem; height:.5rem; border-radius:50%; flex:none;
  background:var(--muted); }}
.ci {{ display:block; padding:.5rem .9rem; border-radius:9px;
  font-size:.92rem; text-decoration:none; border:1px solid var(--line); }}
.ci:hover {{ text-decoration:none; filter:brightness(1.05); }}
.ci.cidrop {{ color:var(--drop); font-weight:600;
  border-color:color-mix(in srgb, var(--drop) 40%, var(--line)); }}
.ci.cidn {{ color:var(--drop); }}
.ci.ciup {{ color:var(--muted); }}
.card {{ scroll-margin-top:1rem; }}
.d {{ font-size:.74rem; font-weight:650; vertical-align:2px;
  white-space:nowrap; }}
.d.dn {{ color:var(--drop); }}
.d.rs {{ color:var(--up); }}
.d.sm {{ color:var(--muted); font-weight:500; }}
.drop {{ color:var(--drop);
  border-color:color-mix(in srgb, var(--drop) 40%, var(--line)); }}
.up {{ color:var(--up); }}
.same, .err {{ color:var(--muted); font-weight:500; }}
.controls {{ margin:1.1rem 0 .2rem; display:flex; flex-wrap:wrap;
  align-items:start; gap:.5rem; }}
.controls > * {{ margin:0; }}
.controls > details {{ margin:0; }}
.controls > details > summary {{ list-style:none; cursor:pointer;
  display:flex; align-items:center; height:2.25rem; padding:0 .95rem;
  border:1px solid var(--line); border-radius:9px;
  font-size:.92rem; color:var(--fg); background:var(--card);
  white-space:nowrap; }}
.controls > details > summary::-webkit-details-marker {{ display:none; }}
.controls > details > summary:hover {{ border-color:color-mix(in srgb,
  var(--fg) 25%, var(--line)); }}
.controls > details[open] > summary {{ border-color:color-mix(in srgb,
  var(--accent) 45%, var(--line)); color:var(--accent); }}
.controls #status {{ flex-basis:100%; margin:.15rem 0 0; }}
.duehead {{ display:flex; justify-content:space-between; gap:1rem;
  align-items:baseline; font-weight:600; margin-bottom:.5rem; }}
.duehead strong {{ font-size:1.15rem; }}
.duerow {{ font-size:.9rem; padding:.35rem 0;
  border-top:1px solid color-mix(in srgb, var(--accent) 18%, transparent); }}
.duebar {{ margin:1rem 0 .5rem; padding:.9rem 1.1rem; border-radius:12px;
  background:var(--card); border:1px solid var(--line); font-size:1rem; }}
.savingsbar {{ margin:.5rem 0; padding:.9rem 1.1rem; border-radius:12px;
  background:var(--card); border:1px solid var(--line); }}
.savingsbar > span {{ margin-right:1.4rem; }}
.savingsbar summary {{ color:var(--drop); }}
table.ledger {{ border-collapse:collapse; width:100%; margin-top:.7rem;
  font-size:.86rem; }}
table.ledger th, table.ledger td {{ text-align:left; padding:.35rem .6rem;
  border-bottom:1px solid var(--line); white-space:nowrap; }}
table.ledger th {{ color:var(--muted); font-weight:600; font-size:.78rem;
  text-transform:uppercase; letter-spacing:.05em; }}
td.savepos {{ color:var(--drop); font-weight:650; }}
.ratebar {{ margin:.9rem 0; padding:.85rem 1.1rem; background:var(--card);
  border:1px solid var(--line); border-radius:12px; display:flex;
  flex-wrap:wrap; align-items:baseline; gap:.3rem 2.4rem; }}
.rate {{ display:flex; align-items:baseline; gap:.55rem; }}
.rate b {{ font-size:1.3rem; font-weight:600; letter-spacing:-.01em;
  font-variant-numeric:tabular-nums; }}
.rate span {{ color:var(--muted); font-size:.87rem; }}
.rcap {{ flex-basis:100%; color:var(--muted); font-size:.8rem; }}
.pointsbar {{ margin:.5rem 0; padding:.9rem 1.1rem;
  border:1px solid var(--line); border-radius:12px; background:var(--card); }}
.pointsbar > span {{ margin-right:1.5rem; white-space:nowrap; }}
.pointsbar input {{ padding:.3rem .45rem; border:1px solid var(--line);
  border-radius:7px; background:var(--bg); color:var(--fg); font-size:1em; }}
.chip {{ display:inline-block; padding:.12rem .6rem; margin:.15rem .25rem 0 0;
  border:1px solid var(--line); border-radius:99px; white-space:nowrap;
  background:var(--box); }}
.lastcheck {{ display:inline-block; margin:.45rem 0 .25rem;
  font-size:.85rem; color:var(--muted); font-variant-numeric:tabular-nums; }}
.searchbar {{ margin:.9rem 0 1.1rem; display:flex; align-items:center;
  gap:.8rem; position:sticky; top:0; z-index:5;
  padding:.6rem .7rem; border-radius:14px;
  background:color-mix(in srgb, var(--bg) 72%, transparent);
  backdrop-filter:blur(20px) saturate(180%);
  -webkit-backdrop-filter:blur(20px) saturate(180%); }}
/* scroll edge effect instead of a hard divider under floating chrome */
.searchbar::after {{ content:""; position:absolute; left:0; right:0;
  bottom:-14px; height:14px; pointer-events:none;
  background:linear-gradient(color-mix(in srgb, var(--bg) 70%, transparent),
    transparent); }}
.searchbar input {{ flex:1; max-width:430px; padding:.55rem .85rem;
  border:1px solid var(--line); border-radius:10px; background:var(--card);
  color:var(--fg); font-size:1em; }}
.searchbar input:focus {{ outline:2px solid
  color-mix(in srgb, var(--accent) 45%, transparent); border-color:transparent; }}
.searchbar select {{ padding:.5rem .7rem; border:1px solid var(--line);
  border-radius:10px; background:var(--card); color:var(--fg);
  font-size:.92em; cursor:pointer; }}
.score {{ display:inline-block; margin:.3rem 0 0 .5rem; font-size:.75rem;
  font-weight:600; cursor:help; color:var(--muted);
  font-variant-numeric:tabular-nums; }}
.score::after {{ content:" / 100"; font-weight:400; opacity:.6; }}
details.rowedit {{ margin:0; }}
details.rowedit summary {{ font-size:.86rem; }}
details.rowedit form {{ max-width:250px; padding:.75rem; }}
button.primary {{ background:var(--fg); color:var(--bg); border:none;
  height:2.25rem; padding:0 1.1rem; border-radius:9px; font-size:.92rem;
  font-weight:600; cursor:pointer; letter-spacing:.01em; }}
button.primary:hover {{ filter:brightness(1.08); }}
button.primary:active {{ transform:scale(.97); }}
button.link:active {{ opacity:.55; }}
.ci:active {{ transform:scale(.995); }}
button, .ci, summary {{ -webkit-tap-highlight-color:transparent;
  transition:transform 100ms ease-out, opacity 100ms ease-out; }}
button.primary:disabled {{ opacity:.5; cursor:wait; }}
button.link {{ background:none; border:none; padding:0; cursor:pointer;
  font-size:.86rem; color:var(--accent); }}
button.link:hover {{ text-decoration:underline; }}
button.link.danger {{ color:var(--up); }}
#status {{ color:var(--muted); font-size:.88rem; min-height:1.2em;
  display:block; }}
details summary {{ cursor:pointer; color:var(--accent); }}
details form {{ display:grid; gap:.6rem; max-width:460px; margin-top:.8rem;
  padding:1rem 1.1rem; border:1px solid var(--line); border-radius:12px;
  background:var(--card); transform-origin:top left; }}
details[open] > form, details[open] > .setupbox, details[open] > table {{
  animation:materialize .28s cubic-bezier(.32,.72,0,1); }}
@keyframes materialize {{
  from {{ opacity:0; transform:scale(.97) translateY(-4px); filter:blur(3px); }}
  to {{ opacity:1; transform:none; filter:none; }} }}
details label {{ display:grid; gap:.18rem; font-size:.88em; }}
details input {{ padding:.45rem .55rem; border:1px solid var(--line);
  border-radius:7px; background:var(--bg); color:var(--fg); font-size:1em; }}
.intro {{ color:var(--muted); font-size:.86rem; max-width:70ch; }}
.setupbox {{ margin-top:.8rem; padding:1rem 1.1rem; border-radius:12px;
  background:var(--card); border:1px solid var(--line); max-width:560px;
  font-size:.9rem; }}
.setupbox input {{ padding:.5rem .6rem; border:1px solid var(--line);
  border-radius:8px; background:var(--bg); color:var(--fg); min-width:220px;
  margin-right:.4rem; }}
@media (prefers-reduced-motion: reduce) {{
  * {{ animation-duration:.01ms !important; animation-iteration-count:1 !important;
       transition-duration:120ms !important; }}
  details[open] > form, details[open] > .setupbox {{ animation:none; }}
}}
@media (prefers-reduced-transparency: reduce) {{
  .searchbar {{ background:var(--bg); backdrop-filter:none;
    -webkit-backdrop-filter:none; }}
  .searchbar::after {{ display:none; }}
}}
@media (prefers-contrast: more) {{
  .card {{ border-color:var(--fg); }}
  .pbox {{ border-color:color-mix(in srgb, var(--fg) 45%, transparent); }}
  .searchbar {{ background:var(--bg); backdrop-filter:none; }}
}}
@media (max-width: 760px) {{
  body {{ padding:1.2rem .8rem 3rem; }}
  h1 {{ font-size:1.35rem; }}
  .card {{ padding:.9rem .85rem; border-radius:14px; }}
  .chead {{ flex-direction:column; gap:.5rem; }}
  /* in column flow a flex-basis would become a height — reset it */
  .cinfo {{ flex:0 0 auto; width:100%; }}
  .cright {{ flex-direction:row; align-items:center; flex-wrap:wrap;
    justify-content:flex-start; text-align:left; gap:.4rem; width:100%; }}
  .prevrun, .score {{ margin:0; }}
  .trend {{ margin:0; }}
  .prices {{ grid-template-columns:1fr 1fr; gap:.45rem; }}
  .pbox {{ padding:.5rem .6rem; }}
  .pval {{ font-size:1.05rem; }}
  .tiny {{ font-size:.74rem; }}
  .insights span {{ font-size:.75rem; }}
  .pointsbar > span, .savingsbar > span {{ display:block;
    margin:0 0 .3rem; white-space:normal; }}
  .searchbar {{ flex-wrap:wrap; }}
  .searchbar input {{ max-width:none; flex:1 1 100%; }}
  table.ledger {{ display:block; overflow-x:auto; }}
}}
@media (max-width: 430px) {{
  .prices {{ grid-template-columns:1fr; }}
}}
</style></head><body>
<h1>Accor price watch</h1>
<div class="hmeta">{subtitle}</div>
<div class="lastcheck">Last checked: <span id="stamp">{checked}</span></div>
{failbar}
{changebar}
{rate_bar}
<p class="intro">on a drop, book the new rate before cancelling the old
one.</p>
{due_bar}
{savings_bar}
{points_bar}
{controls}
<div class="searchbar">
  <input id="hotelsearch" type="search" placeholder="Search hotel, city, booking number…"
         oninput="filterRows(this.value)">
  <select id="sortsel" onchange="sortCards(this.value)">
    <option value="rec">sort: recommended</option>
    <option value="plow">sort: price ↑</option>
    <option value="phigh">sort: price ↓</option>
    <option value="drop">sort: best vs booked</option>
    <option value="orig">sort: my order</option>
  </select>
  <small><span id="rowcount">{len(config["bookings"])}</span> shown</small>
</div>
<script>
function filterRows(q){{q=q.trim().toLowerCase();let n=0;
document.querySelectorAll('.card').forEach(function(c){{
  var show=!q||c.textContent.toLowerCase().indexOf(q)>=0;
  c.style.display=show?'':'none'; if(show)n++;}});
document.getElementById('rowcount').textContent=n;}}
/* critically damped spring (damping 1.0, response .4) sampled into a
   linear() easing — springs settle from the current value, so a re-sort
   mid-flight is picked up rather than jumping */
function springEasing(response, damping, steps){{
  response=response||0.4; damping=damping===undefined?1:damping; steps=steps||60;
  var w=2*Math.PI/response, out=[], T=response*2.2;
  for(var i=0;i<=steps;i++){{
    var t=T*i/steps, v;
    if(damping>=1){{ v=1-(1+w*t)*Math.exp(-w*t); }}
    else {{ var wd=w*Math.sqrt(1-damping*damping);
      v=1-Math.exp(-damping*w*t)*(Math.cos(wd*t)+damping*w/wd*Math.sin(wd*t)); }}
    out.push(v.toFixed(4));
  }}
  return 'linear('+out.join(',')+')';
}}
var SPRING=null, SPRING_MS=880;
function flipReorder(box, reorder){{
  var reduce=window.matchMedia('(prefers-reduced-motion: reduce)').matches;
  var kids=Array.prototype.slice.call(box.children);
  var first={{}};
  kids.forEach(function(c,i){{ first[i]=c.getBoundingClientRect().top; c.dataset.flip=i; }});
  reorder();
  if(reduce) return;
  if(!SPRING) SPRING=springEasing(0.4,1,60);
  Array.prototype.slice.call(box.children).forEach(function(c){{
    var from=first[c.dataset.flip], to=c.getBoundingClientRect().top, dy=from-to;
    if(Math.abs(dy)<1) return;
    /* start from the live on-screen position, cancelling any in-flight move */
    var running=c.getAnimations().filter(function(a){{return a.id==='flip';}});
    running.forEach(function(a){{ a.cancel(); }});
    var a=c.animate([{{transform:'translateY('+dy+'px)'}},{{transform:'none'}}],
      {{duration:SPRING_MS, easing:SPRING, composite:'replace'}});
    a.id='flip';
  }});
}}
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
  flipReorder(box, function(){{
    cards.sort(function(a,b){{
      var pa=parseInt(b.dataset.pin||0,10)-parseInt(a.dataset.pin||0,10);
      if(pa) return pa;
      if(a.dataset.pin==='1'){{
        return parseInt(a.dataset.pinrank||0,10)-parseInt(b.dataset.pinrank||0,10);
      }}
      return key(a)-key(b);}});
    cards.forEach(function(c){{box.appendChild(c);}});
  }});
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
