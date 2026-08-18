"""Accor price-drop watcher.

For each booking in config.json, loads the all.accor.com booking page in
headless Chromium, captures the GraphQL offers response, and extracts the
cheapest FLEXIBLE (free-cancellation) rate. Compares against the booked INR
total, appends to history.json, regenerates dashboard.html, and sends a macOS
notification when a price drops below the booked total by more than the
configured threshold.

Read-only: never books or cancels anything.
"""
import asyncio
import datetime as dt
import json
import os
import pathlib
import subprocess
import time
import urllib.request

from playwright.async_api import async_playwright

from render import (BOOKING_URL, PAIR, PLUS_PCT, accor_plus_price, fmt_inr,
                    render_page)

BASE = pathlib.Path(__file__).parent
CONFIG = json.loads((BASE / "config.json").read_text())
HISTORY_FILE = BASE / "history.json"
DASHBOARD_FILE = BASE / "dashboard.html"

UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/149.0.0.0 Safari/537.36")

def get_fx_rates():
    """All exchange rates with EUR base ({'INR': 109.1, 'THB': 38.9, ...});
    None if unavailable."""
    try:
        with urllib.request.urlopen(
                "https://open.er-api.com/v6/latest/EUR", timeout=15) as r:
            data = json.load(r)
        return {k: float(v) for k, v in data["rates"].items()}
    except Exception:
        return None


def extract_offers(bodies):
    """Find the hotelOffers GraphQL payload among captured JSON bodies."""
    for body in bodies:
        try:
            d = json.loads(body)
        except (ValueError, TypeError):
            continue
        sel = (d.get("data") or {}).get("hotelOffers") or {}
        offers = (sel.get("offersSelection") or {}).get("offers")
        if offers is not None:
            return offers, (sel.get("availability") or {}).get("status")
    return None, None


def extract_room_names(bodies):
    """Map room code -> display name from the hotel GraphQL payload."""
    names = {}
    for body in bodies:
        try:
            d = json.loads(body)
        except (ValueError, TypeError):
            continue
        for acc in ((d.get("data") or {}).get("hotel") or {}).get(
                "accommodations") or []:
            if acc.get("code") and acc.get("name"):
                names[acc["code"]] = acc["name"]
    return names


def base_room_flexible(offers):
    """Starting (cheapest) room category's flexible rates.

    Returns dict with room code, currency, and for both meal plans
    (room-only "flex", breakfast "flex_bb"): member amount, standard
    amount, rate label. Either plan may be missing.
    """
    def parse(o):
        pricing = o.get("pricing") or {}
        main = pricing.get("main") or {}
        canc = ((main.get("simplifiedPolicies") or {}).get("cancellation")
                or {})
        amount = main.get("amount")
        if amount is None:
            return None
        alt = pricing.get("alternative") or {}
        return {
            "member": float(amount),
            "standard": float(alt.get("amount") or amount),
            "currency": pricing.get("currency"),
            "room": (o.get("product") or {}).get("id"),
            "rate_label": (o.get("rate") or {}).get("label"),
            "breakfast": (o.get("mealPlan") or {}).get("code")
                         == "BED_AND_BREAKFAST",
            "cancellable": canc.get("code") == "FREE_CANCELLATION",
        }

    parsed = [p for p in (parse(o) for o in offers) if p]
    if not any(p["cancellable"] for p in parsed):
        return None
    rooms = {}
    for p in parsed:
        rooms.setdefault(p["room"], []).append(p)

    def room_key(code):
        plans = [p for p in rooms[code] if p["cancellable"]]
        if not plans:
            return float("inf")  # rooms without a flexible rate never win
        room_only = [p["member"] for p in plans if not p["breakfast"]]
        # rank rooms by their room-only flexible price (fall back to any)
        return min(room_only) if room_only else min(p["member"] for p in plans)

    base = min(rooms, key=room_key)
    plans = rooms[base]
    result = {"room": base, "currency": plans[0]["currency"]}
    buckets = {
        "flex": [p for p in plans if p["cancellable"] and not p["breakfast"]],
        "flex_bb": [p for p in plans if p["cancellable"] and p["breakfast"]],
        "nf": [p for p in plans
               if not p["cancellable"] and not p["breakfast"]],
        "nf_bb": [p for p in plans
                  if not p["cancellable"] and p["breakfast"]],
    }
    for key, lst in buckets.items():
        if lst:
            result[key] = min(lst, key=lambda p: p["member"])
    return result


