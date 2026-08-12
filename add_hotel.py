"""Interactive helper: add a hotel to the Accor price watch list."""
import datetime as dt
import html
import json
import pathlib
import re
import subprocess
import sys
import urllib.request

BASE = pathlib.Path(__file__).parent
CONFIG_FILE = BASE / "config.json"


def ask(prompt, default=None, validate=None):
    while True:
        suffix = f" [{default}]" if default not in (None, "") else ""
        raw = input(f"{prompt}{suffix}: ").strip()
        if not raw and default is not None:
            raw = str(default)
        if validate is None:
            return raw
        try:
            return validate(raw)
        except Exception as e:
            print(f"  ✗ {e}")


def parse_code(raw):
    raw = raw.strip()
    m = re.search(r"(?:hotel[/=]|/booking/[a-z]{2}/accor/hotel/)([A-Z0-9]{3,6})",
                  raw, re.I)
    if m:
        return m.group(1).upper() if not m.group(1).isdigit() else m.group(1)
    if re.fullmatch(r"[A-Za-z0-9]{3,6}", raw):
        return raw.upper() if not raw.isdigit() else raw
    raise ValueError("paste the hotel's all.accor.com URL or its code "
                     "(e.g. 3044 or A599)")


def fetch_name(code):
    try:
        req = urllib.request.Request(
            f"https://all.accor.com/hotel/{code}/index.en.shtml",
            headers={"User-Agent": "Mozilla/5.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            page = r.read(300000).decode("utf-8", "ignore")
        for pat in (r'property="og:title" content="([^"]+)"',
                    r'"hotelName"\s*:\s*"([^"]+)"',
                    r"<h1[^>]*>\s*([^<]+?)\s*<"):
            m = re.search(pat, page)
            if m and m.group(1).strip():
                return html.unescape(m.group(1).strip())
        m = re.search(r"<title>(.*?)</title>", page, re.S)
        if m:
            return html.unescape(
                m.group(1).strip().split(" | ")[0].split(" - ")[0].strip())
        return None
    except Exception:
        return None


def parse_date(raw):
    return dt.date.fromisoformat(raw).isoformat()


def parse_money(raw):
    v = float(raw.replace(",", "").replace("₹", "").strip() or 0)
    if v < 0:
        raise ValueError("must be >= 0")
    return v


def main():
    print("── Add a hotel to the Accor price watch ──")
    code = ask("Hotel URL or code", validate=parse_code)
    guessed = fetch_name(code)
    name = ask("Hotel name", default=guessed or "")
    city = ask("City", default="")
    date_in = ask("Check-in date (YYYY-MM-DD)", validate=parse_date)
    nights = ask("Nights", validate=lambda r: int(r))
    print("If you already booked, enter the booked total in INR.")
    print("If you're just watching (not booked yet), enter 0.")
    booked = ask("Booked total INR", default="0", validate=parse_money)
    booking_no = ""
    if booked > 0:
        booking_no = ask("Booking number", default="")

    cfg = json.loads(CONFIG_FILE.read_text())
    entry = {"name": name, "city": city, "code": code, "dateIn": date_in,
             "nights": nights, "booked_inr": booked,
             "booking_no": booking_no or "—"}
    cfg["bookings"].append(entry)
    CONFIG_FILE.write_text(json.dumps(cfg, indent=2, ensure_ascii=False))
    print(f"\n✓ Added: {name} ({code}) · {date_in} · {nights} night(s) · "
          + (f"booked ₹{booked:,.0f}" if booked else "watch only"))

    if ask("Run a price check now? (y/n)", default="y").lower().startswith("y"):
        subprocess.run([str(BASE / ".venv/bin/python"), str(BASE / "check.py")])
        subprocess.run(["open", str(BASE / "dashboard.html")])


if __name__ == "__main__":
    try:
        main()
    except (KeyboardInterrupt, EOFError):
        print("\ncancelled")
        sys.exit(1)
