# Overview

This file describes an approach to selecting wind turbine sites based on complementarity. I intend to refine this approach depending on experimental results obtained from running this site selection algorithm through flux-match. This particular approach focuses on K-means clustering to ensure geographic separation of sites.

# Approach

1. Segment the site database by balancing area. The file data/coders/grid_cell_info.csv contains the balancing area of each site. The rest of this algorithm will be run independently for each balancing area. We may consider complementarity of resources across balancing areas in a future iteration. For now, the theory is that Canadian provinces are large enough that we should be able to find measurable complementarity across sites within a single balancing area.

2. Site clustering. We assume that sites geographically near each other will exhibit high correlation. Our first step is to break all possible sites in a balancing area into a set of geographic clusters. We will use K-means clustering to break the entire site collection into 10 clusters. We use 10 as an initial experimental value, but this should be parameterized in the configuration so we can change it later. To help with interpretation of results, the algorithm writes `clusters.csv` to the run folder, assigning each site in each balancing area to a cluster. It is recomputed on every run rather than cached: clustering a few hundred cells takes milliseconds. Columns: `grid_cell,balancing_area,cluster_id,latitude,longitude,annual_wind_capacity_factor,annual_solar_capacity_factor`. `cluster_id` runs from 1 to K. The capacity factors are the cell's own values from grid_cell_rankings.csv; clusters are geographic, so wind and solar share them.

   Clustering details:
   - Latitude and longitude are projected before clustering (equirectangular: `x = longitude * cos(mean latitude of the balancing area)`, `y = latitude`). At Canadian latitudes a degree of longitude is only 0.5-0.7 of a degree of latitude, so raw coordinates would stretch clusters east-west.
   - K-means is seeded (`seed`) and restarted `n_init` times, so runs are reproducible.
   - If a balancing area has fewer cells than K, K is reduced to the cell count for that area (each cell is its own cluster) and a warning is printed to stderr.

3. Perform the following separately for each resource type (wind, solar), until all capacity is satisfied:
- Start a round: the candidate sites list holds every grid cell in the balancing area not yet selected.
- From the candidate sites list, select the grid cell with the best `ba_annual_{wind|solar}_rank` (the highest annual capacity factor, ties already broken deterministically). Assign the maximum capacity allowed per cell, or the remaining capacity if less. Remove all remaining sites in this cluster from the candidate sites list.
- When the candidate list is empty and capacity remains, start a new round, which takes the next-best cell from each cluster. This happens when more sites are needed than there are clusters (for example Ontario.a and Quebec.a when the capacity ratio is 4).
- If the target cannot be met even with every cell in the balancing area at the per-cell maximum, the maximum is raised (with a printed warning) to the smallest value that fits, as in the greedy strategy.

Note that when a balancing area needs only one site, the result is the same as the greedy strategy.

Implementation note: Break out the K-means clustering implementation into a separate class that operates on an input array of grid cells (id, lat, long), and a target cluster count K. It then returns K arrays of sites representing the clusters. Note it is possible that some clusters have more sites than others, so the output is a set of arrays rather than a two-dimensional array. We should implement this as a thin wrapper around the [sklearn K-means](https://scikit-learn.org/stable/modules/generated/sklearn.cluster.KMeans.html) implementation.