def uid_of(b):
    """Stable key: a hotel can be watched twice for the same check-in
    with different stay lengths."""
    return f'{b["code"]}:{b["dateIn"]}:{int(b["nights"])}'


def offers_to_result(booking, offers, status, room_names):
    result = {"code": booking["code"], "uid": uid_of(booking),
              "name": booking["name"]}
    if offers is None:
        result["error"] = "offers response not captured"
        return result
    best = base_room_flexible(offers)
    if best is None:
        result["error"] = f"no flexible rate (availability: {status})"
        return result
    result["room"] = best["room"]
    result["room_name"] = room_names.get(best["room"], best["room"])
    result["currency"] = best["currency"]
    if "flex" in best:
        result["member_amount"] = best["flex"]["member"]
        result["standard_amount"] = best["flex"]["standard"]
        result["rate_label"] = best["flex"]["rate_label"]
    if "flex_bb" in best:
        result["bb_member_amount"] = best["flex_bb"]["member"]
        result["bb_standard_amount"] = best["flex_bb"]["standard"]
        result["bb_rate_label"] = best["flex_bb"]["rate_label"]
    if "nf" in best:
        result["nf_member_amount"] = best["nf"]["member"]
        result["nf_standard_amount"] = best["nf"]["standard"]
    if "nf_bb" in best:
        result["nf_bb_member_amount"] = best["nf_bb"]["member"]
        result["nf_bb_standard_amount"] = best["nf_bb"]["standard"]
    # room has only a breakfast-inclusive flexible rate: use it for comparison
    if "member_amount" not in result and "bb_member_amount" in result:
        result["member_amount"] = result["bb_member_amount"]
        result["standard_amount"] = result["bb_standard_amount"]
        result["rate_label"] = result["bb_rate_label"] + " (breakfast only)"
    return result


ROOMS_CACHE_FILE = BASE / "rooms_cache.json"


def load_rooms_cache():
    if ROOMS_CACHE_FILE.exists():
        return json.loads(ROOMS_CACHE_FILE.read_text())
    # bootstrap from history (it stores the chosen room's name per check)
    cache = {}
    for run in load_history():
        for h in run.get("hotels", []):
            if h.get("room") and h.get("room_name") \
                    and h["room_name"] != h["room"]:
                cache.setdefault(h["code"], {})[h["room"]] = h["room_name"]
    return cache


TEMPLATE_HEADER_KEYS = ("apikey", "app-id", "app-version", "clientid",
                        "identification-token", "lang", "content-type")


async def capture_templates(page, seed):
    """Load one booking page; capture replayable GraphQL request templates.

    Returns dict with url, headers, offers_post, hotel_post (may be None),
    plus the seed's code/dates for retargeting — or None on failure.
    """
    tmpl = {"offers_post": None, "hotel_post": None,
            "seed_code": seed["code"], "seed_date_in": seed["dateIn"],
            "seed_date_out": date_out(seed["dateIn"], seed["nights"]),
            "bodies": []}

    async def on_response(resp):
        if "graphql" not in resp.url:
            return
        try:
            body = await resp.text()
        except Exception:
            return
        tmpl["bodies"].append(body)
        req = resp.request
        if tmpl["offers_post"] is None and '"hotelOffers"' in body \
                and '"offers"' in body:
            tmpl["url"] = resp.url
            tmpl["offers_post"] = req.post_data
            headers = await req.all_headers()
            tmpl["headers"] = {k: headers[k] for k in TEMPLATE_HEADER_KEYS
                               if k in headers}
        if tmpl["hotel_post"] is None and '"accommodations"' in body:
            tmpl["hotel_post"] = req.post_data

    page.on("response", on_response)
    try:
        url = BOOKING_URL.format(code=seed["code"], dateIn=seed["dateIn"],
                                 nights=seed["nights"],
                                 adults=CONFIG["adults"])
        await page.goto(url, wait_until="domcontentloaded", timeout=60000)
        for _ in range(80):
            await page.wait_for_timeout(500)
            if tmpl["offers_post"]:
                break
    except Exception:
        pass
    finally:
        page.remove_listener("response", on_response)
    return tmpl if tmpl["offers_post"] else None


