import re


def visible_and_hidden(html):
    """(visible titles, hidden titles) of the rows on the items list."""
    visible, hidden = set(), set()
    for attrs, body in re.findall(r'<div class="bg-card[^"]*"\s+(data-search=.*?)>(.*?)</a>', html, re.S):
        title = re.search(r'data-href="/items/(\d+)"', attrs).group(1)
        (hidden if re.search(r'\bhidden\s*$', attrs.strip()) else visible).add(int(title))
    return visible, hidden


def chips(html):
    return dict(re.findall(r'data-chip="([^"]*)"[^>]*>\s*(?:[^<]*?)\s*<span data-chip-count[^>]*>(\d+)</span>', html))


def test_chips_built_from_existing_categories(client, add_item):
    add_item("pc a", "Desktop PC"); add_item("pc b", "Desktop PC")
    add_item("ram", "RAM"); add_item("mystery", None)
    add_item("old ssd", "SSD/Storage", status="sold")
    html = client.get("/").text
    assert chips(html) == {"": "5", "Desktop PC": "2", "RAM": "1", "SSD/Storage": "1", "Uncategorized": "1"}
    # Only this status tab's categories; the search narrows the counts.
    assert chips(client.get("/?status=in_stock&q=pc").text) == {"": "2", "Desktop PC": "2", "RAM": "0", "Uncategorized": "0"}
    assert "GPU" not in chips(html)


def test_category_filter_hides_rows_and_combines(client, add_item):
    pc = add_item("Dell pc", "Desktop PC")
    sold_pc = add_item("HP pc", "Desktop PC", status="sold")
    ram = add_item("Dell ram", "RAM")
    none = add_item("mystery", None)

    visible, hidden = visible_and_hidden(client.get("/?category=Desktop+PC").text)
    assert visible == {pc, sold_pc} and hidden == {ram, none}

    visible, hidden = visible_and_hidden(client.get("/?category=Desktop+PC&status=in_stock&q=dell").text)
    assert visible == {pc} and hidden == {ram, none}

    visible, _ = visible_and_hidden(client.get("/?category=Uncategorized").text)
    assert visible == {none}


def test_category_kept_in_status_tabs_and_sort(client, add_item):
    add_item("pc", "Desktop PC")
    html = client.get("/?category=Desktop+PC&q=pc").text
    assert 'href="/?q=pc&amp;category=Desktop+PC&amp;status=sold"' in html
    assert 'id="sort-category" name="category" value="Desktop PC"' in html
    assert "1 item matching \"pc\" in Desktop PC" in html
