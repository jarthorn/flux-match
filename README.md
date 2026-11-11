# flux-match

## Overview

Flux Match is a simple dispatch model that simulates dispatching variable generation resources (wind, solar, batteries), and measures how much they satisfy grid demand in regions around Canada. Historical hourly demand data are used in the calculation, but the generation is based on a hypothetical allocation of wind, solar, and batteries in each region. The purpose of this model is to explore the impact on different site selection strategies on satisfying a real demand curve. Note that this is a very simplistic model, and excludes many considerations, including economics and grid connection costs. The site selection granularity is also fairly coarse, as it selects from a series of map grid cells of approximately 2,500 km^2 in size.

## Usage

Each run is configured by a scenario file, `scenarios/<name>.toml`. Shared parameters live in `scenarios/defaults.toml`; a scenario only needs to set what it overrides (including `selection.strategy`). Steps 2-4 write to a new run folder, `output/<YYYYMMDD-HHMM>-<scenario>/`, which also holds a `config.toml` snapshot of the resolved defaults+scenario merge and a `run.json` manifest (git commit, completed steps, and per-balancing-area `dispatch_stats` for comparing site selection strategies).

```bash
bin/run [--scenario NAME] [--config KEY=VALUE ...]   # steps 1-4 in a new run folder (default scenario: "balanced")
bin/run --batch BATCH [--scenario NAME] [--config ...]  # as above, in output/BATCH/NNNN, keeping only config.toml, run.json and selected_sites.csv
uv run flux-match fetch [--scenario NAME] [--config ...]  # step 1 only, into data/
uv run flux-match select [--scenario NAME [--config ...] | --run DIR]
uv run flux-match preprocess --run output/<run>      # overwrites this step's outputs in place
uv run flux-match dispatch --run output/<run>
bin/viz output/<run>                                 # step 5: Streamlit dashboard
bin/cf-anim --type wind|solar                        # hourly capacity-factor map animation
bin/batch-summary --batch BATCH                      # summary.csv and invariants.toml for a batch
bin/test                                             # lint and unit tests
bin/format                                           # ruff fix and format
```

## Batch Mode

Batch mode (`--batch BATCH`, `all` only) is for large numbers of runs where only the site selection and `dispatch_stats` matter. All runs with the same batch name go in one folder, `output/<BATCH>/`, each in a subfolder named by its zero-padded batch index (`0001`, `0002`, ...). Each new run takes the index one past the highest already in the folder, so re-using a batch name adds runs to it. `run.json` records `batch_name` and `batch_index` (both `null` for a normal run). The batch name must be a plain folder name and cannot be an existing run folder. Only config.toml, run.json and selected_sites.csv are kept. Those three files have the same contents as in a normal run; `clusters.csv` and `dispatch_hourly_<year>.csv` are never written (dispatch still runs, to compute the stats), and `vre_by_ba_hourly.csv` is deleted when the run ends, including on failure. `bin/viz` cannot display a batch run, and individual steps cannot be re-run on it, since the hourly inputs are gone.

When a step is re-run on an existing folder, it reads that folder's `config.toml`, not `scenarios/`, and later steps are marked incomplete until they are re-run.

When running in batch mode, it is helpful to be able to override specific config values in order to test a possibility space of values. `--config` overrides individual values for a new run, taking precedence over the scenario file and `defaults.toml`. Each override is a dotted key and a TOML value; a value that is not valid TOML is taken as a plain string. Several overrides can follow one `--config`, or the option can be repeated:

```bash
bin/run --batch dispatch-years --config 'dispatch.dispatch_years=[2023, 2024]' --config selection.greedy.ranking_metric=median_daily_capacity
```

The key must already exist in the resolved scenario and defaults, so a typo fails instead of being ignored; switching to a strategy whose table the scenario lacks needs a scenario file. The run's `config.toml` holds the overridden values, and `run.json` lists the overrides under `config_overrides`. `--config` cannot be combined with `--run`.

