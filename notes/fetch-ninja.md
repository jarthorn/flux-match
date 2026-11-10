# Overview

I want to add a new source for wind capacity factor data: renewables.ninja. Read the [API documentation](https://www.renewables.ninja/documentation/api) for details on how the API endpoint works. From this API, we need to reconstruct an equivalent wind capacity factor table that we obtain today from CODERS. We need to fetch this data separately for each individual cell. We will do this in two separate steps, both performed within the flux-match "fetch" step:
1. Download raw data files for each cell defined in grid_cell_info.csv from NINJA. This will be a CSV of power output ratings per hour of the year. Download and store each file lazily (only download files not already present in the data/ninja/ folder). This endpoint requires a number of parameters. Use 100000 for the capacity, merra2 as the dataset, but provide the turbine height and model as parameters in defaults.toml. Default height: 105. Default model: "Vestas V150 4500".
2. Collate raw data files into a single capacity factor CSV file. Note that we will need to calculate the capacity factor by dividing the power output in step 1 by the total capacity provided in step 1 (100000). The resulting CSV will have 8760 data rows and 2984 data columns (plus a single row and column header).


Notes:
- Implementation should live in src/steps/fetch/fetch_ninja.py
- We likely need to write the final output file in a streaming fashion one column at a time because it is very large, or some other chunking method.
- Note that the API has a rate limit of 1 request per second, and 50 requests per hour. Let's add a sleep between requests to be polite (configurable, starting with 5 seconds). Each run should track the start time, and then stop proactively with a clear console message if it reaches 50 requests within a rolling 1-hour window, to avoid hitting the rate limit. After some initial testing we'll ramp up the sleep time between requests so that the script can safely run for many hours.
- Although renewables.ninja also provides solar capacity factors, we are only interested in their wiind data for now.
- We can probably set interpolate=false when calling the API, since they use the same MERRA2 grid cells that this project is using.
- I have added NINJA_API_TOKEN to the environment.
- The final output file should be located at data/ninja/wind_capacity_factor_{year}.csv
- All time should be in UTC (use `local_time=false`). Drop February 29th for leap years so that the final file always has 8760 rows of data.
- Step 2 should only run if all the data from step 1 is present. It should also be skipped if the output file is already present and newer than any raw fetch files.