"""Summarise public load and recovery artifacts without contacting a running service."""

from __future__ import annotations

import argparse
import json
import re
import time
from collections import defaultdict
from pathlib import Path


def read_json(path, default=None):
    try:
        return json.loads(path.read_text())
    except (OSError, ValueError):
        return default


def read_jsonl(path):
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    rows = []
    for line in lines:
        try:
            value = json.loads(line)
            if isinstance(value, dict):
                rows.append(value)
        except ValueError:
            continue
    return rows


def rounded(value):
    return round(value, 3) if isinstance(value, (int, float)) else None


def find_recovery(rows, anchor, end, *, fresh=False):
    scenario_observations = sorted(
        (row for row in rows if row["started_at"] < end),
        key=lambda row: row["started_at"],
    )

    def good(row):
        if not row.get("success"):
            return False
        age = row.get("latest_sample_age_seconds")
        return not fresh or (isinstance(age, (int, float)) and 0 <= age <= 15)

    failures = [row for row in scenario_observations if not good(row)]
    last_failure_finished = max((row["finished_at"] for row in failures), default=None)
    # A successful triple before a delayed shutdown is not recovery. Also wait
    # past the completion of failing in-flight probes started before the anchor.
    search_after = max(anchor, last_failure_finished or anchor)
    observations = [row for row in scenario_observations if row["started_at"] >= anchor]
    eligible = [row for row in observations if row["started_at"] >= search_after]
    first = None
    confirmed = None
    for index in range(max(0, len(eligible) - 2)):
        triple = eligible[index : index + 3]
        if all(good(row) for row in triple):
            first = triple[0]["started_at"]
            confirmed = triple[2]["finished_at"]
            break
    final_stable = len(observations) >= 3 and all(good(row) for row in observations[-3:])
    no_failure_observed = not failures
    delay = rounded(first - anchor) if first is not None else None
    return {
        "anchor_at": anchor,
        "first_recovered_at": first,
        "recovery_confirmed_at": confirmed,
        "recovery_delay_seconds": delay if not no_failure_observed else None,
        "probe_confirmation_delay_seconds": delay if no_failure_observed else None,
        "no_failure_observed": no_failure_observed,
        "timing_interpretation": "probe_confirmation_only_no_failure_observed"
        if no_failure_observed
        else "stable_recovery_after_last_failed_or_stale_probe",
        "last_failed_or_stale_finished_at": last_failure_finished,
        "recovery_search_after_at": search_after,
        "within_60_seconds": first is not None and first - anchor < 60,
        "final_three_probes_successful": final_stable,
        "probes_after_anchor": len(observations),
        "failed_or_stale_probes_in_scenario": len(failures),
        "failed_or_stale_probes_after_anchor": sum(
            row["finished_at"] >= anchor for row in failures
        ),
        "failures_after_first_recovery": sum(row["started_at"] > first for row in failures)
        if first is not None
        else None,
        "freshness_requirement_seconds": 15 if fresh else None,
    }


