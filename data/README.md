# data

Raw input data, one sub-folder per source. Everything here except this file is ignored by git.

- `coders/`: files downloaded from [CODERS](https://coders.cme-emh.ca/) by `flux-match fetch`.
  Existing files are not re-downloaded; delete them to force a refresh. Hourly tables always
  have 8760 hours: February 29 is dropped from leap years, and later hours are renumbered.
- `ninja/`: written by `flux-match fetch` when `fetch.ninja.enabled` is true. `raw/<turbine model>_<height>m_<year>/wind_<grid_cell>.csv` is one [renewables.ninja](https://www.renewables.ninja/) response per grid cell, as downloaded: hourly `time` (UTC) and `electricity` (kW, for 100,000 kW of turbines), a full year including February 29 in leap years. Existing files are not re-downloaded. `.requests.log` holds recent request times for the rate limit; delete it only if you know NINJA's hourly limit has reset. `wind_capacity_factor_<year>.csv` collates the raw files like CODERS' `wind_capacity_factor_<year>.csv` (`h` of 1-8760 without February 29, then one capacity-factor column per grid cell, in `grid_cell_info.csv` order). Cells with no raw file yet are 0, with a warning; it is rebuilt when any raw file is newer. Not used by the rest of the application yet.
- `balancing_area_demand.csv`: total annual demand (MWh) per balancing area (rows) and demand year (columns), derived by the end of `flux-match fetch` from every `provincial_demand_*` file in `coders/`. Rebuilt only when missing or older than one of those files.
- `balancing_area_demand_hourly_<year>.csv`: hourly demand (MW) per balancing area for each of `fetch.demand_years`, sorted by balancing area then hour, derived by the end of `flux-match fetch`. Rebuilt only when missing or older than one of that year's provincial demand files.
- `grid_cell_rankings.csv`: derived by the end of `flux-match fetch` from the files in `coders/`; delete it, or refresh its inputs, to rebuild it.