def date_out(date_in, nights):
    d = dt.date.fromisoformat(date_in) + dt.timedelta(days=int(nights))
    return d.isoformat()


def retarget(obj, mapping):
    """Recursively replace seed values (code/dates) with the target's."""
    if isinstance(obj, dict):
        return {k: retarget(v, mapping) for k, v in obj.items()}
    if isinstance(obj, list):
        return [retarget(v, mapping) for v in obj]
    if isinstance(obj, str) and obj in mapping:
        return mapping[obj]
    return obj


async def api_fetch(page, tmpl, post_str, mapping, currency=None):
    post = json.loads(post_str)
    post["variables"] = retarget(post.get("variables", {}), mapping)
    v = post["variables"]
    if currency and "currency" in v:
        v["currency"] = currency
    if "nbAdults" in v:
        v["nbAdults"] = CONFIG["adults"]
    result = await page.evaluate(
        """async ([url, body, hdrs]) => {
            const r = await fetch(url, {method:'POST', headers: hdrs,
                                        body: body, credentials:'include'});
            return {status: r.status, text: await r.text()};
        }""", [tmpl["url"], json.dumps(post), tmpl["headers"]])
    if result["status"] != 200:
        raise RuntimeError(f"API status {result['status']}")
    return json.loads(result["text"])


async def fast_fetch(page, tmpl, booking):
    """Fetch a hotel's offers via direct API replay — INR, ~0.3s."""
    mapping = {tmpl["seed_code"]: booking["code"],
               tmpl["seed_date_in"]: booking["dateIn"],
               tmpl["seed_date_out"]: date_out(booking["dateIn"],
                                               booking["nights"])}
    data = await api_fetch(page, tmpl, tmpl["offers_post"], mapping,
                           currency="INR")
    sel = (data.get("data") or {}).get("hotelOffers") or {}
    offers = (sel.get("offersSelection") or {}).get("offers")
    status = (sel.get("availability") or {}).get("status")
    return offers, status


async def accor_fx_rate(page, tmpl, booking):
    """Accor converts with its own (DEVISEA) rate, not the market rate.
    Ask for the same stay in INR and in EUR and divide — that reproduces
    their booking pages to the rupee."""
    mapping = {tmpl["seed_code"]: booking["code"],
               tmpl["seed_date_in"]: booking["dateIn"],
               tmpl["seed_date_out"]: date_out(booking["dateIn"],
                                               booking["nights"])}

    def first_amount(data):
        sel = (data.get("data") or {}).get("hotelOffers") or {}
        offers = (sel.get("offersSelection") or {}).get("offers") or []
        return offers[0]["pricing"]["main"]["amount"] if offers else None

    try:
        inr = first_amount(await api_fetch(page, tmpl, tmpl["offers_post"],
                                           mapping, currency="INR"))
        eur = first_amount(await api_fetch(page, tmpl, tmpl["offers_post"],
                                           mapping, currency="EUR"))
        if inr and eur:
            return inr / eur
    except Exception as e:
        print(f"  accor fx probe failed: {e}")
    return None


async def fetch_room_names(page, tmpl, code):
    if not tmpl.get("hotel_post"):
        return {}
    try:
        data = await api_fetch(page, tmpl, tmpl["hotel_post"],
                               {tmpl["seed_code"]: code})
    except Exception:
        return {}
    names = {}
    hotel = (data.get("data") or {}).get("hotel") or {}
    for acc in hotel.get("accommodations") or []:
        if acc.get("code") and acc.get("name"):
            names[acc["code"]] = acc["name"]
    gps = (hotel.get("localization") or {}).get("gps") or {}
    lat = gps.get("latitude") or gps.get("lat")
    lon = gps.get("longitude") or gps.get("lng") or gps.get("lon")
    if lat and lon:
        names["_gps"] = [float(lat), float(lon)]
    return names