`bin/batch-summary --batch BATCH` collates every run in `output/BATCH/` for offline analysis, writing two files to that folder:

- `summary.csv`: one row per run, sorted by batch index. Columns are `batch_index` and `started`, then each `config.toml` value that differs between runs (or is missing from some, left blank there), named by dotted key (`selection.wind_capacity_ratio`), then these totals from `dispatch_stats`: `net_demand_pct`, `curtailment_pct` (0.0 where there is no variable generation, which `run.json` records as null), `satisfied_demand_hours_pct`, and `peak_net_demand_mw` with its balancing area `peak_net_demand_ba` (the highest of the per-area peaks).
- `invariants.toml`: the `config.toml` values that are identical in every run, as nested tables.

Every `config.toml` value is in exactly one of the two files. An integer and a float of the same value (`4` and `4.0`) count as identical, and `invariants.toml` keeps the lowest-indexed run's form; booleans never match numbers. Every subfolder of the batch folder must be a completed run (`dispatch` in `completed_steps`) whose `batch_index` matches its folder name; otherwise each bad run is reported and nothing is written.

## Layout

- `src/flux_match/`: the package. `cli.py` is the entry point; `steps/` holds one sub-package per pipeline step (`fetch/`, `select/`, `preprocess/`, `dispatch/`). Site-selection strategies live under `steps/select/` (one module per strategy, registered in `steps/select/__init__.py`; `total_variation.py` is a shared helper, the Total Variation Complementarity Index, rather than a strategy); `viz/` holds the dashboard. Shared code: `config.py`, `run_context.py`, `paths.py`, `io.py`, `schema.py`, `errors.py`.
- `scenarios/`: scenario TOML files, plus `defaults.toml` for shared baseline values.
- `data/`: raw inputs (git-ignored). `output/`: run folders (git-ignored).
- `bin/`: batch scripts.
- `tests/`: unit tests (`unittest`).

## Implementation Details

