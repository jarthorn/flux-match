"""Column names and constants shared by the steps and the dashboard."""

# Leap years are stored without February 29 so that years from different sources line up.
HOURS_PER_YEAR = 8760
# After dropping February 29, this hour is 00:00 on March 1 in every year.
FIRST_HOUR_AFTER_LEAP_DAY = 1417

DISPATCH_COLUMNS = (
    "balancing_area",
    "year",
    "hour",
    "variable_generation_mw",
    "battery_output_mw",
    "net_demand_mw",
)

# Hourly demand per balancing area for one year, written by the fetch step.
BA_DEMAND_HOURLY_COLUMNS = (
    "balancing_area",
    "hour",
    "demand_mw",
)

# Hourly wind and solar generation per balancing area, written by the preprocess step.
VRE_HOURLY_COLUMNS = (
    "balancing_area",
    "hour",
    "wind_mw",
    "solar_mw",
    "variable_generation_mw",
)
