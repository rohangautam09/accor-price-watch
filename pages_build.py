"""Fetch prices on GitHub Actions, publish the dashboard, flag price drops.

Config arrives from an encrypted repo secret (never committed); booking
references are masked in the published HTML. Price history lives in
history_public.json so trends and since-last-run deltas survive between runs.

This script always exits 0 — a failed fetch is reported as a banner on the
dashboard rather than a red workflow run, so GitHub never emails about it.
Price drops are written to alert.md for the workflow to raise as an issue,
and alert_state.json remembers what was already alerted so a drop that
persists for hours does not notify every hour.
"""
import asyncio
import datetime as dt
import json
import pathlib
import random
import time

from playwright.async_api import async_playwright

import check
from check import (CONFIG, UA, capture_templates, extract_room_names,
                   fast_fetch, get_fx_rates, offers_to_result, uid_of)
from render import (PAIR, PLUS_PCT, accor_plus_price, fmt_inr,
                    max_points_for, render_page)

BASE = pathlib.Path(__file__).parent
HIST = BASE / "history_public.json"
STATE = BASE / "alert_state.json"
STATUS = BASE / "status.json"
REPO = "rohangautam09/accor-price-watch"
WORKFLOW = "pages-check.yml"
KEEP_RUNS = 200
IST = dt.timezone(dt.timedelta(hours=5, minutes=30))


def booked_now(b, fx):
    if b.get("booked_eur") and fx:
        return b["booked_eur"] * fx
    return b["booked_inr"]


async def fetch_all():
    """Returns (results, fx, error_or_None)."""
    rates = get_fx_rates()
    fx = rates.get("INR") if rates else None
    bookings = CONFIG["bookings"]
    rooms = check.load_rooms_cache()
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        page = await ctx.new_page()
        tmpl = await capture_templates(page, bookings[0])
        if not tmpl:
            await browser.close()
            return [], fx, "Accor did not serve prices to this run"
        rooms.setdefault(bookings[0]["code"], {}).update(
            extract_room_names(tmpl["bodies"]))
        sem = asyncio.Semaphore(3)   # gentle: 3 hotels at a time

        async def one(b):
            async with sem:
                offers = status = None
                try:
                    offers, status = await fast_fetch(page, tmpl, b)
                except Exception as e:
                    print(f"  replay failed {b['code']}: {e}")
                r = offers_to_result(b, offers, status,
                                     rooms.get(b["code"], {}))
                if "error" in r:
                    r = await check.check_hotel(browser, b)
            cur = r.get("currency")
            if rates and cur in rates and rates[cur]:
                f = rates["INR"] / rates[cur]
                e = 1 / rates[cur]
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
                        r[dst] = r[src] * f
                for src, dst in (("member_amount", "eur_member"),
                                 ("bb_member_amount", "eur_bb_member"),
                                 ("nf_member_amount", "eur_nf_member"),
                                 ("nf_bb_member_amount", "eur_nf_bb_member")):
                    if src in r:
                        r[dst] = r[src] * e
            print(f"[{b['code']}] {b['name'][:36]}: "
                  f"{r.get('inr_member', r.get('error'))}")
            return r

        results = list(await asyncio.gather(*(one(b) for b in bookings)))
        await browser.close()
    if not any("inr_member" in r for r in results):
        return results, fx, "no prices came back for any hotel"
    return results, fx, None


def find_drops(results, fx):
    """Same comparison the dashboard shows: Accor+ subscriber rate estimated
    from the public rate, app discount applied, breakfast matched, booked
    amount fixed in EUR."""
    by_uid = {r.get("uid", r["code"]): r for r in results}
    drops, fingerprint = [], []
    for b in CONFIG["bookings"]:
        r = by_uid.get(uid_of(b), {})
        key = ("inr_bb_member" if b.get("breakfast")
               and r.get("inr_bb_member") else "inr_member")
        now = r.get(key)
        booked = booked_now(b, fx)
        if now is None or not booked:
            continue
        ratio = 1.0
        if b.get("accor_plus", True) is not False:
            p = accor_plus_price(now, r.get(PAIR[key]),
                                 float(b.get("accor_plus_pct") or PLUS_PCT))
            if p and p < now:
                ratio, now = p / now, p
        now *= 1 - float(b.get("app_discount_pct") or 0) / 100
        saved = booked - now
        if saved <= CONFIG["drop_threshold_inr"]:
            continue
        d1 = dt.date.fromisoformat(b["dateIn"])
        d2 = d1 + dt.timedelta(days=int(b["nights"]))
        pts = max_points_for(
            ((r.get("eur_bb_member") if key == "inr_bb_member" else None)
             or r.get("eur_member") or 0) * ratio, b.get("city_tax_pct"))
        drops.append(
            f'### {b["name"]}\n'
            f'{d1:%d %b} → {d2:%d %b %Y} · {b["nights"]} night(s)\n\n'
            f'- now **{fmt_inr(now)}** (you booked at {fmt_inr(booked)})\n'
            f'- **save {fmt_inr(saved)}**\n'
            f'- rebook, then cancel `{b["booking_no"]}`'
            + (f'\n- points on the new booking: up to **{pts:,}**'
               if pts else ""))
        fingerprint.append(f'{uid_of(b)}@{int(now // 500)}')
    return drops, "|".join(sorted(fingerprint))


