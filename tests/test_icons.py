import re
from pathlib import Path

STATIC = Path(__file__).resolve().parent.parent / "tasks" / "static"
TAB_ICON = re.compile(r'<link rel="icon" href="([^"]+)"')


def test_every_page_uses_the_small_icon_in_the_browser_tab():
    pages = sorted(STATIC.glob("*.html"))
    assert pages
    for page in pages:
        found = TAB_ICON.search(page.read_text())
        assert found, page.name
        assert found.group(1).endswith("/static/icon-small.svg"), page.name


def test_the_small_icon_is_a_separate_file_from_the_full_one():
    small = (STATIC / "icon-small.svg").read_text()
    full = (STATIC / "icon.svg").read_text()
    assert small != full
    assert small.startswith("<svg") and full.startswith("<svg")


def test_both_icons_are_served(owner):
    for name in ("icon.svg", "icon-small.svg"):
        assert owner.get(f"/static/{name}").status_code == 200, name
