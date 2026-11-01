# Overview

This file describes an approach to selecting wind and solar generation sites based on complementarity. I intend to refine this approach depending on experimental results obtained from running this site selection algorithm through flux-match. The same algorithm should be repeated independently for each resource type (wind,solar). This particular approach makes use of the Total Variation Complementarity Index to select sites with maximally complementary generation profiles. Lines 141-162 of sustainability-14-08514.xml provide formulas for total variation (section 2.1), and total variation complementarity index (section 2.2).

# Approach

1. Segment the site database by balancing area. The file data/coders/grid_cell_info.csv contains the balancing area of each site. The rest of this algorithm will be run independently for each balancing area.

2. Determine the total number of sites required, N. This should be ceiling(capacity required for this balancing area /max_capacity_per_cell). Filter the sites in this balancing area to select the top 2N sites by `ba_annual_{wind|solar}_rank` (where the multiple "2" here should be configurable so we can explore possible values experimentally). This list becomes the initial pool of candidate sites for that balancing area.

3. Select sites:
- From the candidate sites list, select the grid cell with the highest value for annual_{wind|solar}_capacity_factor.
- Remove it from the pool of candidate sites.
- Add it to the list of confirmed sites, assigning the maximum allowed capacity to this cell. 
- Initialize a variable `selected_sites_cf` to contain the 8760 hourly capacity factors for this grid cell.
- Repeat until we have enough sites to satisfy total capacity:
   - Calculate the Total Complementarity Index between `selected_sites_cf` and the capacity factor timeseries for each remaining candidate site.
   - Select the site with the highest complementarity index. Remove it from the pool of candidate sites, and add it to the list of confirmed sites, assigning maximum allowed remaining capacity to this site.
   - `current_generation_timeseries` becomes the average capacity factor for each hour across all confirmed sites. This can be calculated by using current_generation_timeseries as the running average, and then produce the new average for site number x  in hour h with capacity factor cf(x,h) as avg(h) = ((avg(h)*(x-1) + cf(x,h)) / x.

   Implementation notes:
   - We should implement a generic and reusable function that is given a target timeseries and an array of candidate time series. It should return an array of values for Total Complementarity Index between the target and each candidate. This is a building block we may use in a variety of site selection approaches so it is worth making generic in a separate class/script.
   - This algorithm will become equivalent to the greedy algorithm when the candidate sites list is equal to or smaller than the number of sites required to meet required capacity.
   - This algorithm requireds O(n^2) calculations of total complementarity index, where n is the initial candidate sites list. We may need to re-evaluate the approach if this is computationally too expensive, but the working theory is that the value of n will be low enough to make this tractable.
   The approach for `current_generation_timeseries` measures complementarity against an average of all sites already selected. This could also be calculated against a running sum, min, or max capacity of selected sites. We may need to compare these approaches experimentally to assess which one is most effective.