async def check_hotel(browser, booking):
    url = BOOKING_URL.format(code=booking["code"], dateIn=booking["dateIn"],
                             nights=booking["nights"],
                             adults=CONFIG["adults"])
    bodies = []
    offers = None
    status = None
    for attempt in range(3):
        ctx = await browser.new_context(
            user_agent=UA, locale="en-IN",
            viewport={"width": 1440, "height": 900})

        # we only need the rates API response — skip heavy page assets
        async def _route(route):
            req = route.request
            if (req.resource_type in ("image", "media", "font")
                    or any(h in req.url for h in
                           ("googletagmanager", "google-analytics",
                            "doubleclick", "facebook", "hotjar",
                            "demdex", "omtrdc", "quantummetric"))):
                await route.abort()
            else:
                await route.continue_()
        await ctx.route("**/*", _route)
        page = await ctx.new_page()

        async def on_response(resp):
            if "graphql" not in resp.url:
                return
            try:
                bodies.append(await resp.text())
            except Exception:
                pass

        page.on("response", on_response)
        try:
            await page.goto(url, wait_until="domcontentloaded", timeout=60000)
            for _ in range(80):  # up to ~40s
                await page.wait_for_timeout(500)
                offers, status = extract_offers(bodies)
                if offers is not None:
                    break
        except Exception:
            pass
        finally:
            await ctx.close()
        if offers is not None:
            break
        await asyncio.sleep(5 * (attempt + 1))

    return offers_to_result(booking, offers, status,
                            extract_room_names(bodies))


def eff_booked(b, fx):
    """Booked amount in today's INR: fixed EUR re-converted at the current
    rate (mirrors Accor's account page); falls back to the INR snapshot."""
    if b.get("booked_eur") and fx:
        return b["booked_eur"] * fx
    return b["booked_inr"]


def load_history():
    if HISTORY_FILE.exists():
        return json.loads(HISTORY_FILE.read_text())
    return []


PUBLISH_DIR = BASE / "publish"


PAGES_REPO = "rohangautam09/accor-price-watch"
PAGES_WORKFLOW = "pages-check.yml"


def publish_public(history, fx):
    """Publish this run to the GitHub Pages dashboard — the same page the
    cloud checker builds, so a check on either side updates one site.
    Booking refs are masked there. Best-effort; never blocks a local run."""
    if not PUBLISH_DIR.is_dir():
        return
    # everything in publish/ is generated, and the cloud checker pushes to
    # the same branch — so start from the remote state instead of merging
    g = ["git", "-C", str(PUBLISH_DIR)]
    try:
        subprocess.run(g + ["rebase", "--abort"], capture_output=True,
                       timeout=30)
        subprocess.run(g + ["fetch", "-q", "origin"], capture_output=True,
                       timeout=60)
        subprocess.run(g + ["reset", "--hard", "-q", "origin/main"],
                       capture_output=True, timeout=30)
    except Exception:
        pass
    # share one history with the cloud checker so trends and since-last-run
    # deltas are continuous no matter which side ran
    shared = PUBLISH_DIR / "history_public.json"
    try:
        runs = json.loads(shared.read_text()) if shared.exists() else []
    except ValueError:
        runs = []
    latest = history[-1] if history else None
    if latest:
        stamps = {r.get("checked_at") for r in runs}
        if latest.get("checked_at") not in stamps:
            prev = runs[-1] if runs else None
            if prev:
                pm = {h.get("uid", h["code"]): h
                      for h in prev.get("hotels", [])}
                at = prev.get("checked_at", prev["date"])
                for r in latest["hotels"]:
                    p = pm.get(r.get("uid", r["code"]))
                    if p and "inr_member" in p and "inr_member" in r:
                        r.setdefault("prev", {
                            "at": at, "inr_member": p["inr_member"],
                            "inr_bb_member": p.get("inr_bb_member"),
                            "inr_nf_member": p.get("inr_nf_member"),
                            "inr_nf_bb_member": p.get("inr_nf_bb_member")})
            runs.append(latest)
            runs = runs[-60:]
            shared.write_text(json.dumps(runs, indent=1, ensure_ascii=False))
    for name in ("render.py", "check.py", "savings.json",
                 "rooms_cache.json", "floors.json"):
        src = BASE / name
        if src.exists():
            (PUBLISH_DIR / name).write_text(src.read_text())
    (PUBLISH_DIR / "index.html").write_text(
        render_page(CONFIG, runs or history, fx, cloud=True,
                    repo=PAGES_REPO, workflow=PAGES_WORKFLOW))
    if not (PUBLISH_DIR / ".git").exists():
        return
    try:
        subprocess.run(g + ["add", "-A"], capture_output=True, timeout=30)
        subprocess.run(g + ["commit", "-m",
                            f"prices from the Mac "
                            f"{dt.datetime.now():%Y-%m-%d %H:%M}"],
                       capture_output=True, timeout=30)
        subprocess.run(g + ["push", "-q"], capture_output=True, timeout=60)
    except Exception:
        pass
    # keep the private code repo (cloud morning-alert) in sync too, so
    # watchlist/booked-price edits reach the GitHub Actions checker
    try:
        g = ["git", "-C", str(BASE)]
        subprocess.run(g + ["add", "-A"], capture_output=True, timeout=30)
        subprocess.run(g + ["commit", "-m",
                            f"sync {dt.datetime.now():%Y-%m-%d %H:%M}"],
                       capture_output=True, timeout=30)
        subprocess.run(g + ["push"], capture_output=True, timeout=60)
    except Exception:
        pass


