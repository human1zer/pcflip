CATEGORIES = [
    "Laptop", "Desktop PC", "GPU", "CPU", "Motherboard", "RAM",
    "SSD/Storage", "PSU", "Case", "Monitor", "Peripheral", "Other",
]

CONDITIONS = ["New", "Like New", "Used - Good", "Used - Fair", "For Parts"]

STATUSES = ["in_stock", "listed", "sold", "consumed"]
STATUS_LABELS = {
    "in_stock": "In stock", "listed": "Listed on FINN", "sold": "Sold", "consumed": "Consumed",
}

# "consumed" is deliberately excluded here -- it's only ever set/cleared via
# the consume/undo routes (which keep it in sync with a costs row on the
# target item), never through the plain edit form's status dropdown.
EDITABLE_STATUSES = ["in_stock", "listed", "sold"]

PAYMENT_METHODS = [
    "Vipps", "Cash", "Bank transfer", "FINN Trygg handel", "Other",
]

DELIVERY_METHODS = ["Face to face", "Shipped"]

SHIPPING_COMPANIES = ["Posten/Bring", "PostNord", "Helthjem", "Porterbuddy", "Other"]
