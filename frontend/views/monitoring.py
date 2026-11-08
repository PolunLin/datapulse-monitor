"""Device-scoped live reconciliation with a shared analysis/history entry point."""

import math
import time
from datetime import UTC, datetime

import streamlit as st

from clients.api import APIError
from clients.realtime import SampleEvent, parse_stored_sample
from components.charts import bar_chart, trend_chart
from state import session
from state.analysis import open_system_history
from state.monitor import MonitorBuffer
from utils.timezone import can_display_taipei, format_taipei

SYNC_SECONDS = 5
STALE_SECONDS = 10


def _utc_now() -> datetime:
    return datetime.now(UTC)


def reset_stream() -> None:
    session.stop_receiver()
    st.session_state["monitor_buffer"] = MonitorBuffer()
    st.session_state["monitor_sync_due"] = True
    for key in ("sync_error", "synced_at", "cursor", "sync_attempt", "reconnect_count"):
        st.session_state.pop(f"monitor_{key}", None)
    st.session_state["monitor_waiting_since"] = time.monotonic()


def change_device() -> None:
    reset_stream()
    st.session_state["monitor_paused"] = False


def toggle_pause() -> None:
    st.session_state["monitor_paused"] = not st.session_state.get("monitor_paused", False)
    session.stop_receiver()
    if not st.session_state["monitor_paused"]:
        reset_stream()


def _stored_events(result: dict, device_id: str, after_id: int | None) -> list[SampleEvent]:
    rows = result["items"]
    if not isinstance(rows, list) or len(rows) > 300:
        raise ValueError("Invalid sample page")
    events = [parse_stored_sample(row) for row in rows]
    if any(event.device_id != device_id for event in events):
        raise ValueError("Unexpected device")
    cursor, has_more = result["next_cursor"], result["has_more"]
    if type(cursor) is not int or cursor < 0 or type(has_more) is not bool:
        raise ValueError("Invalid sample cursor")
    ids = [row["id"] for row in rows]
    if ids != sorted(set(ids)) or (after_id is not None and any(i <= after_id for i in ids)):
        raise ValueError("Invalid sample order")
    if cursor != (ids[-1] if ids else after_id or 0) or (has_more and not rows):
        raise ValueError("Invalid sample progress")
    if after_id is None and has_more:
        raise ValueError("Invalid initial snapshot")
    return events


def _synchronize(buffer: MonitorBuffer, device_id: str) -> None:
    now = time.monotonic()
    if (
        not st.session_state.get("monitor_sync_due", True)
        and now - st.session_state.get("monitor_sync_attempt", 0) < SYNC_SECONDS
    ):
        return
    st.session_state["monitor_sync_attempt"] = now
    st.session_state["monitor_sync_due"] = False
    after_id = st.session_state.get("monitor_cursor")
    try:
        result = session.get_api_client().monitoring_sample_feed(
            device_id=device_id, after_id=after_id, limit=300
        )
        events = _stored_events(result, device_id, after_id)
    except APIError as exc:
        if exc.is_unauthorized:
            session.expire_session()
        st.session_state["monitor_sync_error"] = (
            f"歷史同步未完成：{exc.message} 畫面可能缺少資料，會自動重試。"
        )
        return
    except (KeyError, TypeError, ValueError):
        st.session_state["monitor_sync_error"] = (
            "歷史同步回應格式不正確，畫面可能缺少資料，會自動重試。"
        )
        return
    buffer.append(events)
    # Only committed REST rows advance the cursor; WS timestamps can precede
    # a delayed database commit and must never move this checkpoint forward.
    st.session_state["monitor_cursor"] = result["next_cursor"]
    st.session_state["monitor_sync_due"] = result["has_more"]
    st.session_state["monitor_synced_at"] = format_taipei(_utc_now(), "%H:%M:%S") + " 台灣時間"
    st.session_state.pop("monitor_sync_error", None)


