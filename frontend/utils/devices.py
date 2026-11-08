"""Device-backed record categories; the API enforces the same allowed IDs."""

DEVICE_CATEGORIES = {
    "device-001": "設備 A",
    "device-002": "設備 B",
    "device-003": "設備 C",
}


def category_label(value: str) -> str:
    return DEVICE_CATEGORIES.get(value, f"舊分類：{value}")
