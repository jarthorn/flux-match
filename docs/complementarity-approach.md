# Overview

This file describes an approach to selecting wind turbine sites based on complementarity. I intend to refine this approach depending on experimental results obtained from running this site selection algorithm through flux-match. The same algorithm should be repeated independently for each resource type (wind,solar). This particular approach makes use of the Total Complementarity Index to select sites with maximally complementary generation profiles.

# Approach

1. Segment the site database by balancing area. The file data/coders/grid_cell_info.csv contains the balancing area of each site. The rest of this algorithm will be run independently for each balancing area.

2. Determine the total number of sites required, N. This should be ceiling(capacity required for this balancing area /max_capacity_per_cell). Filter the sites in this balancing area to select the top 2N sites by `ba_annual_{wind|solar}_rank`. This list becomes the initial pool of candidate sites.

3. Select sites:
- From the candidate sites list, select the grid cell with the highest value for annual_{wind|solar}_capacity_factor.
- Remove it from the pool of candidate sites.
- Add it to the list of confirmed sites, assigning the maximum allowed capacity to this cell. 
- Initialize a variable `selected_sites_cf` to contain the 8760 hourly capacity factors for this grid cell.
- Repeat until we have enough sites to satisfy total capacity:
   - Calculate the Total Complementarity Index between `selected_sites_cf` and the capacity factor timeseries for each remaining candidate site.
   - Select the site with the highest complementarity index. Remove it from the pool of candidate sites, and add it to the list of confirmed sites, assigning maximum allowed remaining capacity to this site.
   - `current_generation_timeseries` becomes the average capacity factor for each hour across all confirmed sites. This can be calculated by using current_generation_timeseries as the running average, and then produce the new average for site number x  in hour h with capacity factor cf(x,h) as avg(h) = ((avg(h)*(x-1) + cf(x,h)) / x.

   Implementation note: We should implement a generic and reusable function that is given a target timeseries and an array of candidate time series. It should return an array of values for Total Complementarity Index between the target and each candidate. This is a building block we may use in a variety of site selection approaches so it is worth making generic in a separate class/script.