Flux Match is a very simple energy dispatch model. It is given a set of inputs from the [CODERS](https://coders.cme-emh.ca/) database:
- Hourly electricity demand curves per year
- A list of map grid cells that cover the populated areas of Canada
- Hourly load factors for wind and solar generation in each map grid cell (wind can alternatively come from [renewables.ninja](https://www.renewables.ninja/); see `fetch.wind_source`)
- Both demand and capacity are segmented by balancing areas, which are regions of Canada that roughly follow the major segments of the Canadian electricity grid. Most provinces have a single balancing area, but some larger provinces have two.
- A list of wind and solar generation sites, with a given nameplate capacity and grid cell

The model also assumes some battery storage capacity pooled per balancing area. Battery storage capacity is specified in the model configuration using `bess_capacity_ratio`, a ratio of batter capacity to the area's average daily demand in the most recent available demand year. Battery power rating is the total capacity divided by `bess_duration` hours. The power rating sets the maximum charge and discharge rate for that storage. Round-trip efficiency is a scenario parameter, applied entirely on charge; discharge is lossless.

All capacity and demand units are in MW and MWh, unless otherwise noted. Every year has 8760 hours, indexed 1-8760. In leap years, the fetch step drops February 29 (hour-ending 1417-1440) and renumbers the remaining hours, so March 1 starts at hour 1417. Demand from one year may be dispatched against capacity factors from another, so all years must line up hour for hour.

Given these inputs, flux-match runs through the hourly increments of a year, for each balancing area. For each hour it subtracts variable generation from demand. Excess generation is stored in the balancing area's battery storage if possible (subject to both power and capacity rating of batteries). Capacity shortfall is taken from storage if capacity is available. The net demand curve is calculated and saved for each hour. The following values are saved for each hour increment, in one CSV file per dispatch year (`dispatch_hourly_<year>.csv`):
- Calendar year
- Balancing Area Name
- Hour increment (1-8760)
- Variable generation (wind+solar)
- Battery output (positive for discharge, negative for recharge)
- Net demand (input_demand - solar_output - wind_output - battery_output))

The program is divided into the following steps:
1. Fetch raw input data from CODERS and, optionally, renewables.ninja. Downloads are skipped if the files are already present. Wind capacity factor
data can be fetched from either CODERS or renewables.ninja, depending on `fetch.wind_source`. Note that NINJA downloads take multiple days due to rate limiting.
The fetch step also derives `data/grid_cell_rankings_<wind source>.csv` (`_coders` or `_ninja`), with one row per grid cell: annual capacity-factor sums for wind and solar, the median daily capacity (sum of a day's 24 hourly factors) over the year, and each cell's rank within its balancing area on each of those four measures (1 = highest; ties broken by `grid_cell_info.csv` order). It is rebuilt when missing or older than its inputs, and requires exactly one `capacity_factor_years` entry.
Fetch also derives `data/balancing_area_demand.csv`: one row per balancing area, one column per demand year, each cell the total annual demand in MWh. CODERS demand is provincial, so each province's total is split among its balancing areas by population (`PROVINCE_BA_POPULATION` in `balancing_area_demand.py`). It uses every `provincial_demand_*` file in `data/coders/`, regardless of `demand_years`, fails if any year found lacks a province, and is rebuilt only when missing or older than one of those files. Finally, for each of `fetch.demand_years` it derives `data/balancing_area_demand_hourly_<year>.csv`: hourly demand per balancing area, split by the same population shares and sorted by balancing area then hour.
2. Wind and solar site selection. This step employs a variety of strategies to select a set of wind and solar generation sites across Canada. Each strategy has a short name represented as a single string. See the Site Selection Algorithms section below for details on each strategy. Every strategy writes the same output, `selected_sites.csv`: a sparse table with one row per grid cell and generation type (`wind_ons`, `wind_ofs`, `solar`) that has capacity. The format is specified in `steps/select/base.py`.
3. Preprocess input data into the format expected for the dispatch step. This consists of calculating the hourly wind and solar generation per balancing area, using the wind and solar sites selected in step 2, combined with the capacity factors per grid cell fetched in 1.
4. Dispatch. This step runs through hourly increments for each balancing area, as described above, streaming the hourly demand file for each of `dispatch_years` alongside `vre_by_ba_hourly.csv`. Each year and balancing area starts with the battery at `bess_initial_soc`; state of charge is not carried between years. The single capacity-factor year is reused for every dispatch year, and each dispatch year must also be in `fetch.demand_years`.
5. Visualization (optional). This is a standalone script that generates a streamlit dashboard for visualizing the results of the run.

## Site Selection Algorithms

### Greedy

 The default strategy is called "greedy" and fills cells in rank order (1 = highest) up to a maximum capacity per map grid cell. `[selection.greedy] ranking_metric` chooses the ranking: `annual_capacity` or `median_daily_capacity`. This strategy provides a naive baseline that we can use to evaluate against more sophisticated approaches.

### Cluster

The "cluster" strategy (see `docs/clustering-approach.md`) splits each balancing area's cells into `[selection.cluster] cluster_count` geographic clusters with K-means (seeded by `seed`, restarted `n_init` times). The strategy then fills cells in rounds, taking the best remaining cell by annual capacity factor from every cluster in each round; it also writes the clusters to `clusters.csv` in the run folder for offline analysis.

### Total Variation Complementarity Index

The "tvci" strategy (see `docs/complementarity-approach.md`) picks sites whose hourly output complements the sites already picked, separately for each balancing area and for wind and solar. With N = ceil(target / per-cell cap) sites needed, the candidates are the area's top ceil(`[selection.tvci] candidate_multiple` * N) cells by annual capacity factor. The best candidate is picked first; each later pick is the candidate with the highest Total Variation Complementarity Index against the mean hourly capacity factor of the cells picked so far. With `candidate_multiple = 1` it uses the same cells as greedy by annual capacity.