def _display_freshness(events: list[SampleEvent]) -> bool:
    """Report measurement age independently of socket and history-request health."""
    threshold = st.session_state.get("monitor_stale_seconds", STALE_SECONDS)
    if events:
        latest = max(event.timestamp for event in events)
        age = max(0.0, (_utc_now() - latest).total_seconds())
        st.caption(f"最新資料距今 {int(age)} 秒 · 超過 {threshold:g} 秒未更新即告警")
        if age >= threshold:
            st.warning(
                f"資料停止更新：已 {int(age)} 秒未收到較新的設備資料，請確認設備與資料來源。"
            )
            return False
        return True
    waiting_since = st.session_state.setdefault("monitor_waiting_since", time.monotonic())
    waiting = max(0.0, time.monotonic() - waiting_since)
    st.caption(f"等待第一筆設備資料 · 已等待 {int(waiting)} 秒")
    if waiting >= threshold:
        st.warning(f"資料停止更新：等待超過 {threshold:g} 秒仍沒有設備資料，請確認設備與資料來源。")
    return False


def _live_charts(events: list[SampleEvent]) -> None:
    if any(not can_display_taipei(event.timestamp) for event in events):
        st.warning("圖表略過超出台灣時間可顯示範圍的資料；表格保留其原始 UTC 時間。")
    anomalies = [event for event in events if event.is_anomaly]
    latest = events[-1] if events else None
    for column, label, value in zip(
        st.columns(4),
        ["最新數值", "異常事件", "畫面筆數", "最近更新（台灣）"],
        [
            f"{latest.value:.2f} {latest.unit}".strip() if latest else "—",
            str(len(anomalies)),
            str(len(events)),
            format_taipei(latest.timestamp, "%H:%M:%S") if latest else "—",
        ],
        strict=True,
    ):
        column.metric(label, value, border=True)
    with st.container(border=True):
        st.subheader("數值趨勢")
        if events:
            st.altair_chart(trend_chart(events), width="stretch", key="monitor_trend")
            st.caption("灰色虛線為閾值，紅點為異常；時間均為台灣時間，間隔超過 10 秒不連線。")
        else:
            st.info("尚無資料；已落庫歷史與新事件會在同步後顯示。")
    bars, alerts = st.columns([3, 2])
    with bars, st.container(border=True):
        st.subheader("最近數值")
        if events:
            st.altair_chart(bar_chart(events), width="stretch", key="monitor_bars")
        else:
            st.caption("有資料後顯示最近 30 筆數值。")
    with alerts, st.container(border=True):
        st.subheader("異常告警")
        if anomalies:
            st.error(f"目前顯示的 {len(events)} 筆中，有 {len(anomalies)} 筆數值超過閾值。")
            st.dataframe(
                [
                    {
                        "時間（台灣）": format_taipei(event.timestamp),
                        "設備": event.device_name,
                        "數值": str(event.value),
                        "單位": event.unit,
                        "閾值": str(event.threshold),
                    }
                    for event in reversed(anomalies[-20:])
                ],
                hide_index=True,
                width="stretch",
                height=180,
            )
        elif events:
            st.success("目前顯示的資料沒有異常。")
        else:
            st.caption("超過閾值的事件會顯示於此。")


