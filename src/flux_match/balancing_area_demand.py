"""Total annual demand per balancing area and demand year.

Builds `balancing_area_demand.csv`: one row per balancing area, one column per demand year,
each cell the year's total demand in MWh. CODERS publishes demand per province, so each
province's annual total is split among its balancing areas in proportion to the populations in
PROVINCE_BA_POPULATION. A balancing area that spans provinces (Ontario.a also serves part of
Quebec) receives a share from each.

Every `provincial_demand_<province>_<year>.csv` in the CODERS data folder is used, whatever the
scenario's `demand_years`, so the output stays valid across runs. Every year found must have a
file for every province in PROVINCE_BA_POPULATION, otherwise the totals would be too low. Any
missing file or malformed value is a hard failure and no output file is written. Negative hourly
demand is valid, since imports are not modeled and can show up as negative demand upstream. It
is summed like any other value.
"""

import math
import re
from collections.abc import Mapping, Sequence
from pathlib import Path

from flux_match.errors import DataError
from flux_match.io import read_csv, write_csv_atomic
from flux_match.schema import HOURS_PER_YEAR

BA_COLUMN = "balancing_area"

DEMAND_FILE_PATTERN = re.compile(r"provincial_demand_([A-Z]{2})_(\d{4})\.csv")
DEMAND_FILE_COLUMNS = ("province", "annual_hour_ending", "demand_MWh")

# Province codes as used by the CODERS provincial_demand table.
PROVINCE_NAMES: dict[str, str] = {
    "AB": "Alberta",
    "BC": "British Columbia",
    "MB": "Manitoba",
    "NB": "New Brunswick",
    "NL": "Newfoundland and Labrador",
    "NS": "Nova Scotia",
    "ON": "Ontario",
    "PE": "Prince Edward Island",
    "QC": "Quebec",
    "SK": "Saskatchewan",
}

PROVINCE_BA_POPULATION: dict[str, dict[str, int]] = {
    "Alberta": {"Alberta.a": 4_269_532},
    "British Columbia": {"British Columbia.a": 4_993_982},
    "Manitoba": {"Manitoba.a": 1_341_217},
    "New Brunswick": {"New Brunswick.a": 785_275},
    "Newfoundland and Labrador": {
        "Newfoundland and Labrador.a": 485_613,
        "Newfoundland and Labrador.b": 18_294,
    },
    "Nova Scotia": {"Nova Scotia.a": 973_541},
    "Ontario": {
        "Ontario.a": 13_435_314,
        "Ontario.b": 765_558,
    },
    "Prince Edward Island": {"Prince Edward Island.a": 129_267},
    "Quebec": {
        "Ontario.a": 15_187,
        "Quebec.a": 7_867_232,
        "Quebec.b": 653_080,
    },
    "Saskatchewan": {"Saskatchewan.a": 1_134_115},
}

# year -> province code -> demand file
DemandFiles = dict[int, dict[str, Path]]


def find_demand_files(data_dir: Path) -> DemandFiles:
    """Every cached provincial demand file, grouped by year. Each year must be complete."""
    files: DemandFiles = {}
    unknown = []
    for path in sorted(data_dir.glob("provincial_demand_*.csv")):
        match = DEMAND_FILE_PATTERN.fullmatch(path.name)
        if not match:
            raise DataError(f"Unrecognized provincial demand file name: {path.name}")
        code, year = match.group(1), int(match.group(2))
        if code not in PROVINCE_NAMES:
            unknown.append(path.name)
        files.setdefault(year, {})[code] = path
    if unknown:
        raise DataError(f"Provincial demand files for unknown provinces: {unknown}")
    if not files:
        raise DataError(f"No provincial demand files found in {data_dir}")

    missing = [
        f"provincial_demand_{code}_{year}.csv"
        for year, by_province in sorted(files.items())
        for code in PROVINCE_NAMES
        if code not in by_province
    ]
    if missing:
        raise DataError(f"Incomplete provincial demand years; missing files: {missing}")
    return files


