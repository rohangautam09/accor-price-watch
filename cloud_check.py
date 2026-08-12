"""Cloud price check (GitHub Actions).

Fetches current prices with the same engine as check.py, then writes:
  dashboard.md  — full price table (posted as a GitHub issue you can open
                  from your phone; updated every run)
  alert.md      — only if some hotel is cheaper than its booked price
  fingerprint.txt — identifies this drop set, so a persisting drop does not
                  re-notify every hour

Writes no history and makes no commits: state lives in GitHub issues.
"""
import asyncio
import datetime as dt
import json
import pathlib
import re

from playwright.async_api import async_playwright

import check
from check import (CONFIG, UA, capture_templates, fast_fetch, fetch_room_names,
                   get_fx_rates, offers_to_result, uid_of)
from render import fmt_inr

BASE = pathlib.Path(__file__).parent


def booked_now(b, fx):
    """Booked total in today's INR (EUR-fixed, like the dashboard)."""
    if b.get("booked_eur") and fx:
        return b["booked_eur"] * fx
    return b["booked_inr"]


def load_prev():
    """Previous run's prices, hidden in the dashboard issue we last wrote
    (so the cloud needs no database and makes no commits)."""
    f = BASE / "prev_body.md"
    if not f.exists():
        return {}, None
    m = re.search(r"<!--prev:(.*?)-->", f.read_text(), re.S)
    if not m:
        return {}, None
    try:
        d = json.loads(m.group(1))
        return d.get("prices", {}), d.get("at")
    except ValueError:
        return {}, None


async def main():
    rates = get_fx_rates()
    fx = rates.get("INR") if rates else None
    bookings = CONFIG["bookings"]
    rooms_cache = check.load_rooms_cache()
    results = {}
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        ctx = await browser.new_context(user_agent=UA, locale="en-IN")
        page = await ctx.new_page()
        tmpl = await capture_templates(page, bookings[0])
        if not tmpl:
            print("::error::could not capture the Accor API template")
            return 1
        sem = asyncio.Semaphore(6)

        async def one(b):
            async with sem:
                offers = status = None
                try:
                    offers, status = await fast_fetch(page, tmpl, b)
                except Exception as e:
                    print(f"  replay failed for {b['code']}: {e}")
                names = rooms_cache.get(b["code"], {})
                r = offers_to_result(b, offers, status, names)
                if "error" in r:
                    # datacenter IPs can get the direct API call refused;
                    # fall back to a full page load, which Accor allows
                    print(f"  falling back to page load for {b['code']}")
                    r = await check.check_hotel(browser, b)
            cur = r.get("currency")
            if rates and cur in rates and rates[cur]:
                f = rates["INR"] / rates[cur]
                for src, dst in (("member_amount", "inr_member"),
                                 ("bb_member_amount", "inr_bb_member")):
                    if src in r:
                        r[dst] = r[src] * f
            results[uid_of(b)] = r
            print(f"[{b['code']}] {b['name'][:38]}: "
                  f"{r.get('inr_member', r.get('error'))}")

        await asyncio.gather(*(one(b) for b in bookings))
        await browser.close()

    prev_prices, prev_at = load_prev()
    prices_now = {}
    rows, drops, fingerprint, movers = [], [], [], []
    for b in bookings:
        uid = uid_of(b)
        r = results.get(uid, {})
        disc = 1 - float(b.get("app_discount_pct") or 0) / 100
        now = (r.get("inr_bb_member") if b.get("breakfast")
               else None) or r.get("inr_member")
        dates = (f'{dt.date.fromisoformat(b["dateIn"]):%d %b}'
                 f'–{dt.date.fromisoformat(b["dateIn"]) + dt.timedelta(days=int(b["nights"])):%d %b}')
        if now is None:
            rows.append(f'| {b["name"][:34]} | {dates} | – | – | – | '
                        f'{r.get("error", "no data")} |')
            continue
        now *= disc
        prices_now[uid] = round(now)

        # movement since the previous run
        pv = prev_prices.get(uid)
        if pv is None:
            since = "new"
        elif abs(now - pv) <= 50:
            since = "＝"
        elif now < pv:
            since = f"▼ {fmt_inr(pv - now)}"
            movers.append(f'- ▼ **{b["name"]}** ({dates}) fell '
                          f'{fmt_inr(pv - now)} to {fmt_inr(now)}')
        else:
            since = f"▲ {fmt_inr(now - pv)}"
            movers.append(f'- ▲ **{b["name"]}** ({dates}) rose '
                          f'{fmt_inr(now - pv)} to {fmt_inr(now)}')

        bk = booked_now(b, fx)
        if not bk:
            rows.append(f'| {b["name"][:34]} | {dates} | – | '
                        f'{fmt_inr(now)} | {since} | watching |')
            continue
        d = now - bk
        mark = ("🔥 **cheaper**" if d < -CONFIG["drop_threshold_inr"]
                else ("≈ same" if abs(d) <= CONFIG["drop_threshold_inr"]
                      else f"+{fmt_inr(d)}"))
        rows.append(f'| {b["name"][:34]} | {dates} | {fmt_inr(bk)} | '
                    f'{fmt_inr(now)} | {since} | {mark} |')
        if d < -CONFIG["drop_threshold_inr"]:
            drops.append(f'- **{b["name"]}** ({dates}): now **{fmt_inr(now)}**, '
                         f'save **{fmt_inr(-d)}** — rebook, then cancel '
                         f'`{b["booking_no"]}`')
            fingerprint.append(f'{uid_of(b)}@{int(now // 500)}')

    now_ist = dt.datetime.now(dt.timezone(dt.timedelta(hours=5, minutes=30)))
    changed = ("\n\n**Changed since the last check"
               + (f" ({prev_at} IST)" if prev_at else "") + ":**\n"
               + "\n".join(movers) if movers else
               "\n\n_Nothing moved since the last check._")
    header = (f'_Updated {now_ist:%d %b %Y, %H:%M} IST · '
              f'1 EUR ≈ ₹{fx:,.2f}_\n\n'
              f'| Hotel | Dates | Booked | Now | Since last | vs booked |\n'
              f'|---|---|---|---|---|---|\n')
    state = json.dumps({"at": f"{now_ist:%d %b %H:%M}",
                        "prices": prices_now}, separators=(",", ":"))
    (BASE / "dashboard.md").write_text(
        header + "\n".join(rows) + changed
        + "\n\n_Comment `check` to refresh._\n"
        + f"<!--prev:{state}-->\n")
    if drops:
        (BASE / "alert.md").write_text(
            "\n".join(drops)
            + "\n\n**Book the new flexible rate first, confirm it, then "
            "cancel the old booking.**\n\n"
            f"<!--fp:{'|'.join(sorted(fingerprint))}-->\n")
        (BASE / "fingerprint.txt").write_text("|".join(sorted(fingerprint)))
    print(f"\n{len(drops)} drop(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(asyncio.run(main()))
