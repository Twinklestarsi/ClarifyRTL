"""The frontend ships a copy of the compiler; it must not drift."""
from pathlib import Path
import filecmp
import unittest

ROOT = Path(__file__).resolve().parents[4]
MAIN = ROOT / "dac2026_c1" / "Compiler" / "rtl_compiler"
COPY = ROOT / "test" / "tempo_spec_ir_rtl" / "Compiler" / "rtl_compiler"


class CompilerSyncTest(unittest.TestCase):
    def test_frontend_copy_matches_main(self):
        if not COPY.is_dir():
            self.skipTest("frontend compiler copy absent")
        main = {p.name for p in MAIN.glob("*.py")}
        copy = {p.name for p in COPY.glob("*.py")}
        self.assertEqual(main, copy, "compiler module sets differ")
        for name in sorted(main):
            self.assertTrue(
                filecmp.cmp(MAIN / name, COPY / name, shallow=False),
                f"{name} drifted between the compiler copies")


if __name__ == "__main__":
    unittest.main()
