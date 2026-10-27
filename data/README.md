# data

Raw input data, one sub-folder per source. Everything here except this file is ignored by git.

- `coders/`: files downloaded from [CODERS](https://coders.cme-emh.ca/) by `flux-match fetch`.
  Existing files are not re-downloaded; delete them to force a refresh. Hourly tables always
  have 8760 hours: February 29 is dropped from leap years, and later hours are renumbered.
- `balancing_area_demand.csv`: total annual demand (MWh) per balancing area (rows) and demand year (columns), derived by the end of `flux-match fetch` from every `provincial_demand_*` file in `coders/`. Rebuilt only when missing or older than one of those files.
- `grid_cell_rankings.csv`: derived by the end of `flux-match fetch` from the files in `coders/`; delete it, or refresh its inputs, to rebuild it.
