#!/usr/bin/env python3
"""Prayer time (namaz) calculation engine — pure stdlib, no network access.

Implements the standard astronomical method used by praytimes.org and most
prayer-time apps: solar declination + equation of time (Meeus low-precision
sun position), then hour-angle formulas for each twilight/shadow condition.
"""

import argparse
import json
import math
from datetime import date, datetime


def _dsin(x):
    return math.sin(math.radians(x))


def _dcos(x):
    return math.cos(math.radians(x))


def _dtan(x):
    return math.tan(math.radians(x))


def _darcsin(x):
    return math.degrees(math.asin(max(-1.0, min(1.0, x))))


def _darccos(x):
    return math.degrees(math.acos(max(-1.0, min(1.0, x))))


def _darctan2(y, x):
    return math.degrees(math.atan2(y, x))


def _darccot(x):
    return math.degrees(math.atan2(1, x))


def _fix_angle(a):
    return a % 360.0


def _fix_hour(h):
    return h % 24.0


def _julian_day(year, month, day):
    if month <= 2:
        year -= 1
        month += 12
    a = year // 100
    b = 2 - a + a // 4
    return int(365.25 * (year + 4716)) + int(30.6001 * (month + 1)) + day + b - 1524.5


def _sun_position(jd):
    """Low-precision apparent solar declination and equation of time (hours)."""
    d = jd - 2451545.0
    g = _fix_angle(357.529 + 0.98560028 * d)        # mean anomaly
    q = _fix_angle(280.459 + 0.98564736 * d)         # mean longitude
    l = _fix_angle(q + 1.915 * _dsin(g) + 0.020 * _dsin(2 * g))  # ecliptic longitude
    e = 23.439 - 0.00000036 * d                      # obliquity of the ecliptic
    ra = _darctan2(_dcos(e) * _dsin(l), _dcos(l)) / 15.0
    eqt = q / 15.0 - _fix_hour(ra)
    decl = _darcsin(_dsin(e) * _dsin(l))
    return decl, eqt