def scenario_summary(name, marks, all_marks, report):
    start_marks = [mark for mark in marks if mark.get("phase") == "fault_start"]
    if not start_marks:
        return {"scenario": name, "outcome": "incomplete", "reason": "no_fault_start_mark"}
    started = min(mark["timestamp"] for mark in start_marks)
    later = [
        mark["timestamp"]
        for mark in all_marks
        if mark["timestamp"] > started and mark.get("scenario") != name
    ]
    end = min(later, default=report.get("updated_at", time.time()))
    removed_marks = [mark for mark in marks if mark.get("phase") == "fault_removed"]
    # Restart and crash RTO intentionally include the command/process startup time.
    needs_removed = name in {"websocket_toggle", "database_pause"}
    removed = min((mark["timestamp"] for mark in removed_marks), default=None)
    anchor = removed if needs_removed and removed is not None else started
    anchor_kind = "fault_removed" if needs_removed and removed is not None else "fault_start"
    complete = not needs_removed or removed is not None
    http_rows = [
        row
        for row in report.get("http_observations", [])
        if row.get("started_at", 0) < end and row.get("finished_at", 0) >= started
    ]
    transitions = [
        row for row in report.get("websocket_transitions", []) if started <= row.get("at", 0) < end
    ]
    endpoints = {}
    for route in ("live", "ready", "sample_feed"):
        rows = [row for row in http_rows if row.get("route") == route]
        failures = [row for row in rows if not row.get("success")]
        recovery = find_recovery(rows, anchor, end, fresh=route == "sample_feed")
        first_failure = min((row["started_at"] for row in failures), default=None)
        last_failure = max((row["started_at"] for row in failures), default=None)
        recovered = recovery["first_recovered_at"]
        endpoints[route] = {
            "probes": len(rows),
            "failures": len(failures),
            "first_failure_at": first_failure,
            "last_failure_at": last_failure,
            "observed_first_failure_to_recovery_seconds": rounded(recovered - first_failure)
            if recovered is not None and first_failure is not None
            else None,
            **recovery,
        }
        complete = complete and bool(rows)
    clients = {}
    count = report.get("configuration", {}).get("websocket_clients", 0)
    for index in range(count):
        rows = [row for row in transitions if row.get("client") == index]
        disconnects = [row["at"] for row in rows if row.get("kind") == "disconnected"]
        last_disconnect = max(disconnects, default=None)
        recovery_after = max(anchor, last_disconnect or anchor)
        first_samples = [
            row["at"]
            for row in rows
            if row.get("kind") == "first_sample" and row["at"] >= recovery_after
        ]
        first = min(first_samples, default=None)
        last_sample = max(first_samples, default=None)
        clients[index] = {
            "first_sample_after_anchor_at": first,
            "recovery_delay_seconds": rounded(first - anchor)
            if first is not None and disconnects
            else None,
            "probe_confirmation_delay_seconds": rounded(first - anchor)
            if first is not None and not disconnects
            else None,
            "no_failure_observed": not disconnects,
            "recovery_search_after_at": recovery_after,
            "last_disconnect_at": last_disconnect,
            "within_60_seconds": first is not None and first - anchor < 60,
            "disconnects_observed": len(disconnects),
            "first_disconnect_to_sample_seconds": rounded(first - min(disconnects))
            if first is not None and disconnects
            else None,
            "last_observed_disconnect_followed_by_sample": last_sample is not None
            and (last_disconnect is None or last_sample > last_disconnect),
        }
    http_recovered = all(
        route["within_60_seconds"] and route["final_three_probes_successful"]
        for route in endpoints.values()
    )
    ws_recovered = bool(clients) and all(
        client["within_60_seconds"] and client["last_observed_disconnect_followed_by_sample"]
        for client in clients.values()
    )
    rest_continuous = all(route["failures"] == 0 for route in endpoints.values())
    recovered = http_recovered and ws_recovered
    if name == "websocket_toggle":
        recovered = recovered and rest_continuous
    if not complete:
        outcome = "incomplete"
    else:
        outcome = "recovered" if recovered else "failed"
    delays = [
        client["recovery_delay_seconds"]
        for client in clients.values()
        if client["recovery_delay_seconds"] is not None
    ]
    return {
        "scenario": name,
        "outcome": outcome,
        "fault_started_at": started,
        "fault_removed_at": removed,
        "observation_window_end_at": end,
        "recovery_anchor": anchor_kind,
        "recovery_anchor_at": anchor,
        "injected_fault_duration_seconds": rounded(removed - started)
        if removed is not None
        else None,
        "command_completed_at": next(
            (
                mark["timestamp"]
                for mark in marks
                if mark.get("phase") in {"restart_command_completed", "signal_delivered"}
            ),
            None,
        ),
        "http": endpoints,
        "rest_continuous_during_scenario": rest_continuous,
        "websocket_clients": clients,
        "websocket_clients_recovered": sum(
            value["within_60_seconds"] for value in clients.values()
        ),
        "websocket_recovery_max_seconds": max(delays, default=None),
    }


