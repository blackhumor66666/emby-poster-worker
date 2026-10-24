"""Module boundaries: extracted modules must stay importable without app.py.

app.py is the composition root. If a leaf module starts importing it (directly
or through a helper) we get a circular import that only shows up at runtime in
the container, so this test locks the direction of the dependency graph.
"""
import ast
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
LEAF_MODULES = ("badges.py", "rendering.py", "storage.py", "rating_policy.py",
                "rating_review.py", "media_match.py", "douban_source.py")


def imported_names(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    names: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            names.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            names.add(node.module.split(".")[0])
    return names


class ModuleBoundaryTests(unittest.TestCase):
    def test_leaf_modules_never_import_app(self):
        for name in LEAF_MODULES:
            with self.subTest(module=name):
                self.assertNotIn("app", imported_names(ROOT / name))

    def test_rendering_only_depends_on_badges(self):
        local = {n for n in imported_names(ROOT / "rendering.py")
                 if (ROOT / f"{n}.py").exists()}
        self.assertEqual(local, {"badges"})

    def test_app_imports_the_split_modules(self):
        names = imported_names(ROOT / "app.py")
        for module in ("badges", "rendering", "storage"):
            self.assertIn(module, names)


if __name__ == "__main__":
    unittest.main()
