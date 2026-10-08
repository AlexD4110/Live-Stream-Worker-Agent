"""gifting/analysis.py (used by the voice agent) must agree with dashboard/metrics.js (used by the page):
same numbers, same findings, same sentences. The two are checked against each other, not against a copy."""
import json
import math
import os
import shutil
import subprocess
import tempfile
import unittest

from gifting import analysis, generate

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FILTERS = [{}, {"region": "US-East"}, {"platform": "android"}, {"region": "US-East", "platform": "android"}]


def run_node(data_js, flt):
    args = ["node", os.path.join(ROOT, "tests", "node_runner.js"), data_js]
    if flt:
        args.append(json.dumps(flt))
    return json.loads(subprocess.run(args, capture_output=True, text=True, check=True, cwd=ROOT).stdout)


def same(a, b, path="root"):
    """Deep equality where floats match to 9 places. Returns a description of the first difference."""
    if isinstance(a, dict) and isinstance(b, dict):
        if set(a) != set(b):
            return f"{path}: keys differ {sorted(set(a) ^ set(b))}"
        for k in a:
            d = same(a[k], b[k], f"{path}.{k}")
            if d:
                return d
        return None
    if isinstance(a, list) and isinstance(b, list):
        if len(a) != len(b):
            return f"{path}: lengths {len(a)} != {len(b)}"
        for i, (x, y) in enumerate(zip(a, b)):
            d = same(x, y, f"{path}[{i}]")
            if d:
                return d
        return None
    if isinstance(a, (int, float)) and isinstance(b, (int, float)) and not isinstance(a, bool):
        return None if math.isclose(a, b, rel_tol=1e-9, abs_tol=1e-9) else f"{path}: {a} != {b}"
    return None if a == b else f"{path}: {a!r} != {b!r}"


class FormatTest(unittest.TestCase):
    """Number formatting must follow JavaScript's rules, including rounding .5 up."""

    def test_pct(self):
        self.assertEqual(analysis.pct(0.1256), "13%")
        self.assertEqual(analysis.pct(0.125, 0), "13%")      # JS rounds an exact half up; Python's round() would not
        self.assertEqual(analysis.pct(0.0046, 1), "0.5%")
        self.assertEqual(analysis.pct(0.0), "0%")

    def test_signed(self):
        self.assertEqual(analysis.signed(0.226), "+23%")
        self.assertEqual(analysis.signed(-0.135, 1), "-13.5%")
        self.assertEqual(analysis.signed(0.0), "+0%")

    def test_num_and_usd(self):
        self.assertEqual(analysis.num(1234567.5), "1,234,568")
        self.assertEqual(analysis.usd(650), "$650")


@unittest.skipUnless(shutil.which("node"), "node is only needed to run these checks")
class AnalysisParityTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.t, cls.truth = generate.build(seed=7)
        cls.dir = tempfile.mkdtemp()
        cls.data_js = os.path.join(cls.dir, "data.js")
        generate.write_dashboard_data(cls.t, cls.data_js)
        cls.D = analysis.load(cls.data_js)
        cls.node = {json.dumps(f): run_node(cls.data_js, f) for f in FILTERS}

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.dir)

    def test_summary_matches_javascript_for_every_filter(self):
        for f in FILTERS:
            mine = analysis.summary(self.D, f)
            theirs = self.node[json.dumps(f)]["summary"]
            theirs.pop("filter", None)
            mine = {k: v for k, v in mine.items() if k != "filter"}
            self.assertIsNone(same(mine, theirs), f"filter {f}")

    def test_findings_match_javascript_word_for_word(self):
        mine = analysis.findings(self.D)
        theirs = self.node["{}"]["findings"]
        self.assertEqual([f["id"] for f in mine], [f["id"] for f in theirs])
        self.assertIsNone(same(mine, theirs))

    def test_findings_find_the_planted_ring_exactly(self):
        ring = next(f for f in analysis.findings(self.D) if f["id"] == "gift_ring")
        self.assertEqual(set(ring["accounts"]), set(self.truth["ring_gifters"]))

    def test_segments_and_creators_match(self):
        seg = analysis.segments(self.D)
        self.assertEqual(seg[0]["platform"], "android")
        self.assertEqual(seg[0]["region"], "US-East")
        cs = analysis.creator_stats(self.D)
        self.assertEqual([t["tier"] for t in cs["tiers"]], ["head", "mid", "emerging"])
        self.assertEqual(sum(t["creators"] for t in cs["tiers"]), 300)

    def test_load_reads_the_shipped_data_file(self):
        D = analysis.load(os.path.join(ROOT, "dashboard", "data.js"))
        self.assertEqual(D["days"], generate.DAYS)
        self.assertGreater(len(D["gifts"]["day"]), 50000)


if __name__ == "__main__":
    unittest.main()
