#!/usr/bin/env python3
"""
location_timeline.py

Builds a day-by-day location timeline for the next N days, defaulting to a
home base city and overlaying trip entries where they exist.

Trip entries are NOT parsed from email/calendar by this script — an LLM
orchestrator agent extracts them upstream (see README.md "Extraction
recipe") and passes them in as JSON. This script only normalizes that into
a per-day lookup table the prayer-time engine can consume.

Usage:
    python3 location_timeline.py --trips trips.json --days 45 > timeline.json

    # or with trips inline:
    echo '[{"start_date":"2026-09-01","end_date":"2026-09-07","city":"Istanbul"}]' \\
        | python3 location_timeline.py --trips - --days 45

trips.json format (list, may be empty):
    [
      {"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "city": "Istanbul",
       "depart_at": "14:00", "return_at": "18:30"}
    ]

    depart_at (optional): clock time (HH:MM, in the ORIGIN's local time) on
    start_date when the traveler actually leaves the origin. Without it, the
    whole start_date is treated as already at the destination.

    return_at (optional): clock time (HH:MM, in the DESTINATION's local time)
    on end_date when the traveler leaves the destination to head home.
    Without it, the whole end_date is treated as still at the destination.

    This is a same-day-transition approximation (fine for 20-minute event
    granularity) — it does not model multi-day travel or layovers.

Output JSON format — normal day:
    {
      "2026-08-19": {"city": "Astana", "lat": 51.1801, "lon": 71.4460, "tz_offset_hours": 5.0},
      ...
    }

Output JSON format — boundary day (has depart_at/return_at): instead of a
flat city, you get an ordered list of segments. A consumer computes each
prayer under segments[0]'s location; if the resulting HH:MM is before that
segment's "until", keep it; otherwise recompute the prayer under the next
segment's location and use that instead:
    {
      "2026-09-01": {"segments": [
          {"until": "14:00", "city": "Astana", "lat": 51.1801, "lon": 71.4460, "tz_offset_hours": 5.0},
          {"from":  "14:00", "city": "Istanbul", "lat": 41.0082, "lon": 28.9784, "tz_offset_hours": 3.0}
      ]}
    }
"""

import argparse
import json
import sys
from datetime import date, timedelta