def notify(title, message):
    try:
        subprocess.run(
            ["osascript", "-e",
             f'display notification "{message}" with title "{title}"'],
            check=False, timeout=10)
    except Exception:
        pass




async def main(only=None):
    rates = get_fx_rates()
    fx = rates.get("INR") if rates else None  # INR per EUR
    bookings = CONFIG["bookings"]
    if only:
        bookings = [b for b in bookings
                    if uid_of(b) == only or
                    f'{b["code"]}:{b["dateIn"]}' == only]
    t0 = time.monotonic()
    rooms_cache = load_rooms_cache()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)

        def convert(r):
            cur = r.get("currency")
            if rates and cur in rates and rates[cur]:
                inr_per = rates["INR"] / rates[cur]
                eur_per = 1 / rates[cur]
                for src, dst in (("member_amount", "inr_member"),
                                 ("standard_amount", "inr_standard"),
                                 ("bb_member_amount", "inr_bb_member"),
                                 ("bb_standard_amount", "inr_bb_standard"),
                                 ("nf_member_amount", "inr_nf_member"),
                                 ("nf_standard_amount", "inr_nf_standard"),
                                 ("nf_bb_member_amount", "inr_nf_bb_member"),
                                 ("nf_bb_standard_amount",
                                  "inr_nf_bb_standard")):
                    if src in r:
                        r[dst] = r[src] * inr_per
                for src, dst in (("member_amount", "eur_member"),
                                 ("bb_member_amount", "eur_bb_member"),
                                 ("nf_member_amount", "eur_nf_member"),
                                 ("nf_bb_member_amount", "eur_nf_bb_member")):
                    if src in r:
                        r[dst] = r[src] * eur_per
            status = (f"{r.get('member_amount', '?')} {r.get('currency', '')}"
                      if "member_amount" in r else r.get("error"))
            return status

        # one real page load establishes the session and captures a
        # replayable API request; every hotel is then a direct API call
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        seed_page = await ctx.new_page()
        tmpl = await capture_templates(seed_page, bookings[0])
        if tmpl:
            afx = await accor_fx_rate(seed_page, tmpl, bookings[0])
            if afx:
                print(f"accor rate: ₹{afx:.4f}/€ (market ₹{fx:.4f})")
                rates["INR"] = afx * rates.get("EUR", 1)
                fx = afx
            rooms_cache.setdefault(bookings[0]["code"], {}).update(
                extract_room_names(tmpl["bodies"]))
            sem = asyncio.Semaphore(6)

            async def run_one(b):
                async with sem:
                    offers = status_ = None
                    for attempt in range(2):
                        try:
                            offers, status_ = await fast_fetch(seed_page,
                                                               tmpl, b)
                            break
                        except Exception:
                            await asyncio.sleep(1 + attempt)
                    if b["code"] not in rooms_cache:
                        rooms_cache[b["code"]] = await fetch_room_names(
                            seed_page, tmpl, b["code"])
                    r = offers_to_result(b, offers, status_,
                                         rooms_cache.get(b["code"], {}))
                    if "error" in r and "not captured" in r["error"]:
                        # API replay failed: fall back to a full page load
                        r = await check_hotel(browser, b)
                print(f"[{b['code']}] {b['name']}: {convert(r)}")
                return r
        else:
            # template capture failed: classic page-load path for everything
            sem = asyncio.Semaphore(3)

            async def run_one(b):
                async with sem:
                    r = await check_hotel(browser, b)
                print(f"[{b['code']}] {b['name']}: {convert(r)}")
                return r

        results = list(await asyncio.gather(*(run_one(b) for b in bookings)))

        # date scan: check the same stay length starting ±N days around
        # each check-in, flexible rates only, to find a cheaper start date
        scan = int(CONFIG.get("date_scan_days", 0))
        if tmpl and scan > 0:

            async def scan_date(b, offset):
                d = (dt.date.fromisoformat(b["dateIn"])
                     + dt.timedelta(days=offset))
                if d <= dt.date.today():
                    return None
                alt_booking = dict(b, dateIn=d.isoformat())
                async with sem:
                    try:
                        offers, _ = await fast_fetch(seed_page, tmpl,
                                                     alt_booking)
                    except Exception:
                        return None
                best = base_room_flexible(offers or [])
                if not best:
                    return None
                plan = best.get("flex") or best.get("flex_bb")
                cur = best["currency"]
                if cur == "INR":
                    inr = plan["member"]
                elif rates and cur in rates and rates[cur]:
                    inr = plan["member"] * rates["INR"] / rates[cur]
                else:
                    return None
                return (d.isoformat(), inr)

            jobs = []
            for b, r in zip(bookings, results):
                if "inr_member" not in r:
                    continue
                for o in range(-scan, scan + 1):
                    if o:
                        jobs.append((b, r, o))
            found = await asyncio.gather(*(scan_date(b, o)
                                           for b, r, o in jobs))
            per_hotel = {}
            for (b, r, o), cand in zip(jobs, found):
                if cand:
                    per_hotel.setdefault(id(r), []).append(cand)
            for b, r in zip(bookings, results):
                if "inr_member" not in r:
                    continue
                r["alt_scan_days"] = scan
                cands = per_hotel.get(id(r), [])
                cands.append((b["dateIn"], r["inr_member"]))
                best_d, best_i = min(cands, key=lambda c: c[1])
                if best_d != b["dateIn"] and best_i < r["inr_member"] - 1:
                    r["alt_best"] = {"dateIn": best_d, "inr_member": best_i}
        await browser.close()
    ROOMS_CACHE_FILE.write_text(json.dumps(rooms_cache, indent=1,
                                           ensure_ascii=False))
    duration = round(time.monotonic() - t0)

    history = load_history()
    today = dt.date.today().isoformat()
    # remember what each hotel cost at the previous run, so the dashboard
    # can show the change since the last check
    if history:
        prev_entry = history[-1]
        prev_map = {h.get("uid", h["code"]): h
                    for h in prev_entry.get("hotels", [])}
        stamp = prev_entry.get("checked_at", prev_entry["date"])
        for r in results:
            p = prev_map.get(r.get("uid", r["code"]))
            if p and "inr_member" in p and "inr_member" in r:
                r["prev"] = {"at": stamp,
                             "inr_member": p["inr_member"],
                             "inr_bb_member": p.get("inr_bb_member"),
                             "inr_nf_member": p.get("inr_nf_member"),
                             "inr_nf_bb_member": p.get("inr_nf_bb_member")}
    if only and history:
        # partial run: carry over the other hotels from the most recent
        # entry (today's if it exists, else yesterday's), replace the
        # checked one
        prev = next((h for h in history if h["date"] == today), history[-1])
        merged = [h for h in prev["hotels"]
                  if not any(r.get("uid", r["code"])
                             == h.get("uid", h["code"])
                             for r in results)]
        merged.extend(results)
    else:
        merged = results
    history = [h for h in history if h["date"] != today]
    history.append({"date": today,
                    "checked_at": dt.datetime.now().isoformat(timespec="seconds"),
                    "duration_seconds": duration,
                    "fx_inr_per_eur": fx, "fx_source": "accor",
                    "hotels": merged})
    HISTORY_FILE.write_text(json.dumps(history, indent=1))
    DASHBOARD_FILE.write_text(render_page(CONFIG, history, fx))
    publish_public(history, fx)

    # keep the cloud's copy of what's booked in step with the Mac's, so an
    # edit made through the local UI can't sit stale in GitHub — see
    # sync_secret.py for why this exists
    from sync_secret import sync_now
    ok, err = sync_now()
    if not ok:
        print(f"::warning::could not sync config to GitHub secret: {err}")

    drops = []
    for b in bookings:
        r = next((x for x in results
                  if x.get("uid", x["code"]) == uid_of(b)), {})
        # compare like-for-like: breakfast bookings vs flexible+breakfast rate
        key = ("inr_bb_member" if b.get("breakfast")
               and r.get("inr_bb_member") else "inr_member")
        inr = r.get(key)
        if inr is not None:
            # the subscriber rate is invisible logged out, so estimate it
            # from the public rate, then the manually-noted app discount
            if b.get("accor_plus", True) is not False:
                p = accor_plus_price(inr, r.get(PAIR[key]),
                                     float(b.get("accor_plus_pct")
                                           or PLUS_PCT))
                if p and p < inr:
                    inr = p
            inr *= 1 - float(b.get("app_discount_pct") or 0) / 100
        booked = eff_booked(b, fx)
        if inr is not None and booked - inr > CONFIG["drop_threshold_inr"]:
            drops.append({"name": b["name"],
                          "save": booked - inr,
                          "line": f"{b['name']}: {fmt_inr(inr)} "
                                  f"(save {fmt_inr(booked - inr)}, "
                                  f"rebook then cancel {b['booking_no']})"})
    if drops:
        lines = [d["line"] for d in drops]
        notify("Accor price drop!", "; ".join(lines)[:230])
        print("\nPRICE DROPS:\n  " + "\n  ".join(lines))
    else:
        print("\nNo price drops today.")
    print(f"Dashboard: {DASHBOARD_FILE}")


