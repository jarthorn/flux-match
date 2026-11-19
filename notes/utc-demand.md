# Plan: move hourly demand to UTC

## Problem

Dispatch pairs demand and variable generation by hour index (1-8760) with no time conversion, but the two series use different clocks:

- CODERS `provincial_demand_<province>_<year>.csv` is in **local standard time**. The column is named `local_time`, and daily demand bottoms out around 02:00-04:00 and peaks around 17:00-18:00 on that clock in every province.
- CODERS solar and wind capacity factors are in **UTC**. Each grid cell's average daily solar profile is centred within about half an hour of UTC solar noon for its longitude, in every province. renewables.ninja wind is requested in UTC (`local_time=false`) and collated without a shift.

As a result, solar is paired with demand 4-8 hours too late (about 5 hours in Ontario, 8 in BC). Ontario's 13:00 EST solar peak lands on 18:00 demand, which overlaps the evening peak and the hours the battery would otherwise cover. Past results probably overstate how well solar and storage meet demand.

### Demand ignores daylight saving

So a constant shift per province is correct:

- Every demand file steps exactly one hour between rows, with no gap in March and no repeated hour in November.
- In provinces that observe daylight saving, the weekday morning ramp moves about one hour earlier on the file's clock after the March change and back after the November change. This is what clock-driven behaviour looks like when recorded in standard time.
- Saskatchewan, which does not observe daylight saving, shows no such jump.

| Province | Ramp centroid, 2 weeks before / after March change | Before / after November change |
|---|---|---|
| ON 2022 | 6.08 / 5.40 | 5.31 / 6.21 |
| BC 2022 | 6.10 / 5.43 | 5.31 / 6.11 |
| SK 2022 (no DST) | 6.34 / 6.45 | 6.92 / 6.90 |

## Decisions

- **The whole project uses UTC.** All hourly series (demand, capacity factors, variable generation, dispatch output) are indexed by UTC hour-ending 1-8760. This keeps provinces aligned for future import/export modelling, and lets site selection use the normalized demand curve directly.
- **Convert demand once, in fetch,** when deriving `balancing_area_demand_hourly_<year>.csv`. The raw CODERS files stay as downloaded.
- **One fixed offset per province.** CODERS demand is provincial, so it can't be split by timezone within a province.
- **Wrap the year.** The first few UTC hours of January 1 take their values from the last few local hours of December 31 of the same year.
- **Ignore the 30 minute offset for Newfoundland.** We will treat all of Newfoundland and Labrador as Atlantic Standard Time. Interpolating a 30 minute shift for half the province is too difficult, and NL is hydro-dominated so not a particular focus for this project.

### Offsets (local standard time minus UTC)

| Province | Offset | Notes |
|---|---|---|
| BC | -8 (PST) | Northeast BC (Peace region) is on MST year round; ignored. |
| AB | -7 (MST) | |
| SK | -6 (CST) | |
| MB | -6 (CST) | |
| ON | -5 (EST) | Northwestern Ontario is on Central time; ignored. |
| QC | -5 (EST) | Magdalen Islands and far-eastern Quebec are on Atlantic time; ignored. |
| NB, NS, PE | -4 (AST) | |
| NL | -4 (AST) | Island of Newfoundland is -3:30 and most of Labrador is -4. AST avoids a half-hour shift; |

## Implementation

### 1. Shift provincial demand to UTC (`balancing_area_demand.py`)

- Add `PROVINCE_UTC_OFFSET_HOURS: dict[str, int]`, keyed by province code like `PROVINCE_NAMES`, with the values above.
- Add a helper, e.g. `local_to_utc(values, utc_offset_hours)`, that rotates one province's hourly series. With `k = -utc_offset_hours`, UTC hour-ending `u` takes local hour-ending `((u - 1 - k) mod 8760) + 1`. For Ontario (`k = 5`), UTC hour 6 (05:00-06:00 UTC) is local hour 1 (00:00-01:00 EST), and UTC hours 1-5 wrap to local hours 8756-8760.
- In `build_hourly_ba_demand`, rotate each province's series **before** splitting it among balancing areas and summing. This matters for `Ontario.a`, which gets shares from both Ontario and Quebec. Both are EST today, but the order must stay right if offsets change.
- Hard-fail with `DataError` if a province has no offset, and add a test that the offset keys match `PROVINCE_NAMES` exactly, like the existing population test.
- Leave the annual totals (`balancing_area_demand.csv`, `build_ba_demand`) alone. Rotation preserves each year's sum, so battery sizing doesn't change.
- Update the module docstring: hourly output is UTC hour-ending, shifted from local standard time by a fixed offset per province, with the year wrapped.

