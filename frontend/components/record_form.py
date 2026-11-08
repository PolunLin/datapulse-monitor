"""Exact decimal and timezone-aware inputs shared by record create/edit forms."""

import re
from datetime import UTC, datetime
from decimal import Decimal, InvalidOperation

import streamlit as st

from components.date_time_fields import date_time_fields
from utils.devices import DEVICE_CATEGORIES
from utils.timezone import TAIPEI, as_taipei, can_display_taipei

NUMBER_PATTERN = r"^[+-]?[0-9]{1,12}(?:\.[0-9]{1,6})?$"


def record_payload(title: str, value: str, category: str, timestamp: str) -> dict:
    title, category = title.strip(), category.strip()
    if not title or len(title) > 200:
        raise ValueError("標題必須為 1–200 個字元。")
    if category not in DEVICE_CATEGORIES:
        raise ValueError("分類請選擇設備 A、設備 B 或設備 C。")
    if timestamp in {"未選擇日期", "未選擇時間"}:
        raise ValueError("請完整選擇資料日期與時間。")
    if not re.fullmatch(NUMBER_PATTERN, value.strip()):
        raise ValueError("請輸入數字，最多 12 位整數與 6 位小數，可使用正負號。")
    try:
        number = Decimal(value.strip())
    except InvalidOperation as exc:
        raise ValueError("請輸入有效數值。") from exc
    if not number.is_finite():
        raise ValueError("數值不能為 NaN 或無限大。")
    digits = list(number.as_tuple().digits)
    exponent = number.as_tuple().exponent
    while digits and digits[-1] == 0:
        digits.pop()
        exponent += 1
    if digits and (max(len(digits) + exponent, 0) > 12 or max(-exponent, 0) > 6):
        raise ValueError("數值最多 12 位整數與 6 位小數。")
    payload = {"title": title, "value": str(number) if digits else "0", "category": category}
    if timestamp.strip():
        try:
            instant = datetime.fromisoformat(timestamp.strip())
            if instant.tzinfo is None or instant.utcoffset() is None:
                raise ValueError
            instant = instant.astimezone(UTC)
            if instant.year < 1000:
                raise ValueError
        except (ValueError, OverflowError) as exc:
            raise ValueError("時間須包含時區，例如 2026-11-08T12:00:00+08:00。") from exc
        payload["timestamp"] = instant.isoformat()
    return payload


def fields(prefix: str, record: dict | None = None) -> tuple[str, str, str, str]:
    record = record or {}
    left, right = st.columns(2)
    with left:
        title = st.text_input(
            "標題", value=record.get("title", ""), max_chars=200, key=f"{prefix}_title"
        )
        value = st.text_input(
            "數值",
            value=str(record.get("value", "")),
            max_chars=64,
            key=f"{prefix}_value",
            help="最多 12 位整數與 6 位小數；以十進位文字保存精度。",
            validate=(NUMBER_PATTERN, "請輸入數字，最多 12 位整數與 6 位小數，可使用正負號。"),
            required=True,
        )
    with right:
        old_category = record.get("category")
        category = st.selectbox(
            "分類",
            list(DEVICE_CATEGORIES),
            index=list(DEVICE_CATEGORIES).index(old_category)
            if old_category in DEVICE_CATEGORIES
            else None,
            format_func=DEVICE_CATEGORIES.get,
            placeholder="選擇設備 A、設備 B 或設備 C",
            key=f"{prefix}_category",
        )
        if old_category and old_category not in DEVICE_CATEGORIES:
            st.info(f"原分類「{old_category}」已保留；儲存變更前請選擇對應設備。")
        if record.get("timestamp") and not can_display_taipei(record["timestamp"]):
            st.warning("原時間超出台灣日期可顯示範圍，儲存時會保留原始時間。")
            timestamp = record["timestamp"]
        else:
            default = (
                as_taipei(record["timestamp"])
                if record.get("timestamp")
                else st.session_state.setdefault(
                    f"{prefix}_time_seed", datetime.now(TAIPEI).replace(microsecond=0)
                )
            )
            timestamp = date_time_fields(f"{prefix}_timestamp", "資料", default, required=True)
    return title, value, category or "", timestamp
