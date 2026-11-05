# Overview

Create a new standalone python script (and bash helper) called batch_summary. Place it in the @src/flux_match/viz/ folder because the other scripts in that folder are also for post-run analysis and visualization. The purpose of this script is to collate a summary of data across all runs of a batch, suitable as input for later offline analysis.

Add user documentation for this feature in the "Batch Mode" section of README.md.

# Inputs

This script should take a mandatory parameter `--batch <name>`. This represents a batch name that was produced by a previous run of flux-match in batch mode. It should look for a corresponding folder of the form OUTPUT_DIR/<name>. If no such folder is present it should stop with an error message. The inputs for batch_summary will be all of the sub-folders of this batch output folder.

# Outputs

This script should produce two outputs:
- summary.csv - A CSV file with a header row, and one data row per run in the batch. This should include all `config.toml` values that do not have identical values across all runs of the batch, as well as some data from run.json (described below).
- invariants.toml - A TOML file containing the configuration values that are invariant across all runs of the batch

Every value from `config.toml` across the batch should be present in one of these two files, but never both.

## Data from run.json

summary.csv should contain some data from run.json
- batch_index
- started
- dispatch_stats.net_demand_pct.total (Header: "net_demand_pct")
- dispatch_stats.curtailment_pct.total (Header: "curtailment_pct"). Replace "null" values with 0.0
- dispatch_stats.satisfied_demand_hours_pct.total (Header: "satisfied_demand_hours_pct")
- peak_net_demand_mw - The maximum value from the dispatch_stats.peak_net_demand_mw across all balancing areas
- peak_net_demand_ba - The name of the balancing area with the maximum value for peak_net_demand_mw

# Clarifications

- Output location: write summary.csv and invariants.toml directly into OUTPUT_DIR/<name>/ (no timestamp), overwriting any previous summary. Re-running after adding runs to the batch refreshes them.
- Incomplete or malformed runs (e.g. missing run.json, dispatch not in completed_steps, missing dispatch_stats): hard fail, naming the folder and the problem.
- Config keys present in some runs but not others (e.g. runs with different selection strategies): treat as varying. They go in summary.csv, with an empty cell for runs where the key is absent.
- List values in summary.csv (e.g. a varying dispatch.dispatch_years): write as a TOML literal in a single cell, e.g. "[2023, 2024]", matching the --config syntax.
- Ties for the maximum peak_net_demand_mw: use the first balancing area in run.json order.
- An integer and a float of the same value (e.g. 4 and 4.0) are identical, so a key whose values differ only in this way is invariant. Booleans are never identical to numbers.