### 2. Leap years

The fetch step drops February 29 from the local-time demand file and from the UTC capacity-factor files, each in its own clock. Rotating after the drop is still exact everywhere except the year wrap. For example, UTC hour 1417 (March 1, 00:00 UTC) maps to local hour 1412 (February 28, 19:00 EST), which is the same instant. No change is needed in `_drop_leap_day`, but add a short comment or test to show the order (drop, then shift) is deliberate.

### 3. No special treatment for stale hourly files

Users of this application will be told to clean and rebuild their data/ folder after this change, so we don't need to worry about the risk of stale `balancing_area_demand_hourly_YYYY.csv` files. Retain the usual logic that these files should only be rebuilt if raw inputs are newer. In this workspace, the old files with mixed timezones have been moved into the folder `data/old-mixed-timezone-data/`. You can use these for reference, but then derive new data files with normalized timezones directly in `data/` (the paths already used in the code today).

- `normalized_demand_hourly.csv` is rebuilt by preselect whenever any input hourly file is newer, so it follows automatically. It has been deleted anyway as part of the preparation for rollout.
- Update every reference to the old filename: `paths.py`, `README.md`, `data/README.md`, docstrings in `fetch.py`, `preselect.py`, `dispatch.py` and `charts.py`.

### 4. Dashboard and docs

- `viz/app.py` caption: replace "There is no timezone and no daylight-saving shift" with "Times are UTC". `charts.py` `hour_to_display_time` / `format_display_time` docstrings: same. Consider adding " UTC" to axis titles. `cf_animation.py` already labels UTC.
- `README.md` Implementation Details: State that as a design principle of this project, all data files other than raw downloads always store time in UTC. Visualizations can display local time if requested explicitly by the app developers. State that every hourly series is indexed by UTC hour-ending 1-8760, and that demand is shifted from local standard time with the offsets above. Describe the year wrap and the single-offset-per-province approximation.
- `data/README.md`: note that the hourly demand files are UTC, while the raw CODERS demand files are local standard time.

### 5. Tests

In `tests/test_balancing_area_demand.py`:

- **Rotation:** with a short `hours_in_year` (or a hand-built 8760 series), check the mapping for a known offset, including the wrap at the start of the year, and that totals are preserved.
- **Order:** in `build_hourly_ba_demand`, a balancing area spanning two provinces with different offsets gets each province's share shifted by its own offset before summing. Use a patched offset table so the test doesn't depend on ON and QC both being EST.
- **Coverage:** a missing offset fails; the offset keys match `PROVINCE_NAMES`.
- **Written file:** update the existing `write_hourly_ba_demand` test so it expects shifted hours, while annual totals still match `balancing_area_demand.csv`.

## Rollout and verification

1. Implement the steps above and run `bin/test`, `ruff check` and `ruff format`.
2. Delete `data/balancing_area_demand_hourly_*.csv` and `data/normalized_demand_hourly.csv`, then run `flux-match fetch` and `flux-match preselect`.
3. One-off sanity check: in every balancing area, the hour of minimum mean daily demand in the new files should move later by exactly that province's offset. The local minimum is around 02:00-04:00, so that's about 07:00-09:00 UTC in Ontario and 10:00-12:00 UTC in BC.
4. Re-run a reference scenario and compare `dispatch_stats` with a pre-fix run. Expect higher net demand, and probably different curtailment, in solar-heavy areas.
5. Treat all existing run folders and batch results as invalid. Re-running `dispatch --run` on an old folder will use UTC demand, since `vre_by_ba_hourly.csv` was already UTC.