# City lookup: name (lowercase) -> calibration dict.
#   lat/lon/tz/dst: as before (tz = STANDARD TIME UTC offset; dst cities get
#   seasonal adjustment below).
#   country: ISO-ish country code, used by the cron bot to decide whether the
#   DUMK "+5min Dhuhr/Asr" safety margin applies (KZ only).
#   fajr_angle/isha_angle: sun depression angle in degrees. isha_angle is
#   None for cities using the fixed Maghrib+minutes rule instead (see
#   isha_offset_min).
#   isha_offset_min: if set, Isha = Maghrib + this many minutes (Umm al-Qura
#   convention: Saudi Arabia and the UAE), and isha_angle is ignored.
#   asr_method: "hanafi" (shadow factor 2) or "standard"/Shafi'i (factor 1).
#   elevation: meters, feeds prayer_times.py's sunrise/maghrib dip correction.
#   calibrated: True if verified against real local published times; False
#   means it's an untested extrapolation from a related city/authority.
#   note: known caveat, if any.
#
# Calibration source: astana/almaty against muftyat.kz (DUMK) + sajda.com;
# istanbul against aladhan.com method=13 (Diyanet); dubai method=8 (Gulf
# Region); moscow method=14 (DUM Russia); mecca/medina/jeddah method=4
# (Umm al-Qura). Checked 2026-08-19/20. See README.md for the full report.
# Other Kazakhstan cities reuse Astana's verified DUMK params (15°/15°,
# Hanafi asr) at each city's own coordinates/elevation — the astronomical
# formula was validated year-round at Astana, but individual cities weren't
# separately spot-checked against a local published timetable.
CITY_LOOKUP = {
    "astana":  {"lat": 51.1801, "lon": 71.4460, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 347.0, "calibrated": True},
    "almaty":  {"lat": 43.2220, "lon": 76.8512, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 800.0, "calibrated": True,
                "note": "DUMK's own site adds an extra 'ihtiyat' precaution offset "
                        "(asymmetric, up to ~8-10min on Dhuhr/Asr/Maghrib) that this "
                        "engine doesn't model; Fajr/Isha still land within ~1min."},
    "shymkent":    {"lat": 42.3000, "lon": 69.6000, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 506.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "taraz":       {"lat": 42.9000, "lon": 71.3667, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 658.0, "calibrated": False,
                "note": "Verified against sajda.com/kk/.../zhambyl-taraz for 24-27 Aug 2026: "
                        "Fajr/Isha match exactly (0min, confirms 15/15 angles are correct). "
                        "Small consistent residual on the rest (real time relative to raw calc): "
                        "Sunrise +2min, Dhuhr +3min, Asr +3-4min, Maghrib -2 to -3min. Smaller than "
                        "Almaty's 8-10min residual but a different shape than Astana's flat "
                        "+5min Dhuhr/Asr rule — not applied here since the engine only supports "
                        "that flat rule, not a per-prayer offset table. Good to ~3min without it."},
    "karaganda":   {"lat": 49.8047, "lon": 73.1094, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 553.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "aktobe":      {"lat": 50.2839, "lon": 57.2094, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 219.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "pavlodar":    {"lat": 52.2873, "lon": 76.9674, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 123.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "oskemen":     {"lat": 49.9714, "lon": 82.6059, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 290.0, "calibrated": False,
                "note": "Ust-Kamenogorsk. Reuses Astana/DUMK verified params at this city's own coordinates."},
    "semey":       {"lat": 50.4111, "lon": 80.2275, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 200.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "atyrau":      {"lat": 47.1164, "lon": 51.8814, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 0.0, "calibrated": False,
                "note": "Below/near sea level near the Caspian; elevation clamped to 0. "
                        "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "aktau":       {"lat": 43.6510, "lon": 51.1730, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 2.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "kostanay":    {"lat": 53.2144, "lon": 63.6246, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 172.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "petropavlovsk": {"lat": 54.8667, "lon": 69.1500, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 130.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "kyzylorda":   {"lat": 44.8479, "lon": 65.5093, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 130.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "oral":        {"lat": 51.2333, "lon": 51.3667, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 34.0, "calibrated": False,
                "note": "Uralsk. Reuses Astana/DUMK verified params at this city's own coordinates."},
    "kokshetau":   {"lat": 53.2833, "lon": 69.3833, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 340.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "turkestan":   {"lat": 43.3000, "lon": 68.2667, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 220.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "zhezkazgan":  {"lat": 47.8043, "lon": 67.7144, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 317.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "balkhash":    {"lat": 46.8481, "lon": 74.9950, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 343.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "ekibastuz":   {"lat": 51.7302, "lon": 75.3269, "tz": 5.0, "dst": False, "country": "KZ",
                "fajr_angle": 15.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "hanafi", "elevation": 168.0, "calibrated": False,
                "note": "Reuses Astana/DUMK verified params at this city's own coordinates."},
    "istanbul": {"lat": 41.0082, "lon": 28.9784, "tz": 3.0, "dst": False, "country": "TR",  # Turkey: permanent UTC+3 since 2016
                "fajr_angle": 18.0, "isha_angle": 17.0, "isha_offset_min": None,
                "asr_method": "standard", "elevation": 40.0, "calibrated": True,
                "note": "Diyanet applies its own 'ihtiyat' offsets beyond angle+elevation "
                        "(~5-7min on Sunrise/Dhuhr/Asr/Maghrib); Fajr/Isha land within ~1min."},
    "antalya": {"lat": 36.8969, "lon": 30.7133, "tz": 3.0, "dst": False, "country": "TR",
                "fajr_angle": 18.0, "isha_angle": 17.0, "isha_offset_min": None,
                "asr_method": "standard", "elevation": 30.0, "calibrated": False,
                "note": "Not independently verified — reuses Istanbul/Diyanet params "
                        "(same national authority)."},
    "dubai":   {"lat": 25.2048, "lon": 55.2708, "tz": 4.0, "dst": False, "country": "AE",
                "fajr_angle": 19.5, "isha_angle": None, "isha_offset_min": 90,
                "asr_method": "standard", "elevation": 5.0, "calibrated": True},
    "moscow":  {"lat": 55.7558, "lon": 37.6173, "tz": 3.0, "dst": False, "country": "RU",  # Russia: permanent UTC+3 since 2014
                "fajr_angle": 16.0, "isha_angle": 15.0, "isha_offset_min": None,
                "asr_method": "standard", "elevation": 0.0, "calibrated": True},
    "mecca":   {"lat": 21.3891, "lon": 39.8579, "tz": 3.0, "dst": False, "country": "SA",
                "fajr_angle": 18.5, "isha_angle": None, "isha_offset_min": 90,
                "asr_method": "standard", "elevation": 0.0, "calibrated": True},
    "makkah":  {"lat": 21.3891, "lon": 39.8579, "tz": 3.0, "dst": False, "country": "SA",
                "fajr_angle": 18.5, "isha_angle": None, "isha_offset_min": 90,
                "asr_method": "standard", "elevation": 0.0, "calibrated": True},
    "medina":  {"lat": 24.5247, "lon": 39.5692, "tz": 3.0, "dst": False, "country": "SA",
                "fajr_angle": 18.5, "isha_angle": None, "isha_offset_min": 90,
                "asr_method": "standard", "elevation": 0.0, "calibrated": True},
    "jeddah":  {"lat": 21.4858, "lon": 39.1925, "tz": 3.0, "dst": False, "country": "SA",
                "fajr_angle": 18.5, "isha_angle": None, "isha_offset_min": 90,
                "asr_method": "standard", "elevation": 0.0, "calibrated": True},
    # Not yet calibrated against real local sources — generic MWL-ish defaults,
    # Shafi'i asr (more common in Europe than Hanafi). Use with caution.
    "london":  {"lat": 51.5072, "lon": -0.1276, "tz": 0.0, "dst": True, "country": "GB",  # GMT/BST, DST last Sun Mar - last Sun Oct
                "fajr_angle": 18.0, "isha_angle": 17.0, "isha_offset_min": None,
                "asr_method": "standard", "elevation": 11.0, "calibrated": False},
    "paris":   {"lat": 48.8566, "lon": 2.3522, "tz": 1.0, "dst": True, "country": "FR",  # CET/CEST, same EU DST window
                "fajr_angle": 18.0, "isha_angle": 17.0, "isha_offset_min": None,
                "asr_method": "standard", "elevation": 35.0, "calibrated": False},
    "berlin":  {"lat": 52.5200, "lon": 13.4050, "tz": 1.0, "dst": True, "country": "DE",
                "fajr_angle": 18.0, "isha_angle": 17.0, "isha_offset_min": None,
                "asr_method": "standard", "elevation": 34.0, "calibrated": False},
    "rome":    {"lat": 41.9028, "lon": 12.4964, "tz": 1.0, "dst": True, "country": "IT",
                "fajr_angle": 18.0, "isha_angle": 17.0, "isha_offset_min": None,
                "asr_method": "standard", "elevation": 21.0, "calibrated": False},
    "madrid":  {"lat": 40.4168, "lon": -3.7038, "tz": 1.0, "dst": True, "country": "ES",
                "fajr_angle": 18.0, "isha_angle": 17.0, "isha_offset_min": None,
                "asr_method": "standard", "elevation": 667.0, "calibrated": False},
}

# IATA airport code -> city key in CITY_LOOKUP
AIRPORT_TO_CITY = {
    "nqz": "astana", "tse": "astana",   # Nursultan Nazarbayev / Astana Int'l (legacy TSE code)
    "ala": "almaty",
    "cit": "shymkent",
    "dmb": "taraz",
    "kgf": "karaganda",
    "akx": "aktobe",
    "pwq": "pavlodar",
    "ukk": "oskemen",
    "plx": "semey",
    "guw": "atyrau",
    "sco": "aktau",
    "ksn": "kostanay",
    "ppk": "petropavlovsk",
    "kzo": "kyzylorda",
    "ura": "oral",
    "kov": "kokshetau",
    "hsa": "turkestan",
    "dzn": "zhezkazgan",
    "ist": "istanbul", "saw": "istanbul",
    "ayt": "antalya",
    "dxb": "dubai", "dwc": "dubai",
    "svo": "moscow", "dme": "moscow", "vko": "moscow",
    "jed": "jeddah",
    "med": "medina",
    "lhr": "london", "lgw": "london", "ltn": "london",
    "cdg": "paris", "ory": "paris",
    "txl": "berlin", "ber": "berlin",
    "fco": "rome",
    "mad": "madrid",
}


def resolve_city(name: str):
    """Resolve a free-text city name or IATA code to (display_name, calibration_dict)."""
    key = name.strip().lower()
    if key in AIRPORT_TO_CITY:
        key = AIRPORT_TO_CITY[key]
    if key not in CITY_LOOKUP:
        raise KeyError(
            f"Unknown city/airport '{name}'. Add it to CITY_LOOKUP or AIRPORT_TO_CITY in location_timeline.py — "
            f"include fajr_angle/isha_angle (or isha_offset_min)/asr_method/elevation; if you don't have real "
            f"calibration data, reuse the nearest same-country/same-authority city's params and set calibrated=False."
        )
    display_name = name if name[:1].isupper() else key.capitalize()
    return display_name, CITY_LOOKUP[key]


def dst_adjusted_offset(base_offset_hours: float, observes_dst: bool, on_date: date) -> float:
    """
    EU-style DST: clocks forward last Sunday of March, back last Sunday of October.
    Only applies to cities flagged observes_dst=True (London, Paris, Berlin, Rome, Madrid).
    Astana/Almaty/Istanbul/Dubai/Moscow/Mecca/Medina/Jeddah do NOT observe DST.
    """
    if not observes_dst:
        return base_offset_hours

    def last_sunday(year: int, month: int) -> date:
        if month == 12:
            d = date(year + 1, 1, 1) - timedelta(days=1)
        else:
            d = date(year, month + 1, 1) - timedelta(days=1)
        while d.weekday() != 6:  # Sunday
            d -= timedelta(days=1)
        return d

    dst_start = last_sunday(on_date.year, 3)
    dst_end = last_sunday(on_date.year, 10)
    if dst_start <= on_date < dst_end:
        return base_offset_hours + 1.0
    return base_offset_hours


def build_timeline(home_base: dict, trips: list, days: int, start: date = None) -> dict:
    """
    home_base: {"city": str, "lat", "lon", "tz_offset_hours", "fajr_angle",
                "isha_angle", "isha_offset_min", "asr_method", "elevation"}
                (defaults to the calibrated Astana entry if fields are omitted)
    trips: list of {"start_date": "YYYY-MM-DD", "end_date": "YYYY-MM-DD", "city": str,
                     "depart_at": "HH:MM"?, "return_at": "HH:MM"?}
    days: number of days to generate, starting today (or `start` if given)
    """
    start = start or date.today()
    astana_defaults = CITY_LOOKUP["astana"]

    def loc_dict(display_name, calib, d):
        return {
            "city": display_name,
            "country": calib.get("country"),
            "lat": calib["lat"],
            "lon": calib["lon"],
            "tz_offset_hours": dst_adjusted_offset(calib["tz"], calib["dst"], d),
            "fajr_angle": calib["fajr_angle"],
            "isha_angle": calib["isha_angle"],
            "isha_offset_min": calib["isha_offset_min"],
            "asr_method": calib["asr_method"],
            "elevation": calib["elevation"],
            "calibrated": calib.get("calibrated", False),
        }

    def home_loc(d):
        return {
            "city": home_base["city"],
            "country": home_base.get("country", astana_defaults.get("country")),
            "lat": home_base["lat"],
            "lon": home_base["lon"],
            "tz_offset_hours": home_base.get("tz_offset_hours", astana_defaults["tz"]),
            "fajr_angle": home_base.get("fajr_angle", astana_defaults["fajr_angle"]),
            "isha_angle": home_base.get("isha_angle", astana_defaults["isha_angle"]),
            "isha_offset_min": home_base.get("isha_offset_min", astana_defaults["isha_offset_min"]),
            "asr_method": home_base.get("asr_method", astana_defaults["asr_method"]),
            "elevation": home_base.get("elevation", astana_defaults["elevation"]),
            "calibrated": home_base.get("calibrated", True),
        }

    # Pre-resolve trip date ranges -> city info
    trip_ranges = []
    for trip in trips:
        sd = date.fromisoformat(trip["start_date"])
        ed = date.fromisoformat(trip["end_date"])
        display_name, calib = resolve_city(trip["city"])
        trip_ranges.append((sd, ed, display_name, calib, trip.get("depart_at"), trip.get("return_at")))

    timeline = {}
    for i in range(days):
        d = start + timedelta(days=i)
        entry = None
        for sd, ed, display_name, calib, depart_at, return_at in trip_ranges:
            if sd <= d <= ed:
                dest_loc = loc_dict(display_name, calib, d)
                if d == sd and depart_at:
                    # Travel-out day: home until depart_at (origin clock), then destination.
                    entry = {"segments": [
                        {"until": depart_at, **home_loc(d)},
                        {"from": depart_at, **dest_loc},
                    ]}
                elif d == ed and return_at:
                    # Travel-back day: destination until return_at (destination clock), then home.
                    entry = {"segments": [
                        {"until": return_at, **dest_loc},
                        {"from": return_at, **home_loc(d)},
                    ]}
                else:
                    entry = dest_loc
                break  # first matching trip wins if overlapping ranges exist
        if entry is None:
            entry = home_loc(d)
        timeline[d.isoformat()] = entry

    return timeline


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--trips", default="-", help="Path to trips JSON file, or '-' for stdin (default: '-'). Pass '[]' worth of no trips by using an empty file or '[]' on stdin.")
    parser.add_argument("--days", type=int, default=45, help="Number of days to generate (default: 45)")
    parser.add_argument("--start", default=None, help="Start date YYYY-MM-DD (default: today)")
    parser.add_argument("--home-city", default=None, help="Defaults to the calibrated Astana entry")
    parser.add_argument("--home-lat", type=float, default=None)
    parser.add_argument("--home-lon", type=float, default=None)
    parser.add_argument("--home-tz-offset", type=float, default=None)
    args = parser.parse_args()

    if args.trips == "-":
        raw = sys.stdin.read().strip()
        trips = json.loads(raw) if raw else []
    else:
        with open(args.trips) as f:
            trips = json.load(f)

    astana = CITY_LOOKUP["astana"]
    home_base = {
        "city": args.home_city or "Astana",
        "lat": args.home_lat if args.home_lat is not None else astana["lat"],
        "lon": args.home_lon if args.home_lon is not None else astana["lon"],
        "tz_offset_hours": args.home_tz_offset if args.home_tz_offset is not None else astana["tz"],
    }
    if home_base["city"].strip().lower() != "astana":
        # A non-Astana home base with no calibration override falls back to
        # Astana's angles/asr/elevation, which is only a rough approximation.
        home_base["calibrated"] = False

    start = date.fromisoformat(args.start) if args.start else None
    timeline = build_timeline(home_base, trips, args.days, start)
    print(json.dumps(timeline, indent=2, ensure_ascii=False))


if __name__ == "__main__":
    main()