def restart_evidence_summary(evidence):
    if evidence is None:
        return {"outcome": "incomplete", "restart_verified": None, "reason": "evidence_missing"}
    before = evidence.get("before", {})
    after = evidence.get("after", {})
    earlier = before.get("restart_count")
    later = after.get("restart_count")
    counts_increased = isinstance(earlier, int) and isinstance(later, int) and later > earlier
    verified = evidence.get("automatic_restart_verified") is True and counts_increased
    return {
        "outcome": "verified" if verified else "failed",
        "restart_verified": verified,
        "restart_count_before": earlier,
        "restart_count_after": later,
        "after_status": after.get("status"),
        "oom_killed": after.get("oom_killed"),
        "signal_command_error_type": evidence.get("signal_command_error_type"),
    }


def committed_sample_summary(evidence):
    if evidence is None:
        return {"outcome": "incomplete", "reason": "evidence_missing"}
    sampled = evidence.get("sampled_previously_committed", 0)
    confirmed = evidence.get("confirmed", 0)
    if not sampled:
        outcome = "incomplete"
    else:
        outcome = "verified" if confirmed == sampled else "failed"
    return {
        "outcome": outcome,
        "sampled_previously_committed": sampled,
        "confirmed": confirmed,
        "unconfirmed_event_ids": [
            row["event_id"]
            for row in evidence.get("events", [])
            if row.get("confirmed") is not True
        ],
        "scope": "sample of previously confirmed commits; not an exhaustive database audit",
    }


def availability_summary(report, timeline, crash_evidence=None, committed_evidence=None):
    if not report:
        return {"outcome": "incomplete", "reason": "availability_report_missing"}
    all_marks = sorted(timeline, key=lambda row: row.get("timestamp", 0))
    grouped = defaultdict(list)
    for row in all_marks:
        grouped[row.get("scenario")].append(row)
    names = ("websocket_toggle", "graceful_restart", "database_pause", "backend_crash")
    scenarios = [scenario_summary(name, grouped[name], all_marks, report) for name in names]
    restart = restart_evidence_summary(crash_evidence)
    committed = committed_sample_summary(committed_evidence)
    crash = next(row for row in scenarios if row["scenario"] == "backend_crash")
    crash["recovery_outcome"] = crash["outcome"]
    crash["restart_evidence"] = restart
    if restart["outcome"] != "verified":
        crash["outcome"] = "incomplete" if restart["outcome"] == "incomplete" else "failed"
    complete = report.get("status") == "complete"
    outcomes = [row["outcome"] for row in scenarios]
    if not complete or "incomplete" in outcomes or committed["outcome"] == "incomplete":
        outcome = "incomplete"
    elif all(value == "recovered" for value in outcomes) and committed["outcome"] == "verified":
        outcome = "recovered"
    else:
        outcome = "failed"
    reconciliation = report.get("reconciliation", {})
    return {
        "outcome": outcome,
        "observer_status": report.get("status"),
        "acceptance": {
            "recovery_less_than_seconds": 60,
            "http_consecutive_successes": 3,
            "sample_feed_latest_age_max_seconds": 15,
            "websocket_requires_first_sample_per_client_after_anchor": True,
            "websocket_toggle_requires_no_rest_failures": True,
        },
        "scenarios": scenarios,
        "previously_committed_sample": committed,
        "sampled_persistence": {
            "status": reconciliation.get("status"),
            "sampled_events": reconciliation.get("candidate_count"),
            "outcomes": reconciliation.get("outcomes", {}),
            "missing_pending_event_ids": [
                row["event_id"]
                for row in reconciliation.get("events", [])
                if row.get("outcome") == "missing_pending"
            ],
            "scope": "sampled received pending events; not an exhaustive zero-loss audit",
            "affects_recovery_outcome": False,
        },
    }


