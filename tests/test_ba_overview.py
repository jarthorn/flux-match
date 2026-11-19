import tempfile
import unittest
from pathlib import Path

from flux_match.errors import DataError
from flux_match.viz.ba_overview import (
    ANNUAL_DIV_ID,
    CSS_HREF,
    DEFAULT_BA,
    MAP_DIV_ID,
    NORMALIZED_DIV_ID,
    OTHER_COLOR,
    SELECTED_COLOR,
    load_annual_demand,
    load_normalized_demand,
    membership_z,
    mwh_to_gwh,
    render_page,
    require_same_areas,
)
from flux_match.viz.cf_animation import cell_polygons


class TempDirTest(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = Path(tmp.name)

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text)
        return path


class MwhToGwhTest(unittest.TestCase):
    def test_rounds_half_up(self):
        # 2.5 and 1.5 round away from zero, not to the even integer.
        self.assertEqual(mwh_to_gwh(2500), 3)
        self.assertEqual(mwh_to_gwh(1500), 2)
        self.assertEqual(mwh_to_gwh(1499), 1)
        self.assertEqual(mwh_to_gwh(130358952.4136759), 130359)
        self.assertEqual(mwh_to_gwh(0), 0)


class MembershipTest(unittest.TestCase):
    def test_selected_cells_are_one(self):
        areas = ["Ontario.a", "Alberta.a", "Ontario.a", "Quebec.a"]
        self.assertEqual(membership_z(areas, "Ontario.a"), [1, 0, 1, 0])

    def test_unknown_area_is_all_zero(self):
        areas = ["Ontario.a", "Alberta.a"]
        self.assertEqual(membership_z(areas, "Quebec.b"), [0, 0])


class RequireAreasTest(unittest.TestCase):
    def test_returns_sorted_names(self):
        areas = {"Ontario.a", "Alberta.a"}
        self.assertEqual(
            require_same_areas(areas, set(areas), set(areas)),
            ["Alberta.a", "Ontario.a"],
        )

    def test_missing_default_fails(self):
        areas = {"Alberta.a"}
        with self.assertRaisesRegex(DataError, "Ontario.a is not in the inputs"):
            require_same_areas(areas, set(areas), set(areas))

    def test_area_in_only_one_file_fails(self):
        grid = {"Ontario.a", "Alberta.a"}
        annual = {"Ontario.a"}
        normalized = {"Ontario.a", "Alberta.a"}
        with self.assertRaisesRegex(DataError, "balancing areas differ"):
            require_same_areas(grid, annual, normalized)


class LoadAnnualDemandTest(TempDirTest):
    def test_rounds_mwh_to_gwh(self):
        path = self.write(
            "balancing_area_demand.csv",
            "balancing_area,2018,2019\nOntario.a,2500,1500\nAlberta.a,1000,0\n",
        )
        years, demand = load_annual_demand(path)
        self.assertEqual(years, [2018, 2019])
        self.assertEqual(demand["Ontario.a"], [3, 2])
        self.assertEqual(demand["Alberta.a"], [1, 0])

    def test_non_numeric_fails(self):
        path = self.write(
            "balancing_area_demand.csv",
            "balancing_area,2018\nOntario.a,nope\n",
        )
        with self.assertRaisesRegex(DataError, "not a number"):
            load_annual_demand(path)


class LoadNormalizedDemandTest(TempDirTest):
    def test_hour_gap_fails(self):
        path = self.write(
            "normalized_demand_hourly.csv",
            "balancing_area,hour,normalized_demand\nOntario.a,1,1.0\nOntario.a,3,1.1\n",
        )
        with self.assertRaisesRegex(DataError, "expected hour 2, got 3"):
            load_normalized_demand(path)


class RenderPageTest(unittest.TestCase):
    def test_links_css_plotly_and_default_area(self):
        cells = ["1", "2"]
        areas = ["Alberta.a", DEFAULT_BA]
        geojson = cell_polygons(cells, [-100.0, -99.375], [50.0, 50.0])
        balancing_areas = ["Alberta.a", DEFAULT_BA]
        page = render_page(
            cells=cells,
            areas=areas,
            geojson=geojson,
            years=[2018, 2019],
            annual={"Alberta.a": [100, 110], DEFAULT_BA: [200, 210]},
            normalized={"Alberta.a": [1.0, 1.1, 0.9], DEFAULT_BA: [0.8, 1.2, 1.0]},
            balancing_areas=balancing_areas,
        )
        self.assertIn(f'href="{CSS_HREF}"', page)
        self.assertEqual(page.count("cdn.plot.ly"), 1)
        self.assertIn(f'<option value="{DEFAULT_BA}" selected>', page)
        self.assertNotIn('<option value="Alberta.a" selected>', page)
        self.assertLess(page.index('value="Alberta.a"'), page.index(f'value="{DEFAULT_BA}"'))
        self.assertIn(f'id="{MAP_DIV_ID}"', page)
        self.assertIn(f'id="{ANNUAL_DIV_ID}"', page)
        self.assertIn(f'id="{NORMALIZED_DIV_ID}"', page)
        self.assertIn("Jan 1 00:00-01:00 UTC", page)
        self.assertNotIn("Hour %{customdata}", page)
        self.assertIn(SELECTED_COLOR, page)
        self.assertIn(OTHER_COLOR, page)


class RenderPageRequiresDefaultTest(unittest.TestCase):
    def test_missing_default_fails(self):
        with self.assertRaisesRegex(DataError, "Ontario.a is not in the inputs"):
            render_page(
                cells=["1"],
                areas=["Alberta.a"],
                geojson={"type": "FeatureCollection", "features": []},
                years=[2018],
                annual={"Alberta.a": [1]},
                normalized={"Alberta.a": [1.0]},
                balancing_areas=["Alberta.a"],
            )
