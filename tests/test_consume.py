import re

import pytest


def consume(client, part_id, pc_id):
    return client.post(f"/items/{part_id}/consume", data={
        "csrf_token": client.csrf, "target_item_id": pc_id, "next": f"/items/{pc_id}#parts",
    })


def addable_parts(client, pc_id):
    """Part ids offered by the "+ Add" list on the PC's page."""
    html = client.get(f"/items/{pc_id}").text
    return {int(i) for i in re.findall(r'action="/items/(\d+)/consume" data-search', html)}


@pytest.mark.parametrize("pc_status", ["in_stock", "listed"])
def test_pc_takes_many_parts(client, db, add_item, pc_status):
    pc = add_item("Dell OptiPlex", "Desktop PC", 1000, pc_status)
    parts = [add_item("16GB DDR4", "RAM", 250), add_item("1TB NVMe", "SSD/Storage", 400.5),
             add_item("GTX 1650", "GPU", 900)]

    for n, part in enumerate(parts, 1):
        # The PC page must still offer every part not yet installed.
        assert addable_parts(client, pc) == set(parts[n - 1:])
        r = consume(client, part, pc)
        assert r.status_code == 303 and r.headers["location"] == f"/items/{pc}#parts"
        assert db.execute("SELECT status FROM items WHERE id = ?", (part,)).fetchone()[0] == "consumed"
        # The PC itself is untouched: same status, one more cost row.
        assert db.execute("SELECT status FROM items WHERE id = ?", (pc,)).fetchone()[0] == pc_status
        assert db.execute("SELECT count(*) FROM costs WHERE item_id = ?", (pc,)).fetchone()[0] == n

    rows = db.execute("SELECT source_item_id, amount FROM costs WHERE item_id = ? ORDER BY id", (pc,)).fetchall()
    assert [r[0] for r in rows] == parts
    assert sum(r[1] for r in rows) == pytest.approx(250 + 400.5 + 900)
    assert addable_parts(client, pc) == set()
    assert "Parts (3)" in client.get(f"/items/{pc}").text


def test_undo_one_part_keeps_the_others(client, db, add_item):
    pc = add_item("ThinkPad", "Laptop", 1500)
    a, b = add_item("8GB", "RAM", 100), add_item("256GB", "SSD/Storage", 200)
    consume(client, a, pc)
    consume(client, b, pc)
    client.post(f"/items/{a}/consume/undo", data={"csrf_token": client.csrf})
    assert db.execute("SELECT status FROM items WHERE id = ?", (a,)).fetchone()[0] == "in_stock"
    assert [r[0] for r in db.execute("SELECT source_item_id FROM costs WHERE item_id = ?", (pc,))] == [b]
    assert addable_parts(client, pc) == {a}


def test_consume_refuses_sold_pc_and_pc_as_part(client, db, add_item):
    sold_pc = add_item("Sold PC", "Desktop PC", 1000, "sold")
    pc, other_pc = add_item("PC", "Desktop PC"), add_item("Laptop", "Laptop")
    part = add_item("RAM", "RAM", 100)
    consume(client, part, sold_pc)
    consume(client, other_pc, pc)
    assert db.execute("SELECT count(*) FROM costs").fetchone()[0] == 0


def test_consume_requires_csrf(client, db, add_item):
    pc, part = add_item("PC", "Desktop PC"), add_item("RAM", "RAM", 100)
    r = client.post(f"/items/{part}/consume", data={"csrf_token": "wrong", "target_item_id": pc})
    assert r.headers["location"] == "/login"
    assert db.execute("SELECT count(*) FROM costs").fetchone()[0] == 0
