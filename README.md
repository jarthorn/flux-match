# flux-match

Flux Match is a very simple energy dispatch model. It is given a set of inputs from the [CODERS](https://coders.cme-emh.ca/) database:
- Hourly electricity demand curves for a year
- A list of map grid cells that cover the populated areas of Canada
- Hourly load factors for wind and solar generation in each map grid cell
- Both demand and capacity are segmented by balancing areas, which are regions of Canada that roughly follow the major segments of the Canadian electricity grid. Most provinces have a single balancing area, but some larger provinces have two.
- A list of wind and solar generation sites, with a given nameplate capacity and grid cell
- Available battery storage power and capacity per balancing area. The power rating sets the maximum charge and discharge rate for that storage. Round-trip efficiency is a scenario parameter, applied entirely on charge; discharge is lossless.
- All capacity and demand units are in MW and MWh, unless otherwise noted. Every year has 8760 hours, indexed 1-8760. In leap years, the fetch step drops February 29 (hour-ending 1417-1440) and renumbers the remaining hours, so March 1 starts at hour 1417. Demand from one year may be dispatched against capacity factors from another, so all years must line up hour for hour.

Given these inputs, flux-match runs through the hourly increments of a year, for each balancing area. For each hour it subtracts variable generation from demand. Excess generation is stored in the balancing area's battery storage if possible (subject to both power and capacity rating of batteries). Capacity shortfall is taken from storage if capacity is available. The net demand curve is calculated and saved for each hour. The following values are saved for each hour increment, in one CSV file per dispatch year (`dispatch_hourly_<year>.csv`):
- Calendar year
- Balancing Area Name
- Hour increment (1-8760)
- Variable generation (wind+solar)
- Battery output (positive for discharge, negative for recharge)
- Net demand (input_demand - solar_output - wind_output - battery_output))

The program is divided into the following steps:
1. Fetch raw input data from CODERS. Downloads are skipped if the files are already present. The step ends by deriving `data/grid_cell_rankings.csv`, with one row per grid cell: annual capacity-factor sums for wind and solar, the median daily capacity (sum of a day's 24 hourly factors) over the year, and each cell's rank within its balancing area on each of those four measures (1 = highest; ties broken by `grid_cell_info.csv` order). It is rebuilt when missing or older than its inputs, and requires exactly one `capacity_factor_years` entry. It then derives `data/balancing_area_demand.csv`: one row per balancing area, one column per demand year, each cell the total annual demand in MWh. CODERS demand is provincial, so each province's total is split among its balancing areas by population (`PROVINCE_BA_POPULATION` in `balancing_area_demand.py`). It uses every `provincial_demand_*` file in `data/coders/`, regardless of `demand_years`, fails if any year found lacks a province, and is rebuilt only when missing or older than one of those files.
2. Wind and solar selection. This step employs a variety of strategies to select a set of wind and solar generation sites across Canada. Each strategy has a short name represented as a single string. The default strategy is called "greedy" and simply selects locations that maximize annual output, up to a maximum output per map grid cell. Every strategy writes the same output, `selected_sites.csv`: a sparse table with one row per grid cell and generation type (`wind_ons`, `wind_ofs`, `solar`) that has capacity. The format is specified in `selection/base.py`.
3. Preprocess input data into the format expected for the dispatch step. This consists of calculating the hourly wind and solar generation per balancing area, using the wind and solar sites selected in step 2, combined with the capacity factors per grid cell fetched in 1.
4. Dispatch. This step runs through hourly increments for each balancing area, as described above.
5. Visualization. This is a standalone script that generates a streamlit dashboard for visualizing the results of the run.

## Usage

Each run is configured by a scenario file, `scenarios/<name>.toml`, which sets every parameter the run uses, including strategy-specific ones. Steps 2-4 write to a new run folder, `output/<YYYYMMDD-HHMM>-<scenario>/`, which also holds a `config.toml` snapshot of the scenario and a `run.json` manifest (git commit, completed steps).

```bash
bin/run [--scenario NAME]                            # steps 1-4 in a new run folder (default scenario: "default")
uv run flux-match fetch [--scenario NAME]            # step 1 only, into data/coders/
uv run flux-match select [--scenario NAME | --run DIR]
uv run flux-match preprocess --run output/<run>      # overwrites this step's outputs in place
uv run flux-match dispatch --run output/<run>
bin/viz output/<run>                                 # step 5: Streamlit dashboard
bin/test                                             # lint and unit tests
bin/format                                           # ruff fix and format
```

When a step is re-run on an existing folder, it reads that folder's `config.toml`, not `scenarios/`, and later steps are marked incomplete until they are re-run.

## Layout

- `src/flux_match/`: the package. `cli.py` is the entry point; `steps/` holds fetch, preprocess and dispatch; `selection/` holds the site-selection strategies (one module per strategy, registered in `selection/__init__.py`); `viz/` holds the dashboard. Shared code: `config.py`, `run_context.py`, `paths.py`, `io.py`, `schema.py`, `errors.py`.
- `scenarios/`: scenario TOML files.
- `data/`: raw inputs (git-ignored). `output/`: run folders (git-ignored).
- `bin/`: batch scripts. `tests/`: unit tests (`unittest`).
