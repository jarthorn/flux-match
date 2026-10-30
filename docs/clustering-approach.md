# Overview

This file describes an approach to selecting wind turbine sites based on complementarity. I intend to refine this approach depending on experimental results obtained from running this site selection algorithm through flux-match. This particular approach focuses on K-means clustering to ensure geographic separation of sites.

# Approach

1. Segment the site database by balancing area. The file data/coders/grid_cell_info.csv contains the balancing area of each site. The rest of this algorithm will be run independently for each balancing area. We may consider complementarity of resources across balancing areas in a future iteration. For now, the theory is that Canadian provinces are large enough that we should be able to find measurable complementarity across sites within a single balancing area.

2. Site clustering. We assume that sites geographically near each other will exhibit high complementarity. Our first step is to break all possible sites in a balancing area into a set of geographic clusters. We will use K-means clustering to break the entire site collection into 10 clusters. We use 10 as an initial experimental value, but this should be parameterized in the configuration so we can change it later. Since this step may be compute-intensive, the algorithm should write an intermediate file that assigns each site in each balancing area to a cluster. Suggested format: a CSV file with columns grid_id,balancing_area,cluster_id,annual_wind_capacity_factor where cluster_id is from 1-10, and annual_wind_capacity_factor is the sum of annual_wind_capacity_factor values for each cell in that cluster, taken from the file grid_cell_rankings.csv.

3. Perform the following until all capacity is satisfied, separately for each resource type (wind, solar):
- Initialize candidate sites list to contain all grid cells in the balancing area
- From the candidate sites list, select the grid cell with the highest value for annual_{wind|solar}_capacity_factor. Assign maximum capacity allowed per cell to this site. Remove all remaining sites from this cluster from the candidate sites list.
- Repeat until all required capacity is allocated.

Implementation note: Break out the K-means clustering implementation into a separate class that operates on an input array of grid cells (id, lat, long), and a target cluster count K. It then returns K arrays of sites representing the clusters. Note it is possible that some clusters have more sites than others, so the output is a set of arrays rather than a two-dimensional array.