def read_annual_demand(path: Path, province_code: str) -> float:
    """Total demand in MWh over the 8760 hours of one provincial demand file."""
    name = path.name
    rows = read_csv(path, DEMAND_FILE_COLUMNS)
    if len(rows) != HOURS_PER_YEAR:
        raise DataError(f"{name}: expected {HOURS_PER_YEAR} hourly rows, got {len(rows)}")
    values = []
    for hour, row in enumerate(rows, start=1):
        if row["province"] != province_code:
            raise DataError(
                f"{name}: hour {hour}: province is {row['province']!r}, expected {province_code!r}"
            )
        if row["annual_hour_ending"] != str(hour):
            raise DataError(
                f"{name}: expected hour {hour} on row {hour}, got {row['annual_hour_ending']!r}"
            )
        text = row["demand_MWh"]
        try:
            value = float(text)
        except ValueError:
            raise DataError(f"{name}: hour {hour}: demand_MWh is not a number: {text!r}") from None
        if not math.isfinite(value):
            raise DataError(f"{name}: hour {hour}: invalid demand_MWh {text!r}")
        values.append(value)
    return math.fsum(values)


def split_by_population(total: float, populations: Mapping[str, int]) -> dict[str, float]:
    """Divide `total` among balancing areas in proportion to their population."""
    population = sum(populations.values())
    return {ba: total * share / population for ba, share in populations.items()}


def build_ba_demand(
    provincial_totals: Mapping[int, Mapping[str, float]],
) -> tuple[list[int], list[dict[str, object]]]:
    """Years (ascending) and one row per balancing area, from year -> province code -> MWh."""
    years = sorted(provincial_totals)
    parts: dict[str, dict[int, list[float]]] = {
        ba: {year: [] for year in years}
        for populations in PROVINCE_BA_POPULATION.values()
        for ba in populations
    }
    for year in years:
        for code, name in PROVINCE_NAMES.items():
            shares = split_by_population(
                provincial_totals[year][code], PROVINCE_BA_POPULATION[name]
            )
            for ba, mwh in shares.items():
                parts[ba][year].append(mwh)
    rows = [
        {BA_COLUMN: ba, **{str(year): math.fsum(by_year[year]) for year in years}}
        for ba, by_year in sorted(parts.items())
    ]
    return years, rows


def write_balancing_area_demand(files: DemandFiles, dest: Path) -> None:
    """Build `dest` from the files found by find_demand_files. Leaves no partial file on failure."""
    totals = {
        year: {code: read_annual_demand(path, code) for code, path in by_province.items()}
        for year, by_province in files.items()
    }
    years, rows = build_ba_demand(totals)
    write_csv_atomic(dest, (BA_COLUMN, *map(str, years)), rows)


def read_ba_demand(path: Path) -> tuple[int, dict[str, float]]:
    """Newest year in the file, and that year's demand in MWh per balancing area."""
    rows = read_csv(path, (BA_COLUMN,))
    if not rows:
        raise DataError(f"{path.name}: no balancing areas")
    years = _year_columns(path.name, rows[0])
    year = max(years)
    column = str(year)
    demand: dict[str, float] = {}
    for line, row in enumerate(rows, start=2):
        ba = row[BA_COLUMN].strip()
        if not ba:
            raise DataError(f"{path.name}:{line}: blank balancing_area")
        if ba in demand:
            raise DataError(f"{path.name}:{line}: duplicate balancing area {ba}")
        text = row[column]
        try:
            value = float(text)
        except TypeError, ValueError:
            raise DataError(
                f"{path.name}:{line}: {year} demand is not a number: {text!r}"
            ) from None
        if not math.isfinite(value):
            raise DataError(f"{path.name}:{line}: invalid {year} demand {text!r}")
        demand[ba] = value
    return year, demand


def _year_columns(name: str, row: Mapping[str, str]) -> list[int]:
    years = []
    for column in row:
        if column == BA_COLUMN:
            continue
        try:
            years.append(int(column))
        except ValueError:
            raise DataError(f"{name}: demand column {column!r} is not a year") from None
    if not years:
        raise DataError(f"{name}: no demand year columns")
    return years


def demand_file_paths(files: DemandFiles) -> Sequence[Path]:
    return [path for by_province in files.values() for path in by_province.values()]