def websocket_summary(path, report):
    configuration = report.get("configuration", {})
    device_id = configuration.get("device_id")
    profile = (
        "http_only" if configuration.get("http_only") else device_id if device_id else "all_devices"
    )
    stages = []
    for row in report.get("stages", []):
        stages.append(
            {
                "source": path.name,
                "profile": profile,
                "device_id": device_id,
                "target": row.get("target"),
                "duration_seconds": row.get("duration_seconds"),
                "connections_start": row.get("connections_start"),
                "connections_end": row.get("connections_end"),
                "fresh_connections_end": row.get("fresh_connections_end"),
                "close_codes": row.get("close_codes", {}),
                "sample_age_p95_ms": row.get("sample_age_ms", {}).get("p95"),
                "sample_age_p99_ms": row.get("sample_age_ms", {}).get("p99"),
                "samples_per_client_second": row.get("samples_per_client_second"),
                "generator_loop_lag_p95_ms": row.get("generator", {})
                .get("event_loop_lag_ms", {})
                .get("p95"),
                "generator_cpu_one_core_percent": row.get("generator", {}).get(
                    "cpu_one_core_percent"
                ),
                "provisionally_stable": row.get("provisionally_stable", False),
                "failure_reasons": row.get("failure_reasons", []),
                "endpoints": {
                    route: {
                        "success_ratio": value.get("success_ratio"),
                        "p95_ms": value.get("latency_ms", {}).get("p95"),
                        "dropped_by_generator": value.get("dropped"),
                    }
                    for route, value in row.get("endpoints", {}).items()
                },
            }
        )
    stable = [row["target"] for row in stages if row["provisionally_stable"]]
    return {
        "source": path.name,
        "profile": profile,
        "device_id": device_id,
        "traffic_profile": configuration.get("traffic_profile", profile),
        "complete": "finished_at" in report,
        "highest_provisionally_stable_target": max(stable, default=None),
        "highest_target_tested": max((row["target"] for row in stages), default=None),
        "stop_reason": report.get("stop_reason"),
        "configuration": report.get("configuration", {}),
        "stages": stages,
        "sampled_persistence": report.get("persistence_verification", {}),
    }


def memory_bytes(value):
    match = re.match(r"\s*([\d.]+)\s*([A-Za-z]+)", str(value))
    if not match:
        return None
    factors = {
        "B": 1,
        "KB": 1000,
        "MB": 1000**2,
        "GB": 1000**3,
        "KiB": 1024,
        "MiB": 1024**2,
        "GiB": 1024**3,
        "TiB": 1024**4,
    }
    factor = factors.get(match[2])
    return round(float(match[1]) * factor) if factor else None


def resource_summary(rows):
    grouped = defaultdict(list)
    for row in rows:
        grouped[row.get("Name", row.get("Container", "unknown"))].append(row)
    result = {}
    for name, samples in grouped.items():
        cpu = []
        memory = []
        pids = []
        for row in samples:
            try:
                cpu.append(float(str(row.get("CPUPerc", "")).rstrip("%")))
            except ValueError:
                pass
            used = memory_bytes(str(row.get("MemUsage", "")).split("/")[0])
            if used is not None:
                memory.append(used)
            try:
                pids.append(int(row.get("PIDs", 0)))
            except (TypeError, ValueError):
                pass
        result[name] = {
            "samples": len(samples),
            "cpu_percent_max": rounded(max(cpu)) if cpu else None,
            "cpu_percent_mean": rounded(sum(cpu) / len(cpu)) if cpu else None,
            "memory_used_bytes_max": max(memory, default=None),
            "memory_used_bytes_mean": round(sum(memory) / len(memory)) if memory else None,
            "pids_max": max(pids, default=None),
        }
    return result


