"""Every navigable section has an icon.

A section with no entry in the rail's icon map rendered an EMPTY 40px
button. `{@html undefined}` writes nothing, the anchor still worked, the
tooltip still named it — so the page was reachable by keyboard shortcut and
invisible in the rail. Four shipped that way: the three Virtual Trading
surfaces and the On-Chain desk.

Nothing could have caught it. Svelte does not warn, TypeScript cannot type
an `{@html}` expression's emptiness, and a screenshot only shows the gap if
you already know how many icons there should be. So it is checked here, in
the one place that reads BOTH files and can compare them.
"""
import pathlib
import re
import unittest

NAV = pathlib.Path("frontend/src/lib/components/NavRail.svelte")
SECTIONS = pathlib.Path("frontend/src/lib/stores/section.svelte.ts")


def declared_sections() -> list[str]:
    src = SECTIONS.read_text(encoding="utf-8")
    return re.findall(r'\{\s*id:\s*"(\w+)"', src)


def icon_keys() -> set[str]:
    """Keys of the `icons` record, read from its literal alone.

    Scoped to the object rather than the whole file on purpose: the style
    block is full of `property:` lines, and matching those would make this
    pass for a section whose only "icon" is a CSS rule that shares its name.
    """
    src = NAV.read_text(encoding="utf-8")
    start = src.index("const icons")
    end = src.index("\n  };", start)
    return set(re.findall(r"^\s{4}(\w+):", src[start:end], re.M))


class EverySectionIsVisible(unittest.TestCase):
    def test_every_declared_section_has_an_icon(self):
        missing = [s for s in declared_sections() if s not in icon_keys()]
        self.assertEqual(
            missing, [],
            f"these sections would render an empty button in the nav rail: "
            f"{missing}")

    def test_there_is_a_fallback_so_the_next_one_is_visible(self):
        """Belt and braces: if a section is added without an icon, it
        should look WRONG rather than look absent."""
        src = NAV.read_text(encoding="utf-8")
        self.assertIn("FALLBACK_ICON", src)
        self.assertIn("icons[section.id] ?? FALLBACK_ICON", src)

    def test_sections_are_declared_before_they_are_grouped(self):
        """A group naming an id that no section declares renders nothing —
        the same invisible failure from the other direction."""
        nav = NAV.read_text(encoding="utf-8")
        grouped = set(re.findall(r'ids:\s*\[([^\]]*)\]', nav))
        named = {m for g in grouped for m in re.findall(r'"(\w+)"', g)}
        unknown = sorted(named - set(declared_sections()))
        self.assertEqual(unknown, [],
                         f"nav groups reference unknown sections: {unknown}")


if __name__ == "__main__":
    unittest.main()