FLOORS_FILE = BASE / "floors.json"


async def floor_scan(code, date_in):
    """Scan the next ~6 months (every 2nd day) for the cheapest flexible
    price for the same stay length — the hotel's price floor."""
    b = next((x for x in CONFIG["bookings"]
              if x["code"] == code and x["dateIn"] == date_in), None)
    if b is None:
        print(f"[floor] {code}:{date_in} not in watch list")
        return
    rates = get_fx_rates()
    fx = rates.get("INR") if rates else None
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        page = await ctx.new_page()
        tmpl = await capture_templates(page, b)
        if not tmpl:
            print("[floor] could not capture API template")
            return
        sem = asyncio.Semaphore(6)
        start = dt.date.today() + dt.timedelta(days=3)

        async def one(offset):
            d = start + dt.timedelta(days=offset)
            async with sem:
                try:
                    offers, _ = await fast_fetch(
                        page, tmpl, dict(b, dateIn=d.isoformat()))
                except Exception:
                    return None
            best = base_room_flexible(offers or [])
            if not best:
                return None
            plan = best.get("flex") or best.get("flex_bb")
            cur = best["currency"]
            if cur == "INR":
                inr = plan["member"]
            elif rates and cur in rates and rates[cur]:
                inr = plan["member"] * rates["INR"] / rates[cur]
            else:
                return None
            return (d.isoformat(), inr)

        found = [c for c in await asyncio.gather(
            *(one(o) for o in range(0, 180, 2))) if c]
        await browser.close()
    if not found:
        print("[floor] no flexible prices found in the next 6 months")
        return
    best_d, best_i = min(found, key=lambda c: c[1])
    floors = (json.loads(FLOORS_FILE.read_text())
              if FLOORS_FILE.exists() else {})
    floors[f"{code}:{date_in}"] = {
        "inr": best_i, "dateIn": best_d, "nights": b["nights"],
        "scanned": dt.date.today().isoformat(),
        "checked_dates": len(found)}
    FLOORS_FILE.write_text(json.dumps(floors, indent=1))
    DASHBOARD_FILE.write_text(render_page(CONFIG, load_history(), fx))
    print(f"[floor] {b['name']}: cheapest {fmt_inr(best_i)} starting "
          f"{best_d} ({len(found)} dates checked)")


if __name__ == "__main__":
    import sys
    if len(sys.argv) >= 3 and sys.argv[1] == "--floor":
        code_arg, date_arg = sys.argv[2].split(":", 1)
        asyncio.run(floor_scan(code_arg, date_arg))
    else:
        only_arg = None
        if len(sys.argv) >= 3 and sys.argv[1] == "--only":
            only_arg = sys.argv[2]  # "CODE:YYYY-MM-DD"
        asyncio.run(main(only_arg))
