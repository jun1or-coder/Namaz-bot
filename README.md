# namaz-bot / prayer_times.py

Pure-Python (stdlib only: `math`, `datetime`, `argparse`, `json`), dependency-free
calculation of the 5 daily prayer times, tuned to match the **Sajda app** for
Astana, Kazakhstan.

## Method

Sajda (per its own site, `sajda.com/kk/...`) labels its Kazakhstan times as the
**ҚМДБ / KMDB (Kazakhstan Muftiate, i.e. DUMK)** method with **Hanafi Asr**.
KMDB doesn't publish its exact twilight angles publicly, so the angles below
were reverse-engineered by cross-checking this engine's output against a live
Sajda page for Astana:

- **Fajr angle: 15°**
- **Isha angle: 15°**
- **Asr: Hanafi** (shadow factor 2)
- **Elevation: 347 m** (Astana's approximate altitude — affects sunrise/sunset dip)
- **Maghrib = sunset** (no extra angle)

These are the script's defaults. If you ever need a different convention
(e.g. Muslim World League 18°/17°, or Standard/Shafi'i Asr), pass different
CLI flags — nothing is hardcoded beyond the defaults.

### Cross-check vs. Sajda (Astana, 2026-08-19)

| Prayer  | This script | Sajda (KMDB) | Diff |
|---------|-------------|--------------|------|
| Fajr    | 03:18       | 03:18        | 0 min |
| Sunrise | 05:02       | 05:02        | 0 min |
| Dhuhr   | 12:18       | 12:23        | +5 min |
| Asr     | 17:15       | 17:20        | +5 min |
| Maghrib | 19:33       | 19:33        | 0 min |
| Isha    | 21:16       | 21:16        | 0 min |

Fajr, Sunrise, Maghrib and Isha match Sajda to the minute. Dhuhr and Asr are a
consistent 5 minutes later on Sajda's side — this looks like KMDB applies a
fixed +5 min safety margin to Dhuhr (and Asr, which is anchored after Dhuhr),
a common convention among Muftiate-published timetables that sits on top of
the pure astronomical calculation rather than being part of it. This script
does *not* bake in that margin (it computes true solar values), so expect
Dhuhr/Asr to run ~5 minutes ahead of Sajda's published time. Everything else
should match closely.

**Caveat:** this is a best-effort match, not a guaranteed bit-for-bit
replica of SajdaApp. KMDB's exact internal parameters aren't publicly
documented; the angles above were fit against one date/location and could
drift slightly for other dates, or if Sajda changes its backend.

## Usage

```
python3 prayer_times.py --lat 51.1801 --lon 71.4460 --tz 5 --date 2026-08-19 \
  --fajr-angle 15 --isha-angle 15 --asr-method hanafi --elevation 347
```

Prints JSON:

```json
{
  "date": "2026-08-19",
  "lat": 51.1801,
  "lon": 71.446,
  "tz": 5.0,
  "fajr": "03:18",
  "sunrise": "05:02",
  "dhuhr": "12:18",
  "asr": "17:15",
  "maghrib": "19:33",
  "isha": "21:16"
}
```

### CLI flags

| Flag | Default | Meaning |
|------|---------|---------|
| `--lat` | required | Latitude, decimal degrees |
| `--lon` | required | Longitude, decimal degrees (East positive) |
| `--tz` | required | Fixed UTC offset in hours (e.g. `5` for Astana) |
| `--date` | required | `YYYY-MM-DD` |
| `--fajr-angle` | `18.0` | Fajr twilight depression angle, degrees |
| `--isha-angle` | `17.0` | Isha twilight depression angle, degrees |
| `--asr-method` | `hanafi` | `hanafi` (shadow factor 2) or `standard` (factor 1, Shafi'i/Maliki/Hanbali) |
| `--elevation` | `0.0` | Meters above sea level |
| `--maghrib-angle` | `0.0` | `0` = Maghrib at sunset; set >0 to delay Maghrib by a fixed angle |

Note: the CLI defaults (`--fajr-angle 18 --isha-angle 17`) are the generic
Muslim World League values, for use as a general-purpose fallback. For the
Astana/SajdaApp-matched behavior documented above, pass `--fajr-angle 15
--isha-angle 15 --elevation 347` explicitly, as shown in the example.

### Python API

```python
from datetime import date
from prayer_times import compute_prayer_times

times = compute_prayer_times(
    lat=51.1801, lon=71.4460, date_obj=date(2026, 8, 19),
    tz_offset_hours=5, fajr_angle=15.0, isha_angle=15.0,
    asr_shadow_factor=2, elevation=347.0,
)
# -> {'fajr': '03:18', 'sunrise': '05:02', 'dhuhr': '12:18',
#     'asr': '17:15', 'maghrib': '19:33', 'isha': '21:16'}
```

## Algorithm

Standard astronomical method (the same public-domain formulas underlying
praytimes.org and most prayer-time software): low-precision solar position
(apparent ecliptic longitude, declination, equation of time) from Meeus,
then hour-angle formulas per prayer:

- **Dhuhr**: local solar noon (12:00 corrected by the equation of time and
  longitude).
- **Fajr / Isha**: time when the sun is `N°` below the horizon (before/after
  solar noon).
- **Sunrise / Sunset**: time when the sun's upper limb crosses the horizon
  (0.833° dip for refraction + solar radius, plus a small elevation
  correction).
- **Asr**: time when an object's shadow length equals `factor × object
  height + noon shadow` (factor 1 = Standard, factor 2 = Hanafi).
- **Maghrib**: sunset, unless a nonzero `--maghrib-angle` is given.

Verified across 5 dates spanning the year for Astana (51.1801°N, 71.4460°E,
UTC+5), including both solstices — no degenerate/undefined results at this
latitude (day-length swings from ~4h50m Fajr-to-sunrise in June to sunrise
~08:10 in December, all physically sane).