@st.fragment(run_every=1)
def live_panel() -> None:
    # A scheduled live fragment must not reopen a receiver after page navigation.
    if st.session_state.get("monitor_view_open", True) is False:
        session.stop_receiver()
        return
    if not st.session_state.get("auth_token") or session.session_expired():
        session.expire_session()
    device_id = st.session_state.get("monitor_device", "device-001")
    buffer = st.session_state.setdefault("monitor_buffer", MonitorBuffer())
    if st.session_state.get("monitor_paused", False):
        if not session.verify_paused_session():
            return
        st.info("畫面更新已暫停；設備仍持續產生與保存資料。繼續後會重新載入最近資料。")
    else:
        # Subscribe before the snapshot, then reconcile only newly committed rows.
        receiver = session.get_receiver(device_id=device_id)
        snapshot = receiver.snapshot()
        if snapshot.status == "auth_failed":
            session.expire_session()
        if snapshot.reconnect_count != st.session_state.get("monitor_reconnect_count", 0):
            st.session_state["monitor_sync_due"] = True
        st.session_state["monitor_reconnect_count"] = snapshot.reconnect_count
        _synchronize(buffer, device_id)
        snapshot = receiver.snapshot()
        if snapshot.status == "auth_failed":
            session.expire_session()
        buffer.append(event for event in receiver.drain() if event.device_id == device_id)
        sync_error = st.session_state.get("monitor_sync_error")
        if sync_error:
            st.warning(sync_error)
        fresh = _display_freshness(buffer.recent(300)) if device_id != "legacy" else False
        if snapshot.status == "connected":
            if sync_error:
                st.caption("即時連線已連接，歷史同步尚未恢復。")
            elif device_id == "legacy":
                st.caption("歷史資料檢視 · 不等待新的設備資料")
            elif fresh:
                st.caption("🟢 已連線 · 設備資料持續更新")
            else:
                st.caption("即時連線已連接 · 請留意資料更新狀態")
        elif snapshot.status in {"connecting", "reconnecting"}:
            st.info(snapshot.message)
        else:
            st.warning(snapshot.message)
            if st.button("重新連線", key="monitor_retry"):
                reset_stream()
                st.rerun()
        if snapshot.reconnect_count:
            st.caption(f"已重新連線 {snapshot.reconnect_count} 次；最近的已落庫資料會重新同步。")
        if snapshot.dropped_count:
            st.warning(
                f"接收緩衝區已略過 {snapshot.dropped_count} 筆較舊事件；"
                "最近的已落庫資料會重新同步。"
            )
        if snapshot.invalid_count:
            st.warning(f"已略過 {snapshot.invalid_count} 筆格式不符的事件。")
    if synced := st.session_state.get("monitor_synced_at"):
        st.caption(f"歷史同步於 {synced}")
    _live_charts(buffer.recent(st.session_state.get("monitor_limit", 120)))
    st.caption(
        "畫面保留最近最多 300 筆，每 5 秒增量同步已落庫資料；即時事件可能仍待批次寫入。"
        "告警表顯示其中最近 20 筆異常。更早資料請至「資料分析」查詢。"
    )


def render() -> None:
    st.title("即時監控")
    st.write("設備持續產生與保存資料；選擇設備查看最近趨勢，歷史查詢與統計位於資料分析。")
    if not st.session_state.get("monitor_view_open", False):
        reset_stream()
        st.session_state["monitor_paused"] = False
        st.session_state["monitor_view_open"] = True
    try:
        catalog = session.get_api_client().monitoring_devices()
        devices = {item["id"]: item for item in catalog["items"]}
        if not devices:
            raise ValueError("No devices")
        interval = catalog.get("sample_interval_seconds", 1)
        if type(interval) not in {int, float} or not math.isfinite(interval) or interval <= 0:
            raise ValueError("Invalid sample interval")
        st.session_state["monitor_stale_seconds"] = max(STALE_SECONDS, interval * 3)
    except APIError as exc:
        session.stop_receiver()
        session.handle_api_error(exc)
        return
    except (KeyError, TypeError, ValueError):
        session.stop_receiver()
        st.error("設備清單暫時無法讀取，請重新整理。")
        return
    if st.session_state.get("monitor_device") not in devices:
        st.session_state["monitor_device"] = next(iter(devices))
        change_device()
    selected = st.selectbox(
        "設備",
        list(devices),
        format_func=lambda identifier: f"{devices[identifier]['name']} · {identifier}",
        key="monitor_device",
        on_change=change_device,
        persist_state="session",
    )
    device = devices[selected]
    st.caption(
        f"分類：{device['category'] or '依舊資料'} · 單位：{device['unit'] or '未指定'} · "
        f"異常閾值：{device['threshold'] if device['threshold'] is not None else '依每筆資料'}"
    )
    if selected == "legacy":
        st.info("這些舊資料未記錄設備來源，保留為獨立歷史；不會再產生此類新樣本。")
    if st.button("查看歷史與分析", key="monitor_open_analysis"):
        from views import analysis

        session.stop_receiver()
        st.session_state["monitor_view_open"] = False
        open_system_history(selected)
        st.switch_page(st.Page(analysis.render, title="資料分析", url_path="analysis"))
        return
    limit, pause, refresh = st.columns([2, 2, 2], vertical_alignment="bottom")
    limit.selectbox(
        "顯示筆數", [30, 120, 300], index=1, key="monitor_limit", persist_state="session"
    )
    pause.button(
        "繼續畫面更新" if st.session_state.get("monitor_paused") else "暫停畫面更新",
        on_click=toggle_pause,
        width="stretch",
        type="primary",
    )
    refresh.button(
        "重新同步",
        on_click=reset_stream,
        width="stretch",
        disabled=st.session_state.get("monitor_paused", False),
    )
    live_panel()