def summarize(directory):
    metadata = read_json(directory / "metadata.json", {})
    ws_runs = [
        websocket_summary(path, read_json(path, {}))
        for path in sorted(directory.glob("websocket-capacity*.json"))
    ]
    http_runs = [
        (path, read_json(path, {})) for path in sorted(directory.glob("http-capacity*.json"))
    ]
    http_stages = [
        {"source": path.name, **row}
        for path, report in http_runs
        for row in report.get("stages", [])
    ]
    stable_http = [row["target_rps"] for row in http_stages if row.get("stable")]
    highest_by_profile = {}
    sustained_by_profile = {}
    for row in ws_runs:
        target = row["highest_provisionally_stable_target"]
        if row["profile"] != "http_only" and target is not None:
            highest_by_profile[row["profile"]] = max(
                highest_by_profile.get(row["profile"], 0), target
            )
        for stage in row["stages"]:
            if (
                row["profile"] != "http_only"
                and stage["provisionally_stable"]
                and (stage.get("duration_seconds") or 0) >= 180
            ):
                sustained_by_profile[row["profile"]] = max(
                    sustained_by_profile.get(row["profile"], 0), stage["target"]
                )
    return {
        "schema_version": 1,
        "generated_at": time.time(),
        "metadata": metadata,
        "websocket_runs": ws_runs,
        "highest_stable_by_profile": highest_by_profile,
        "highest_sustained_target_by_profile": sustained_by_profile,
        "sustained_definition": "tested >=180 seconds and met provisional criteria; not zero loss",
        "global_websocket_max_scope": "maximum across tested profiles; consult per-profile results",
        "highest_provisionally_stable_websocket_target": max(
            (
                row["highest_provisionally_stable_target"]
                for row in ws_runs
                if row["highest_provisionally_stable_target"] is not None
            ),
            default=None,
        ),
        "http_capacity": {
            "scope": "authenticated sample-feed reads, one request per scheduled iteration",
            "highest_stable_target_rps": max(stable_http, default=None),
            "stages": http_stages,
        },
        "availability": availability_summary(
            read_json(directory / "availability.json"),
            read_jsonl(directory / "timeline.jsonl"),
            read_json(directory / "crash-evidence.json"),
            read_json(directory / "committed-event-verification.json"),
        ),
        "resources": resource_summary(
            [row for path in sorted(directory.glob("resources*.jsonl")) for row in read_jsonl(path)]
        ),
        "limitations": [
            "Results apply to recorded hardware, resource limits, single worker and workload only.",
            "Load generator and Docker target share a host; generator can be a bottleneck.",
            "Short stages provide provisional capacity, not a long-duration stability guarantee.",
            "Highest tested stable target is a measured lower bound, not an exact universal limit.",
            "HTTP workload is authenticated sample-feed reads; no general API capacity claim.",
            "Availability covers backend API/WebSocket recovery, not browser or Streamlit uptime.",
            "Fault exercises do not establish high availability or a monthly availability SLO.",
            "Persistence reconciliation is sampled; pending memory events may be lost on crash.",
            "HTTP recovery uses 1-second probes, 3 consecutive successes and a 3-second timeout.",
            "Missing or partial artifacts produce incomplete results, never an assumed pass.",
        ],
    }


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--run-dir", type=Path, required=True)
    args = parser.parse_args()
    result = summarize(args.run_dir)
    path = args.run_dir / "summary.json"
    temporary = path.with_suffix(".json.tmp")
    temporary.write_text(json.dumps(result, indent=2, allow_nan=False) + "\n")
    temporary.replace(path)
    print(
        json.dumps(
            {
                "event": "summary_written",
                "output": str(path),
                "highest_provisionally_stable_websocket_target": result[
                    "highest_provisionally_stable_websocket_target"
                ],
                "availability_outcome": result["availability"]["outcome"],
                "highest_stable_by_profile": result["highest_stable_by_profile"],
            }
        )
    )


if __name__ == "__main__":
    main()