async def main():
    await asyncio.sleep(random.randint(0, 90))   # avoid clockwork timing
    t0 = time.monotonic()
    results, fx, error = await fetch_all()
    now = dt.datetime.now(IST)

    history = json.loads(HIST.read_text()) if HIST.exists() else []
    if error:
        STATUS.write_text(json.dumps(
            {"ok": False, "at": now.isoformat(timespec="seconds"),
             "error": error}))
        print(f"::warning::check failed: {error}")
        if history:      # keep the last good dashboard, just add the banner
            (BASE / "index.html").write_text(
                render_page(CONFIG, history, history[-1].get("fx_inr_per_eur"),
                            cloud=True, repo=REPO, workflow=WORKFLOW,
                            failure=f"{error} ({now:%d %b %H:%M} IST)"))
        return 0

    STATUS.write_text(json.dumps(
        {"ok": True, "at": now.isoformat(timespec="seconds")}))
    if history:
        prev = {h.get("uid", h["code"]): h
                for h in history[-1].get("hotels", [])}
        stamp = history[-1].get("checked_at", history[-1]["date"])
        for r in results:
            p_ = prev.get(r.get("uid", r["code"]))
            if p_ and "inr_member" in p_ and "inr_member" in r:
                r["prev"] = {"at": stamp,
                             "inr_member": p_["inr_member"],
                             "inr_bb_member": p_.get("inr_bb_member"),
                             "inr_nf_member": p_.get("inr_nf_member"),
                             "inr_nf_bb_member": p_.get("inr_nf_bb_member")}
    history.append({"date": now.date().isoformat(),
                    "checked_at": now.isoformat(timespec="seconds"),
                    "duration_seconds": round(time.monotonic() - t0),
                    "fx_inr_per_eur": fx, "hotels": results})
    history = history[-KEEP_RUNS:]
    HIST.write_text(json.dumps(history, indent=1, ensure_ascii=False))
    (BASE / "index.html").write_text(
        render_page(CONFIG, history, fx, cloud=True,
                    repo=REPO, workflow=WORKFLOW))

    drops, fp = find_drops(results, fx)
    old = json.loads(STATE.read_text()) if STATE.exists() else {}
    if drops and fp != old.get("fingerprint"):
        write_email(results, fx, drops, now)      # email only for new drops
        STATE.write_text(json.dumps(
            {"fingerprint": fp, "at": now.isoformat(timespec="seconds")}))
        print(f"EMAIL: {len(drops)} drop(s)")
    elif drops:
        print(f"{len(drops)} drop(s) — same as last alert, no email")
    else:
        if old.get("fingerprint"):
            STATE.write_text(json.dumps({}))      # reset when prices recover
        print("no drops — no email, dashboard updated")
    return 0


def write_email(results, fx, drops, now):
    """Only called when a new price drop is found: the drop details first,
    then the full table for context."""
    by_uid = {r.get("uid", r["code"]): r for r in results}
    rows, moved = [], 0
    for b in CONFIG["bookings"]:
        r = by_uid.get(uid_of(b), {})
        nowp = (r.get("inr_bb_member") if b.get("breakfast")
                else None) or r.get("inr_member")
        tag = "booked" if b.get("status", "booked") == "booked" else "tracking"
        d1 = dt.date.fromisoformat(b["dateIn"])
        when = f'{d1:%d %b}+{int(b["nights"])}n'
        if nowp is None:
            rows.append(f'| {b["name"][:30]} | {when} | – | – | no data |')
            continue
        nowp *= 1 - float(b.get("app_discount_pct") or 0) / 100
        prev = (r.get("prev") or {})
        pv = (prev.get("inr_bb_member") if b.get("breakfast")
              else None) or prev.get("inr_member")
        if pv and abs(nowp - pv) > 50:
            moved += 1
            since = ("▼ " if nowp < pv else "▲ ") + fmt_inr(abs(nowp - pv))
        else:
            since = "＝"
        booked = booked_now(b, fx)
        vs = (fmt_inr(nowp - booked) if booked else tag)
        if booked and nowp < booked:
            vs = f"🔥 {fmt_inr(booked - nowp)} cheaper"
        elif booked:
            vs = f"+{fmt_inr(nowp - booked)}"
        rows.append(f'| {b["name"][:30]} | {when} | '
                    f'{fmt_inr(booked) if booked else "–"} | '
                    f'{fmt_inr(nowp)} | {since} | {vs} |')

    subject = f"👀 Bhai Accor check kar — {len(drops)} hotel(s) cheaper!"
    head = ("**A watched hotel is now cheaper than what you booked.**\n\n"
            + "\n\n".join(drops)
            + "\n\n**Book the new flexible rate first, confirm it, then "
              "cancel the old booking.**\n\n---\n\n")
    body = (head
            + "| Hotel | Dates | Booked | Now | Since last | vs booked |\n"
            + "|---|---|---|---|---|---|\n" + "\n".join(rows)
            + f"\n\n[Open the dashboard](https://rohangautam09.github.io/"
              f"accor-price-watch/) · checked {now:%d %b %H:%M} IST\n")
    (BASE / "email_subject.txt").write_text(subject)
    (BASE / "email_body.md").write_text(body)
    (BASE / "has_drops.txt").write_text("yes")


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
