"""Fetch prices on GitHub Actions and build the full dashboard for Pages.

Config arrives from an encrypted repo secret (never committed); booking
references are masked in the published HTML. Price history lives in
history_public.json so trends and since-last-run deltas survive between runs.
"""
import asyncio
import datetime as dt
import json
import pathlib
import time

from playwright.async_api import async_playwright

import check
from check import (CONFIG, UA, capture_templates, extract_room_names,
                   fast_fetch, get_fx_rates, offers_to_result, uid_of)
from render import render_page

BASE = pathlib.Path(__file__).parent
HIST = BASE / "history_public.json"
REPO = "rohangautam09/accor-price-watch"
WORKFLOW = "pages-check.yml"
KEEP_DAYS = 60


async def main():
    t0 = time.monotonic()
    rates = get_fx_rates()
    fx = rates.get("INR") if rates else None
    bookings = CONFIG["bookings"]
    rooms = check.load_rooms_cache()
    results = []
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        page = await ctx.new_page()
        tmpl = await capture_templates(page, bookings[0])
        if not tmpl:
            print("::error::could not capture the Accor API template")
            return 1
        rooms.setdefault(bookings[0]["code"], {}).update(
            extract_room_names(tmpl["bodies"]))
        sem = asyncio.Semaphore(6)

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

    history = json.loads(HIST.read_text()) if HIST.exists() else []
    # carry the previous run's prices so the cards show since-last-run deltas
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
    now = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    history.append({"date": now.date().isoformat(),
                    "checked_at": now.isoformat(timespec="seconds"),
                    "duration_seconds": round(time.monotonic() - t0),
                    "fx_inr_per_eur": fx, "hotels": results})
    history = history[-KEEP_DAYS:]
    HIST.write_text(json.dumps(history, indent=1, ensure_ascii=False))

    (BASE / "index.html").write_text(
        render_page(CONFIG, history, fx, cloud=True,
                    repo=REPO, workflow=WORKFLOW))
    print("dashboard built")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