def compute_prayer_times(
    lat,
    lon,
    date_obj,
    tz_offset_hours,
    fajr_angle=18.0,
    isha_angle=17.0,
    asr_shadow_factor=2,
    elevation=0.0,
    maghrib_angle=0.0,
    isha_minutes_after_maghrib=None,
):
    """Compute the 5 daily prayer times plus sunrise.

    Args:
        lat, lon: coordinates in decimal degrees (lon East positive).
        date_obj: a datetime.date.
        tz_offset_hours: fixed UTC offset in hours (e.g. 5 for Astana).
        fajr_angle, isha_angle: sun depression angle below horizon, degrees.
        asr_shadow_factor: 1 = Standard/Shafi'i, 2 = Hanafi.
        elevation: meters above sea level (adds a small dip correction to
            sunrise/sunset/maghrib).
        maghrib_angle: 0.0 means Maghrib = sunset (most conventions,
            including the Kazakhstan Muftiate method); set >0 for
            conventions that delay Maghrib by a fixed twilight angle.
        isha_minutes_after_maghrib: if set (e.g. 90), Isha is computed as
            Maghrib + this many minutes instead of via isha_angle — this is
            the Umm al-Qura convention (Saudi Arabia) and also used by the
            UAE. When set, isha_angle is ignored.

    Returns:
        dict with HH:MM string values for fajr, sunrise, dhuhr, asr,
        maghrib, isha.
    """
    base_jd = _julian_day(date_obj.year, date_obj.month, date_obj.day) - lon / (15 * 24)

    def mid_day(t):
        _, eqt = _sun_position(base_jd + t)
        return _fix_hour(12 - eqt)

    def sun_angle_time(angle, t, ccw=False):
        decl, _ = _sun_position(base_jd + t)
        noon = mid_day(t)
        num = -_dsin(angle) - _dsin(decl) * _dsin(lat)
        den = _dcos(decl) * _dcos(lat)
        h = _darccos(num / den) / 15.0
        return noon - h if ccw else noon + h

    def asr_time(shadow_factor, t):
        decl, _ = _sun_position(base_jd + t)
        angle = -_darccot(shadow_factor + _dtan(abs(lat - decl)))
        return sun_angle_time(angle, t)

    # Standard horizon dip (34' atmospheric refraction + 16' solar radius),
    # plus the small elevation correction from Meeus for sunrise/sunset.
    horizon_angle = 0.833 + 0.0347 * math.sqrt(max(elevation, 0.0))

    guess = {"fajr": 5.0, "sunrise": 6.0, "dhuhr": 12.0, "asr": 13.0, "sunset": 18.0, "isha": 18.0}
    for _ in range(2):  # one refinement pass is enough; declination/eqt drift slowly
        guess = {
            "fajr": sun_angle_time(fajr_angle, guess["fajr"] / 24, ccw=True),
            "sunrise": sun_angle_time(horizon_angle, guess["sunrise"] / 24, ccw=True),
            "dhuhr": mid_day(guess["dhuhr"] / 24),
            "asr": asr_time(asr_shadow_factor, guess["asr"] / 24),
            "sunset": sun_angle_time(horizon_angle, guess["sunset"] / 24),
            "isha": sun_angle_time(isha_angle, guess["isha"] / 24),
        }

    if maghrib_angle and maghrib_angle > 0.0:
        maghrib = sun_angle_time(maghrib_angle, guess["sunset"] / 24)
    else:
        maghrib = guess["sunset"]

    isha = maghrib + isha_minutes_after_maghrib / 60.0 if isha_minutes_after_maghrib is not None else guess["isha"]

    tz_adjust = tz_offset_hours - lon / 15.0

    def fmt(h):
        h = (h + tz_adjust) % 24.0
        total_minutes = round(h * 60)
        hh = (total_minutes // 60) % 24
        mm = total_minutes % 60
        return f"{hh:02d}:{mm:02d}"

    return {
        "fajr": fmt(guess["fajr"]),
        "sunrise": fmt(guess["sunrise"]),
        "dhuhr": fmt(guess["dhuhr"]),
        "asr": fmt(guess["asr"]),
        "maghrib": fmt(maghrib),
        "isha": fmt(isha),
    }


# Kaaba coordinates (precise, not the generic "Mecca" city centroid).
_KAABA_LAT = 21.4225
_KAABA_LON = 39.8262

_COMPASS = ["С", "ССВ", "СВ", "ВСВ", "В", "ВЮВ", "ЮВ", "ЮЮВ",
            "Ю", "ЮЮЗ", "ЮЗ", "ЗЮЗ", "З", "ЗСЗ", "СЗ", "ССЗ"]


def qibla_bearing(lat, lon):
    """Great-circle initial bearing (degrees, 0-360 clockwise from true North)
    from (lat, lon) to the Kaaba. Also returns a Russian compass label."""
    lat1, lon1 = math.radians(lat), math.radians(lon)
    lat2, lon2 = math.radians(_KAABA_LAT), math.radians(_KAABA_LON)
    dlon = lon2 - lon1
    x = math.sin(dlon) * math.cos(lat2)
    y = math.cos(lat1) * math.sin(lat2) - math.sin(lat1) * math.cos(lat2) * math.cos(dlon)
    bearing = (math.degrees(math.atan2(x, y)) + 360.0) % 360.0
    label = _COMPASS[round(bearing / 22.5) % 16]
    return round(bearing, 1), label


def _parse_args():
    p = argparse.ArgumentParser(description="Compute daily Islamic prayer times.")
    p.add_argument("--lat", type=float, required=True, help="Latitude in decimal degrees")
    p.add_argument("--lon", type=float, required=True, help="Longitude in decimal degrees (East positive)")
    p.add_argument("--tz", type=float, required=True, help="Fixed UTC offset in hours, e.g. 5")
    p.add_argument("--date", type=str, required=True, help="Date as YYYY-MM-DD")
    p.add_argument("--fajr-angle", type=float, default=18.0, help="Fajr twilight angle, degrees (default 18)")
    p.add_argument("--isha-angle", type=float, default=17.0, help="Isha twilight angle, degrees (default 17)")
    p.add_argument(
        "--asr-method",
        choices=["standard", "hanafi"],
        default="hanafi",
        help="Asr shadow method: standard (factor 1) or hanafi (factor 2). Default hanafi.",
    )
    p.add_argument("--elevation", type=float, default=0.0, help="Elevation in meters (default 0)")
    p.add_argument("--maghrib-angle", type=float, default=0.0, help="Maghrib twilight angle, 0 = at sunset (default)")
    return p.parse_args()


def main():
    args = _parse_args()
    d = datetime.strptime(args.date, "%Y-%m-%d").date()
    shadow_factor = 2 if args.asr_method == "hanafi" else 1
    times = compute_prayer_times(
        lat=args.lat,
        lon=args.lon,
        date_obj=d,
        tz_offset_hours=args.tz,
        fajr_angle=args.fajr_angle,
        isha_angle=args.isha_angle,
        asr_shadow_factor=shadow_factor,
        elevation=args.elevation,
        maghrib_angle=args.maghrib_angle,
    )
    print(json.dumps({"date": args.date, "lat": args.lat, "lon": args.lon, "tz": args.tz, **times}, indent=2))


if __name__ == "__main__":
    main()
