"""Combine adjacent verified population intervals without double-counting a saved pin."""

from __future__ import annotations

import argparse
from hashlib import sha256
import importlib.util
import json
from pathlib import Path
import sys

RESIDENT_THRESHOLD = 240
ORDINARY_COUNT = "ordinary_active_named_excluding_player_and_original_key_npcs"

_PROJECT_ROOT = Path(__file__).resolve().parents[1]
_AUDIT = None


def _audit_helpers():
    """Load backend audit code only when a caller requests terminal proof."""
    global _AUDIT
    if _AUDIT is None:
        backend_path = str(_PROJECT_ROOT / "backend")
        if backend_path not in sys.path:
            sys.path.insert(0, backend_path)
        spec = importlib.util.spec_from_file_location(
            "merge_capacity_terminal_audit", _PROJECT_ROOT / "scripts" / "audit-world-capacity.py",
        )
        if spec is None or spec.loader is None:
            raise RuntimeError("Could not load the shared capacity audit helpers.")
        module = importlib.util.module_from_spec(spec)
        sys.modules[spec.name] = module
        spec.loader.exec_module(module)
        _AUDIT = module
    return _AUDIT


class MergeError(ValueError):
    """The supplied reports do not prove one continuous chain."""


def _check_interval(report):
    interval = report["interval"]
    if (report["audit"] != "capacity_interval_population" or report["schema"] != 1
        or report["status"] != "verified" or report["success"] is not True
        or interval["commit_boundary_sequence_verified"] is not True):
        raise MergeError("Each input must be a verified population interval.")
    start, end = interval["start_checkpoint"], interval["end_checkpoint"]
    if any(int(start[field]) >= int(end[field]) for field in ("year", "tick", "sequence", "state_version")):
        raise MergeError("Each interval must move forward between its saved pins.")
    count = interval["observed_commit_boundary_count"]
    if (type(count) is not int or count < 2 or count != interval["expected_commit_boundary_count"]
        or interval["first_sequence"] != start["sequence"] or interval["last_sequence"] != end["sequence"]):
        raise MergeError("Interval commit counts or saved pin sequences do not match.")
    for row in report["population"]["minimum_counts"].values():
        if (type(row["count"]) is not int or row["count"] < 0
            or any(not int(start[field]) <= int(row[field]) <= int(end[field]) for field in ("sequence", "tick"))):
            raise MergeError("A population minimum lies outside its verified interval.")
    return start, end, count


def _identity(report):
    scope = report["scope"]
    return (
        scope["world_id"], scope["branch_id"], scope["verified_root_genesis_hash"],
        scope["original_key_identity_source"], report["interval"]["calendar_basis"],
        report["population"]["definitions"],
    )


def _checked_shortfalls(report, start, end, boundary_count):
    population = report["population"]
    minimum = population["minimum_counts"][ORDINARY_COUNT]
    threshold_report = population.get("ordinary_population_shortfalls")
    if threshold_report is None:
        if minimum["count"] < RESIDENT_THRESHOLD:
            raise MergeError("A deficient legacy interval must be re-audited to recover its shortfall windows.")
        return []
    if threshold_report["threshold"] != RESIDENT_THRESHOLD:
        raise MergeError("The shortfall threshold must match the 240-resident acceptance criterion.")

    episodes = threshold_report["episodes"]
    previous = None
    for index, episode in enumerate(episodes):
        first, last = int(episode["start_sequence"]), int(episode["end_sequence"])
        began, ended = int(episode["start_tick"]), int(episode["end_tick"])
        deficient = episode["deficient_commit_count"]
        if (not int(start["sequence"]) <= first <= last <= int(end["sequence"])
            or not int(start["tick"]) <= began <= ended <= int(end["tick"])
            or not 0 <= episode["minimum_count"] < RESIDENT_THRESHOLD
            or type(deficient) is not int or not 1 <= deficient <= last - first + int(episode["right_censored"])
            or episode["duration_seconds_observed"] != ended - began
            or episode["left_censored"] != (first == int(start["sequence"]))
            or (episode["left_censored"] and (index != 0 or began != int(start["tick"])))
            or (episode["right_censored"] and
                (index != len(episodes) - 1 or (last, ended) != (int(end["sequence"]), int(end["tick"]))))
            or (previous is not None and (first <= int(previous["end_sequence"]) or began < int(previous["end_tick"])))):
            raise MergeError("Shortfall windows do not match their interval bounds.")
        previous = episode

    deficient = sum(row["deficient_commit_count"] for row in episodes)
    if (deficient != threshold_report["deficient_commit_count"] or deficient > boundary_count
        or bool(episodes) != (minimum["count"] < RESIDENT_THRESHOLD)
        or (episodes and min(row["minimum_count"] for row in episodes) != minimum["count"])):
        raise MergeError("Shortfall windows disagree with the verified interval minimum or counts.")
    return [row.copy() for row in episodes]


def _append_episodes(combined, incoming, shared):
    left_open = bool(combined and combined[-1]["right_censored"])
    right_open = bool(incoming and incoming[0]["left_censored"])
    if left_open != right_open:
        raise MergeError("Reports disagree about whether the shared state was below capacity.")
    if not left_open:
        combined.extend(incoming)
        return
    left, right = combined.pop(), incoming[0]
    if (left["end_sequence"], left["end_tick"]) != (shared["sequence"], shared["tick"]):
        raise MergeError("The shortfall seam is not the saved shared state.")
    combined.append({
        **left,
        "end_sequence": right["end_sequence"], "end_tick": right["end_tick"],
        "right_censored": right["right_censored"],
        "minimum_count": min(left["minimum_count"], right["minimum_count"]),
        "deficient_commit_count": left["deficient_commit_count"] + right["deficient_commit_count"] - 1,
        "duration_seconds_observed": int(right["end_tick"]) - int(left["start_tick"]),
    })
    combined.extend(incoming[1:])


def _merge_organization_gaps(ordered):
    """Join zero-member windows by organization identity at the verified shared pins."""
    institutions = [report.get("institutions", {}) for report in ordered]
    if any("censoring" not in part or not part.get("pin_matches_last_committed_sample") for part in institutions):
        return None
    by_id = {}
    names = {}
    for interval, part in zip(ordered, institutions, strict=True):
        observed = interval["interval"]["observed_commit_boundary_count"]
        if part["censoring"]["observed_commit_boundary_count"] != observed:
            raise MergeError("Institution windows do not cover the population report's commit boundaries.")
        incoming = {}
        for organization in part["organizations"]:
            ident = organization["organization_id"]
            names[ident] = organization["organization_name"]
            incoming[ident] = organization["gap_windows"]
        start = interval["interval"]["start_checkpoint"]
        if interval is ordered[0]:
            by_id = {ident: [window.copy() for window in windows] for ident, windows in incoming.items() if windows}
            continue
        for ident in by_id.keys() | incoming.keys():
            before = by_id.get(ident, [])
            after = [window.copy() for window in incoming.get(ident, [])]
            open_before = bool(before and before[-1]["right_censored"])
            open_after = bool(after and after[0]["left_censored"])
            if open_before != open_after:
                raise MergeError("Reports disagree about an organization's membership at the shared state.")
            if open_before:
                left, right = before.pop(), after.pop(0)
                if ((left["end_sequence"], left["end_tick"]) != (start["sequence"], start["tick"])
                    or (right["start_sequence"], right["start_tick"]) != (start["sequence"], start["tick"])):
                    raise MergeError("An organization's open gap does not match the shared state.")
                before.append({
                    **left,
                    **{key: right[key] for key in (
                        "right_censored", "end_sequence", "end_tick", "end_reason",
                        "last_zero_member_sequence", "last_zero_member_tick",
                    )},
                    "observed_zero_member_committed_state_count": (
                        left["observed_zero_member_committed_state_count"]
                        + right["observed_zero_member_committed_state_count"] - 1
                    ),
                    "duration_seconds_observed": int(right["end_tick"]) - int(left["start_tick"]),
                })
            before.extend(after)
            if before:
                by_id[ident] = before
    return {
        "observed_organization_count": len(names),
        "organizations_with_gaps": [
            {"organization_id": ident, "organization_name": names[ident], "gap_windows": windows}
            for ident, windows in sorted(by_id.items())
        ],
    }


def merge_intervals(reports):
    if not reports:
        raise MergeError("Provide at least one verified interval.")
    ordered = sorted(reports, key=lambda report: int(report["interval"]["start_checkpoint"]["sequence"]))
    identity = _identity(ordered[0])
    episodes, minima = [], {}
    previous_pin = None
    total = 0
    for report in ordered:
        start, end, observed = _check_interval(report)
        if _identity(report) != identity:
            raise MergeError("All reports must use the same world, branch, genesis, calendar and counting definitions.")
        incoming = _checked_shortfalls(report, start, end, observed)
        if previous_pin is None:
            episodes.extend(incoming)
            total = observed
        else:
            if start != previous_pin:
                raise MergeError("Adjacent intervals must share the exact saved pin, including its hash.")
            _append_episodes(episodes, incoming, start)
            total += observed - 1
        for metric, row in report["population"]["minimum_counts"].items():
            current = minima.get(metric)
            if current is None or (row["count"], int(row["sequence"])) < (current["count"], int(current["sequence"])):
                minima[metric] = row
        previous_pin = end
    summary = {
        "audit": "capacity_interval_chain",
        "schema": 1,
        "source": "Merged verified interval reports; the chain has not been independently replayed.",
        "world_id": identity[0], "branch_id": identity[1], "verified_root_genesis_hash": identity[2],
        "calendar_basis": identity[4],
        "start_checkpoint": ordered[0]["interval"]["start_checkpoint"],
        "end_checkpoint": previous_pin,
        "observed_distinct_commit_boundary_count": total,
        "minimum_counts": minima,
        "ordinary_population_shortfalls": {
            "threshold": RESIDENT_THRESHOLD,
            "episodes": episodes,
            "deficient_commit_count": sum(row["deficient_commit_count"] for row in episodes),
        },
    }
    organization_gaps = _merge_organization_gaps(ordered)
    if organization_gaps is not None:
        summary["organization_membership_gaps"] = organization_gaps
    return summary


_PREFIX_COVERAGE_FLAGS = (
    "replayed_from_root_genesis",
    "root_initial_state_hash_verified",
    "replay_initial_callback_matches_root_genesis",
    "replay_boundary_count_matches_versions_plus_initial",
    "replay_sequences_monotonic",
    "requested_boundary_replayed_from_manifest_pin",
    "stored_checkpoint_event_and_snapshot_match",
    "content_index_matches_root_genesis",
    "authored_key_id_index_valid",
    "authored_resident_id_index_valid",
    "seed_counts_match_content_index",
    "seed_counts_unchanged_from_root_genesis",
    "manifest_and_owner_identity_read_only",
)
_STRUCTURAL_METRICS = ("active_countries", "active_other_organizations", "active_places")
_PREFIX_ORDINARY_COUNT = "active_named_residents_excluding_player_and_authored_keys"


def _integer(value, label):
    if isinstance(value, bool):
        raise MergeError(f"{label} must be an integer.")
    if type(value) is int:
        return value
    if isinstance(value, str):
        try:
            return int(value)
        except ValueError as error:
            raise MergeError(f"{label} must be an integer.") from error
    raise MergeError(f"{label} must be an integer.")


def _row(row, label):
    if not isinstance(row, dict):
        raise MergeError(f"{label} is missing.")
    count = _integer(row.get("count"), f"{label} count")
    sequence = _integer(row.get("sequence"), f"{label} sequence")
    tick = _integer(row.get("tick"), f"{label} tick")
    if count < 0 or sequence < 0 or tick < 0:
        raise MergeError(f"{label} contains a negative value.")
    return {"count": count, "sequence": str(sequence), "tick": str(tick)}


def _earlier_minimum(left, right):
    if (left["count"], int(left["sequence"])) <= (right["count"], int(right["sequence"])):
        return left.copy()
    return right.copy()


def _validate_prefix(prefix):
    if not isinstance(prefix, dict):
        raise MergeError("The genesis prefix must be a JSON object.")
    try:
        scope = prefix["scope"]
        root = prefix["root_genesis"]
        coverage = prefix["coverage"]
        time = prefix["time"]
        capacity = prefix["capacity"]
        structural = prefix["structural_capacity"]
        institutions = prefix["institutional_continuity"]
    except (KeyError, TypeError) as error:
        raise MergeError("The genesis prefix is missing required audit sections.") from error
    if any(not isinstance(value, dict) for value in (scope, root, coverage, time, capacity, structural, institutions)):
        raise MergeError("The genesis prefix contains an invalid audit section.")
    if (prefix.get("audit") != "world_capacity_genesis_replay" or prefix.get("schema") != 1
        or prefix.get("status") != "checkpoint_prefix_verified" or prefix.get("success") is not True
        or prefix.get("replay_verified") is not True or scope.get("audit_mode") != "checkpoint_prefix"):
        raise MergeError("The prefix must be a successful, replay-verified checkpoint-prefix audit.")
    if scope.get("owner_matches") is not True:
        raise MergeError("The prefix owner identity was not verified.")
    for flag in _PREFIX_COVERAGE_FLAGS:
        if coverage.get(flag) is not True:
            raise MergeError(f"The prefix coverage flag {flag} is not verified.")
    if (coverage.get("database_or_manifest_mutated") is not False
        or type(coverage.get("model_calls")) is not int or coverage["model_calls"] != 0
        or coverage.get("sqlite_open_mode") != "uri_mode_ro_with_query_only"):
        raise MergeError("The prefix must be a read-only replay with no model calls or mutations.")
    counts = [
        _integer(coverage.get(key), f"Prefix {key}")
        for key in ("committed_states_audited", "distinct_sequences_audited", "expected_committed_states_including_initial")
    ]
    if counts[0] < 2 or counts[0] != counts[1] or counts[1] != counts[2]:
        raise MergeError("Prefix replay coverage counts do not agree.")
    if time.get("audited_boundary_replayed_and_stored") is not True:
        raise MergeError("The requested prefix checkpoint was not replayed and stored.")

    try:
        year = _integer(scope["checkpoint_year"], "Prefix checkpoint year")
        pin_raw = scope["pinned_checkpoint"]
        pin = {
            "sequence": _integer(pin_raw["sequence"], "Prefix pin sequence"),
            "tick": _integer(pin_raw["tick"], "Prefix pin tick"),
            "year": year,
            "state_version": _integer(pin_raw["state_version"], "Prefix pin state version"),
            "state_hash": pin_raw["state_hash"],
        }
        root_tick = _integer(root["current_tick"], "Root genesis tick")
        root_version = _integer(root["state_version"], "Root genesis state version")
        root_hash = root["state_hash"]
        calendar = time["calendar_basis"]
        day_seconds = _integer(calendar["day_seconds"], "Prefix calendar day seconds")
        year_days = _integer(calendar["year_days"], "Prefix calendar year days")
        root_calendar = root["calendar"]
        root_day_seconds = _integer(root_calendar["day_seconds"], "Genesis calendar day seconds")
        root_year_days = _integer(root_calendar["year_days"], "Genesis calendar year days")
    except (KeyError, TypeError) as error:
        raise MergeError("The prefix is missing its root or pinned-checkpoint metadata.") from error
    if (year <= 0 or pin["sequence"] <= 0 or pin["state_version"] <= 0 or root_tick != 0 or root_version != 0
        or not isinstance(root_hash, str) or not root_hash
        or not isinstance(pin["state_hash"], str) or not pin["state_hash"]
        or day_seconds <= 0 or year_days <= 0 or (day_seconds, year_days) != (root_day_seconds, root_year_days)):
        raise MergeError("The prefix root, calendar or checkpoint metadata is invalid.")
    expected_tick = root_tick + year * day_seconds * year_days
    if (pin["tick"] != expected_tick
        or _integer(scope.get("audited_through_sequence"), "Prefix audited-through sequence") != pin["sequence"]
        or _integer(scope.get("audited_through_tick"), "Prefix audited-through tick") != pin["tick"]
        or _integer(time.get("audited_boundary_tick"), "Prefix audited-boundary tick") != pin["tick"]
        or _integer(capacity.get("audited_through_year"), "Prefix audited-through year") != year
        or scope.get("world_id") is None or scope.get("branch_id") is None
        or root.get("branch_id") != scope.get("branch_id")):
        raise MergeError("The prefix endpoint does not match its exact checkpoint year, tick, sequence and version.")
    if counts[1] != pin["state_version"] - root_version + 1:
        raise MergeError("Prefix distinct-boundary coverage does not match its state-version span.")

    if (capacity.get("audit_interval_scope") != "checkpoint_prefix"
        or _integer(capacity.get("threshold_active_named_residents"), "Prefix ordinary threshold") != RESIDENT_THRESHOLD):
        raise MergeError("The prefix ordinary-population scope or threshold is invalid.")
    prefix_minimum = _row(
        capacity.get("minimum_counts", {}).get(_PREFIX_ORDINARY_COUNT), "Prefix ordinary-population minimum",
    )
    if (int(prefix_minimum["sequence"]) > pin["sequence"] or int(prefix_minimum["tick"]) > pin["tick"]):
        raise MergeError("The prefix ordinary-population minimum lies beyond its audited pin.")
    first_by_definition = capacity.get("first_below_240_by_definition")
    if not isinstance(first_by_definition, dict):
        raise MergeError("The prefix ordinary-population first-below evidence is missing.")
    first_below = first_by_definition.get(_PREFIX_ORDINARY_COUNT)
    if capacity.get("first_below_240") != first_below:
        raise MergeError("The prefix ordinary first-below summary disagrees with its per-definition evidence.")
    if first_below is None:
        if prefix_minimum["count"] < RESIDENT_THRESHOLD:
            raise MergeError("The deficient prefix has no persisted first-below evidence.")
    else:
        first_below = _row(first_below, "Prefix ordinary first-below evidence")
        if (first_below["count"] >= RESIDENT_THRESHOLD or first_below["count"] < prefix_minimum["count"]
            or int(first_below["sequence"]) > pin["sequence"] or int(first_below["tick"]) > pin["tick"]):
            raise MergeError("The prefix ordinary first-below evidence is out of range.")

    if structural.get("audit_interval_scope") != "checkpoint_prefix":
        raise MergeError("The prefix structural-capacity scope is invalid.")
    structural_checks = structural.get("continuous_checks_over_replayed_history")
    if not isinstance(structural_checks, dict):
        raise MergeError("The prefix structural gate coverage is missing.")
    prefix_structural = {}
    for metric in _STRUCTURAL_METRICS:
        threshold = _integer(structural.get("thresholds", {}).get(metric), f"Prefix {metric} threshold")
        row = _row(structural.get("minimum_counts", {}).get(metric), f"Prefix {metric} minimum")
        check = structural_checks.get(metric)
        if (not isinstance(check, dict) or check.get("threshold") != threshold
            or check.get("minimum_count") != row["count"]
            or type(check.get("passed_continuously")) is not bool
            or check["passed_continuously"] != (row["count"] >= threshold)
            or int(row["sequence"]) > pin["sequence"] or int(row["tick"]) > pin["tick"]):
            raise MergeError(f"Prefix structural evidence for {metric} is inconsistent.")
        prefix_structural[metric] = {"minimum": row, "threshold": threshold}

    if (institutions.get("interval_scope") != "checkpoint_prefix"
        or institutions.get("pin_matches_last_committed_sample") is not True
        or _integer(institutions.get("audited_from_sequence"), "Prefix institution start sequence") != 0
        or _integer(institutions.get("audited_from_tick"), "Prefix institution start tick") != root_tick
        or _integer(institutions.get("audited_through_sequence"), "Prefix institution end sequence") != pin["sequence"]
        or _integer(institutions.get("audited_through_tick"), "Prefix institution end tick") != pin["tick"]):
        raise MergeError("The prefix organization aggregate does not cover genesis through its pinned checkpoint.")
    organizations = institutions.get("organizations")
    if not isinstance(organizations, list):
        raise MergeError("The prefix organization aggregate is missing its organization list.")
    seen = set()
    for row in organizations:
        if (not isinstance(row, dict) or not row.get("organization_id")
            or row["organization_id"] in seen or not isinstance(row.get("organization_name"), str)
            or type(row.get("open_at_pin")) is not bool):
            raise MergeError("The prefix organization aggregate contains invalid or duplicate data.")
        seen.add(row["organization_id"])
    return {
        "world_id": scope["world_id"], "branch_id": scope["branch_id"], "root_hash": root_hash,
        "root": {"sequence": 0, "tick": root_tick, "year": 0, "state_version": root_version, "state_hash": root_hash},
        "pin": pin, "calendar": {"day_seconds": day_seconds, "year_days": year_days},
        "boundary_count": counts[1], "ordinary_minimum": prefix_minimum,
        "first_below": first_below, "structural": prefix_structural,
        "institutions": institutions,
    }


def _validate_suffix_scope(report, prefix_info):
    scope = report.get("scope", {})
    if (scope.get("world_id") != prefix_info["world_id"]
        or scope.get("branch_id") != prefix_info["branch_id"]
        or scope.get("verified_root_genesis_hash") != prefix_info["root_hash"]):
        raise MergeError("The prefix and suffix must use the same world, branch and verified root-genesis hash.")
    calendar = report["interval"].get("calendar_basis", {})
    if (_integer(calendar.get("day_seconds"), "Suffix calendar day seconds") != prefix_info["calendar"]["day_seconds"]
        or _integer(calendar.get("year_days"), "Suffix calendar year days") != prefix_info["calendar"]["year_days"]):
        raise MergeError("The prefix and suffix calendar bases differ.")
    institutional_flag = scope.get("institutional_metrics_audited")
    institutions = report.get("institutions")
    if institutional_flag is not None and type(institutional_flag) is not bool:
        raise MergeError("Suffix institutional coverage flag must be boolean.")
    if institutional_flag is True and (scope.get("population_only") is not False or not isinstance(institutions, dict)):
        raise MergeError("Suffix institutional coverage is claimed without complete interval evidence.")
    if institutions is None:
        return None
    if not isinstance(institutions, dict) or institutions.get("pin_matches_last_committed_sample") is not True:
        raise MergeError("Suffix organization coverage does not verify its saved interval pins.")
    start, end, observed = _check_interval(report)
    if (_integer(institutions.get("audited_from_sequence"), "Suffix institution start sequence") != int(start["sequence"])
        or _integer(institutions.get("audited_from_tick"), "Suffix institution start tick") != int(start["tick"])
        or _integer(institutions.get("audited_through_sequence"), "Suffix institution end sequence") != int(end["sequence"])
        or _integer(institutions.get("audited_through_tick"), "Suffix institution end tick") != int(end["tick"])):
        raise MergeError("Suffix organization coverage does not match its population interval endpoints.")
    censoring = institutions.get("censoring")
    if not isinstance(censoring, dict) or censoring.get("observed_commit_boundary_count") != observed:
        raise MergeError("Suffix organization coverage does not match its commit-boundary count.")
    organization_rows = institutions.get("organizations")
    if not isinstance(organization_rows, list):
        raise MergeError("Suffix organization coverage is missing per-organization rows.")
    seen = set()
    for row in organization_rows:
        if (not isinstance(row, dict) or not row.get("organization_id") or row["organization_id"] in seen
            or type(row.get("left_censored_at_interval_start")) is not bool
            or not isinstance(row.get("gap_windows"), list)):
            raise MergeError("Suffix organization coverage contains invalid or duplicate data.")
        seen.add(row["organization_id"])
    structural = institutions.get("structural_capacity")
    if structural is not None:
        if not isinstance(structural, dict) or structural.get("observed_commit_boundary_count") != observed:
            raise MergeError("Suffix structural coverage does not match its commit-boundary count.")
        for metric in _STRUCTURAL_METRICS:
            threshold = _integer(structural.get("thresholds", {}).get(metric), f"Suffix {metric} threshold")
            row = _row(structural.get("minimum_counts", {}).get(metric), f"Suffix {metric} minimum")
            check = structural.get("continuous_checks_over_replayed_interval", {}).get(metric)
            if (not isinstance(check, dict) or check.get("threshold") != threshold
                or check.get("minimum_count") != row["count"]
                or type(check.get("passed_continuously")) is not bool
                or check["passed_continuously"] != (row["count"] >= threshold)
                or not int(start["sequence"]) <= int(row["sequence"]) <= int(end["sequence"])
                or not int(start["tick"]) <= int(row["tick"]) <= int(end["tick"])):
                raise MergeError(f"Suffix structural evidence for {metric} is inconsistent.")
    return institutions


def _join_structural(prefix_info, reports):
    suffix_parts = []
    for report in reports:
        institutions = report.get("institutions")
        structural = institutions.get("structural_capacity") if isinstance(institutions, dict) else None
        if not isinstance(structural, dict):
            return {
                "qualified": False, "suffix_coverage_complete": False,
                "prefix_minimum_counts": {m: v["minimum"] for m, v in prefix_info["structural"].items()},
                "suffix_minimum_counts": None, "minimum_counts": None, "thresholds": None,
                "continuous_checks_over_replayed_history": None,
                "thresholds_passed_over_replayed_history": None,
            }
        suffix_parts.append(structural)
    suffix_minima, combined, thresholds = {}, {}, {}
    checks = {}
    for metric in _STRUCTURAL_METRICS:
        prefix_metric = prefix_info["structural"][metric]
        threshold = prefix_metric["threshold"]
        suffix_minimum = None
        for part in suffix_parts:
            suffix_threshold = _integer(part["thresholds"][metric], f"Suffix {metric} threshold")
            if suffix_threshold != threshold:
                raise MergeError(f"Prefix and suffix structural thresholds differ for {metric}.")
            row = _row(part["minimum_counts"][metric], f"Suffix {metric} minimum")
            suffix_minimum = row if suffix_minimum is None else _earlier_minimum(suffix_minimum, row)
        suffix_minima[metric] = suffix_minimum
        combined[metric] = _earlier_minimum(prefix_metric["minimum"], suffix_minimum)
        thresholds[metric] = threshold
        checks[metric] = {
            "minimum_count": combined[metric]["count"],
            "threshold": threshold,
            "passed_continuously": combined[metric]["count"] >= threshold,
        }
    return {
        "qualified": True, "suffix_coverage_complete": True,
        "prefix_minimum_counts": {m: v["minimum"] for m, v in prefix_info["structural"].items()},
        "suffix_minimum_counts": suffix_minima, "minimum_counts": combined,
        "thresholds": thresholds, "continuous_checks_over_replayed_history": checks,
        "thresholds_passed_over_replayed_history": all(row["passed_continuously"] for row in checks.values()),
    }


def _organization_coverage(prefix_info, reports, suffix_summary):
    prefix_rows = {row["organization_id"]: row for row in prefix_info["institutions"]["organizations"]}
    parts = [report.get("institutions") for report in reports]
    complete = all(isinstance(part, dict) for part in parts)
    first_rows = {}
    if complete:
        first_rows = {row["organization_id"]: row for row in parts[0]["organizations"]}
    comparisons = []
    if complete:
        for ident in sorted(prefix_rows.keys() & first_rows.keys()):
            matches = prefix_rows[ident]["open_at_pin"] == first_rows[ident]["left_censored_at_interval_start"]
            comparisons.append({
                "organization_id": ident,
                "organization_name": prefix_rows[ident]["organization_name"],
                "prefix_open_at_pin": prefix_rows[ident]["open_at_pin"],
                "suffix_left_censored_at_start": first_rows[ident]["left_censored_at_interval_start"],
                "matches": matches,
            })
        if any(not row["matches"] for row in comparisons):
            raise MergeError("Prefix and suffix organization membership states differ at the shared pin.")
    suffix_gaps = suffix_summary.get("organization_membership_gaps")
    return {
        "prefix_aggregate": {
            "scope": "root-genesis replay through prefix pin; full windows are unavailable",
            "audited_through_sequence": prefix_info["institutions"]["audited_through_sequence"],
            "audited_through_tick": prefix_info["institutions"]["audited_through_tick"],
            "organizations": prefix_info["institutions"]["organizations"],
        },
        "suffix_windows": {
            "scope": "suffix interval chain only",
            "coverage_available": complete and suffix_gaps is not None,
            "missing_interval_indexes": [i for i, part in enumerate(parts) if not isinstance(part, dict)],
            "observed_organization_count": suffix_gaps["observed_organization_count"] if suffix_gaps else None,
            "organizations_with_gaps": suffix_gaps["organizations_with_gaps"] if suffix_gaps else None,
        },
        "shared_pin_open_state_comparison": {
            "available": complete,
            "compared_organization_count": len(comparisons),
            "per_organization": comparisons,
            "all_compared_states_match": all(row["matches"] for row in comparisons) if complete else None,
        },
    }


def merge_prefix_and_intervals(prefix, reports):
    """Join a verified root-genesis prefix to one or more adjacent suffix reports."""
    if not reports:
        raise MergeError("A verified suffix interval is required with --genesis-prefix.")
    prefix_info = _validate_prefix(prefix)
    ordered = sorted(reports, key=lambda report: int(report["interval"]["start_checkpoint"]["sequence"]))
    for report in ordered:
        _validate_suffix_scope(report, prefix_info)
    suffix = merge_intervals(ordered)
    pin = prefix_info["pin"]
    start = suffix["start_checkpoint"]
    if (any(_integer(start[field], f"First suffix {field}") != pin[field]
            for field in ("sequence", "tick", "state_version"))
        or start.get("state_hash") != pin["state_hash"]
        or _integer(start.get("year"), "First suffix year") != pin["year"]):
        raise MergeError("The first suffix interval must start at the prefix's exact saved pin, including year and hash.")
    if (suffix["world_id"] != prefix_info["world_id"] or suffix["branch_id"] != prefix_info["branch_id"]
        or suffix["verified_root_genesis_hash"] != prefix_info["root_hash"]):
        raise MergeError("The prefix and suffix identities do not match.")
    calendar = suffix["calendar_basis"]
    if (_integer(calendar["day_seconds"], "Suffix calendar day seconds") != prefix_info["calendar"]["day_seconds"]
        or _integer(calendar["year_days"], "Suffix calendar year days") != prefix_info["calendar"]["year_days"]):
        raise MergeError("The prefix and suffix calendar bases differ.")

    ordinary_suffix_minimum = suffix["minimum_counts"][ORDINARY_COUNT]
    ordinary_minimum = _earlier_minimum(prefix_info["ordinary_minimum"], ordinary_suffix_minimum)
    first_below = prefix_info["first_below"]
    first_below_source = "genesis_prefix" if first_below is not None else None
    episodes = suffix["ordinary_population_shortfalls"]["episodes"]
    if first_below is None and episodes:
        if episodes[0]["left_censored"]:
            raise MergeError("A suffix shortfall is left-censored even though the prefix never fell below threshold.")
        first_below = {
            "sequence": episodes[0]["start_sequence"], "tick": episodes[0]["start_tick"],
            "count_at_first_below": None, "episode_minimum_count": episodes[0]["minimum_count"],
        }
        first_below_source = "suffix_interval_chain"
    elif first_below is not None:
        first_below_source = "genesis_prefix"
    suffix_count = _integer(suffix["observed_distinct_commit_boundary_count"], "Suffix boundary count")
    total_count = prefix_info["boundary_count"] + suffix_count - 1
    end_version = _integer(suffix["end_checkpoint"]["state_version"], "Suffix end state version")
    if total_count != end_version - prefix_info["root"]["state_version"] + 1:
        raise MergeError("The joined distinct-boundary count does not match the root-to-end state-version span.")

    return {
        "audit": "capacity_genesis_prefix_interval_chain", "schema": 1,
        "source": "Joined verified root-genesis checkpoint-prefix and suffix interval reports; no new replay was performed.",
        "world_id": prefix_info["world_id"], "branch_id": prefix_info["branch_id"],
        "verified_root_genesis_hash": prefix_info["root_hash"], "calendar_basis": prefix_info["calendar"],
        "start_checkpoint": prefix_info["root"], "prefix_checkpoint": pin,
        "end_checkpoint": suffix["end_checkpoint"],
        "prefix_distinct_commit_boundary_count": prefix_info["boundary_count"],
        "suffix_distinct_commit_boundary_count": suffix_count,
        "shared_prefix_suffix_pin_count": 1,
        "observed_distinct_commit_boundary_count": total_count,
        "ordinary_population": {
            "threshold": RESIDENT_THRESHOLD, "minimum_count": ordinary_minimum,
            "first_below_240": first_below, "first_below_source": first_below_source,
            "suffix_shortfall_episodes": episodes,
        },
        "structural_capacity": _join_structural(prefix_info, ordered),
        "organization_membership_coverage": _organization_coverage(prefix_info, ordered, suffix),
        "suffix_chain": {
            "interval_count": len(ordered), "start_checkpoint": suffix["start_checkpoint"],
            "end_checkpoint": suffix["end_checkpoint"],
            "ordinary_population_shortfalls": suffix["ordinary_population_shortfalls"],
        },
        "population_600_year_passed": False,
        "durable_job_completion_verified": False,
        "claim_note": "A report join does not establish durable job completion or an overall 600-year pass.",
    }


def verify_terminal_data_directory(
    data_directory,
    end_checkpoint,
    *,
    expected_world_id,
    expected_branch_id,
    expected_root_genesis_hash,
    expected_calendar,
):
    """Verify a merged endpoint against a saved probe receipt and read-only SQLite.

    This checks only terminal evidence. It does not replay historical commits;
    the prefix and interval reports remain responsible for their stated history.
    """
    audit = _audit_helpers()
    try:
        directory = Path(data_directory).expanduser().resolve(strict=True)
        if not directory.is_dir():
            raise MergeError("The terminal data directory is not a directory.")
        end = {
            "sequence": _integer(end_checkpoint.get("sequence"), "Terminal sequence"),
            "tick": _integer(end_checkpoint.get("tick"), "Terminal tick"),
            "year": _integer(end_checkpoint.get("year"), "Terminal year"),
            "state_version": _integer(end_checkpoint.get("state_version"), "Terminal state version"),
            "state_hash": end_checkpoint.get("state_hash"),
        }
        if (end["sequence"] <= 0 or end["tick"] < 0 or end["year"] <= 0 or end["year"] % 10
            or end["state_version"] <= 0 or not isinstance(end["state_hash"], str) or not end["state_hash"]):
            raise MergeError("The merged endpoint is not a valid exact decade pin.")
        expected_world_id = audit.UUID(str(expected_world_id))
        expected_branch_id = audit.UUID(str(expected_branch_id))
        expected_calendar = {
            "day_seconds": _integer(expected_calendar.get("day_seconds"), "Expected calendar day seconds"),
            "year_days": _integer(expected_calendar.get("year_days"), "Expected calendar year days"),
        }
        manifest_path = directory / audit.MANIFEST_NAME
        manifest_bytes = manifest_path.read_bytes()
        manifest, scope, world_id, branch_id, job_id, target_tick = audit._load_inputs(directory)
        if manifest_path.read_bytes() != manifest_bytes:
            raise MergeError("The saved manifest changed during terminal verification.")
        if (world_id != expected_world_id or branch_id != expected_branch_id
            or manifest.get("database_file", audit.DATABASE_NAME) != audit.DATABASE_NAME):
            raise MergeError("The saved probe world and branch do not match the merged chain.")
        if (str(manifest.get("start_tick")) != str(_integer(manifest.get("start_tick"), "Manifest start tick"))
            or str(manifest.get("target_tick")) != str(target_tick)
            or target_tick != end["tick"]):
            raise MergeError("The saved probe target does not match the merged endpoint.")

        engine = audit._readonly_engine(directory / audit.DATABASE_NAME)
        try:
            repository = audit.WorldRepository(engine)
            view = repository.read_world(scope, world_id, branch_id)
            if (view.world.owner_id != scope.user_id or view.world.tenant_id != scope.tenant_id
                or view.world.world_id != world_id or view.branch.branch_id != branch_id):
                raise MergeError("The saved identity does not own the selected probe world branch.")

            job = repository.read_job(scope, job_id)
            if (job.job_id != job_id or job.tenant_id != scope.tenant_id
                or (job.world_id, job.branch_id) != (world_id, branch_id)
                or job.job_type != "simulation.world_advance"):
                raise MergeError("The saved durable job does not belong to the selected world branch.")
            plan = job.payload.get("plan") if isinstance(job.payload, dict) else None
            if not isinstance(plan, dict) or _integer(plan.get("target_tick"), "Durable job target") != target_tick:
                raise MergeError("The durable job target differs from the saved probe target.")
            if (job.status != audit.JobStatus.SUCCEEDED
                or job.committed_through_tick != target_tick):
                raise MergeError("The durable world-advance job is not succeeded at its exact target.")

            root_genesis, genesis_metadata = audit._root_genesis(engine, world_id, branch_id)
            if (root_genesis["state_hash"] != expected_root_genesis_hash
                or root_genesis["current_tick"] != "0"
                or root_genesis["calendar"] != expected_calendar):
                raise MergeError("The actual root genesis hash or calendar differs from the merged prefix.")
            config = manifest.get("config")
            if (not isinstance(config, dict)
                or _integer(config.get("day_seconds"), "Manifest calendar day seconds") != expected_calendar["day_seconds"]
                or _integer(config.get("year_days"), "Manifest calendar year days") != expected_calendar["year_days"]
                or _integer(manifest.get("start_tick"), "Manifest start tick") != 0):
                raise MergeError("The saved manifest calendar or genesis-relative start does not match the root.")

            checkpoint = audit._manifest_checkpoint(
                manifest, end["year"], start_tick=0, target_tick=target_tick,
                day_seconds=expected_calendar["day_seconds"], year_days=expected_calendar["year_days"],
            )
            if any(checkpoint[key] != end[key] for key in ("sequence", "tick", "year", "state_version", "state_hash")):
                raise MergeError("The exact saved decade pin does not match the merged endpoint.")

            raw_head = None
            with engine.connect() as connection:
                raw_head = connection.execute(
                    audit.text(
                        "SELECT head_seq, state_version, current_tick, state_hash FROM world_branches "
                        "WHERE world_id = :world_id AND branch_id = :branch_id"
                    ),
                    {"world_id": world_id.hex, "branch_id": branch_id.hex},
                ).mappings().one_or_none()
            if raw_head is None:
                raise MergeError("The selected probe branch has no stored database head.")
            database_head = {
                "sequence": int(raw_head["head_seq"]), "tick": int(raw_head["current_tick"]),
                "state_version": int(raw_head["state_version"]), "state_hash": raw_head["state_hash"],
            }
            if any(database_head[key] != end[key] for key in database_head):
                raise MergeError("The actual database head does not match the merged endpoint.")
            if (view.last_seq != database_head["sequence"]
                or view.state.current_tick != database_head["tick"]
                or view.branch.state_version != database_head["state_version"]
                or view.state_hash != database_head["state_hash"]):
                raise MergeError("The repository view and stored database head disagree.")
            if not audit._verify_stored_checkpoint_boundary(engine, world_id, branch_id, checkpoint):
                raise MergeError("The terminal sequence lacks an exact committed event and matching saved snapshot witness.")

            root_branch_id = audit.UUID(root_genesis["branch_id"])
            with engine.connect() as connection:
                root_payload = connection.execute(
                    audit.text(
                        "SELECT initial_state_json FROM world_branches "
                        "WHERE world_id = :world_id AND branch_id = :branch_id"
                    ),
                    {"world_id": world_id.hex, "branch_id": root_branch_id.hex},
                ).mappings().one_or_none()
            if root_payload is None:
                raise MergeError("The root genesis state is missing from the saved database.")
            initial_payload = root_payload["initial_state_json"]
            if isinstance(initial_payload, str):
                initial_payload = json.loads(initial_payload)
            root_state = audit.WorldState.model_validate(initial_payload)
            if audit.state_hash(root_state) != root_genesis["state_hash"]:
                raise MergeError("The stored root genesis state changed during terminal verification.")
            root_index = genesis_metadata.get("content_index")
            root_index = root_index if isinstance(root_index, dict) else {}
            raw_key_ids = root_index.get("key_npc_ids")
            key_ids = audit._uuid_set(raw_key_ids)
            key_profile = audit._initial_key_profile_threshold(
                key_ids,
                audit._sample_counts(root_state, key_ids)["active_authored_key_npcs"],
                key_index_valid=audit._is_uuid_index(raw_key_ids),
            )

            # The read helpers use separate read-only connections. Recheck the
            # mutable head, job receipt, and manifest after boundary validation
            # so an advancing run cannot produce a mixed-time proof.
            with engine.connect() as connection:
                final_head = connection.execute(
                    audit.text(
                        "SELECT head_seq, state_version, current_tick, state_hash FROM world_branches "
                        "WHERE world_id = :world_id AND branch_id = :branch_id"
                    ),
                    {"world_id": world_id.hex, "branch_id": branch_id.hex},
                ).mappings().one_or_none()
            latest_job = repository.read_job(scope, job_id)
            if (final_head is None
                or tuple(final_head[key] for key in ("head_seq", "state_version", "current_tick", "state_hash"))
                    != tuple(raw_head[key] for key in ("head_seq", "state_version", "current_tick", "state_hash"))
                or latest_job.status != job.status
                or latest_job.committed_through_tick != job.committed_through_tick
                or latest_job.payload != job.payload
                or manifest_path.read_bytes() != manifest_bytes):
                raise MergeError("The saved job, manifest, or branch head changed during terminal verification.")
        finally:
            engine.dispose()

        consolidated_receipt_matches = (
            manifest.get("status") == audit.JobStatus.SUCCEEDED.value
            and manifest.get("last_job_status") == audit.JobStatus.SUCCEEDED.value
            and manifest.get("last_tick") == str(end["tick"])
            and manifest.get("last_state_version") == str(end["state_version"])
            and manifest.get("last_state_hash") == end["state_hash"]
        )
        target_rows = [
            row for row in manifest.get("checkpoints", [])
            if isinstance(row, dict) and row.get("label") == "target"
        ] if isinstance(manifest.get("checkpoints"), list) else []
        target_receipt_matches = (
            len(target_rows) == 1
            and target_rows[0].get("scheduled_year") is None
            and target_rows[0].get("sequence") == str(end["sequence"])
            and target_rows[0].get("tick") == str(end["tick"])
            and target_rows[0].get("state_version") == str(end["state_version"])
            and target_rows[0].get("state_hash") == end["state_hash"]
        )
        if consolidated_receipt_matches:
            manifest_receipt_form = "consolidated_final_receipt"
        elif manifest.get("status") == "running" and target_receipt_matches:
            manifest_receipt_form = "persisted_target_checkpoint_during_final_verification"
        elif (manifest.get("status") == audit.JobStatus.SUCCEEDED.value
            and manifest.get("last_job_status") in {"running", "paused"}
            and manifest.get("last_tick") == str(end["tick"])
            and target_receipt_matches):
            manifest_receipt_form = "persisted_target_checkpoint_before_consolidated_job_receipt"
        elif manifest.get("status") == "running":
            raise MergeError("The running manifest has no unique exact target checkpoint for the merged endpoint.")
        else:
            raise MergeError("The saved manifest final receipt does not match the succeeded database head.")

        return {
            "verified": True,
            "terminal_only": True,
            "historical_replay_performed": False,
            "source": "Read-only SQLite job, branch head, root genesis and committed-boundary evidence plus the saved probe manifest.",
            "data_directory": str(directory),
            "world_id": str(world_id), "branch_id": str(branch_id), "job_id": str(job_id),
            "job_type": job.job_type, "job_status": job.status.value,
            "job_started_tick": plan.get("started_tick"),
            "job_planned_target_tick": str(target_tick),
            "job_committed_through_tick": str(job.committed_through_tick),
            "database_head": {key: str(value) for key, value in database_head.items()},
            "manifest_status": manifest.get("status"),
            "manifest_receipt_form": manifest_receipt_form,
            "manifest_consolidated_fields_match_head": consolidated_receipt_matches,
            "manifest_target_checkpoint_matches_head": target_receipt_matches,
            "manifest_final_receipt_matches_head": consolidated_receipt_matches,
            "exact_decade_pin_matches_endpoint": True,
            "stored_commit_event_and_available_snapshots_match": True,
            "root_genesis_hash": root_genesis["state_hash"],
            "root_calendar": root_genesis["calendar"],
            "original_key_profile_at_genesis": key_profile,
            "sqlite_open_mode": "uri_mode_ro_with_query_only",
            "database_or_manifest_mutated": False,
            "model_calls": 0,
            "manifest_sha256": sha256(manifest_bytes).hexdigest(),
        }
    except MergeError:
        raise
    except audit.AuditError as error:
        raise MergeError(f"Terminal data verification failed: {error}") from error
    except Exception as error:
        raise MergeError(f"Terminal data verification failed: {error}") from error


def _attach_terminal_data_proof(summary, prefix, data_directory):
    prefix_info = _validate_prefix(prefix)
    proof = verify_terminal_data_directory(
        data_directory,
        summary["end_checkpoint"],
        expected_world_id=prefix_info["world_id"],
        expected_branch_id=prefix_info["branch_id"],
        expected_root_genesis_hash=prefix_info["root_hash"],
        expected_calendar=prefix_info["calendar"],
    )
    summary["terminal_data_proof"] = proof
    summary["durable_job_completion_verified"] = proof["verified"]
    elapsed_ticks = int(summary["end_checkpoint"]["tick"]) - int(summary["start_checkpoint"]["tick"])
    expected_600_ticks = 600 * prefix_info["calendar"]["day_seconds"] * prefix_info["calendar"]["year_days"]
    ordinary_minimum = summary["ordinary_population"]["minimum_count"]["count"]
    structural = summary["structural_capacity"]
    summary["population_600_year_passed"] = (
        proof["verified"]
        and elapsed_ticks == expected_600_ticks
        and int(summary["end_checkpoint"]["year"]) == 600
        and ordinary_minimum >= RESIDENT_THRESHOLD
        and structural.get("qualified") is True
        and structural.get("thresholds_passed_over_replayed_history") is True
        and proof["original_key_profile_at_genesis"].get("passed_at_genesis") is True
    )
    summary["population_600_year_pass_scope"] = (
        "Requires terminal read-only job and exact-decade pin proof, exactly 600 years from root genesis, "
        "ordinary active named resident minimum of at least 240, all structural thresholds passed, and "
        "at least 32 unique authored key identities present as active named profiles at genesis."
    )
    summary["claim_note"] = (
        "Terminal completion is independently verified from read-only saved data; the 600-year population "
        "pass also requires the joined historical population, structural and root key-profile thresholds."
    )
    return summary

def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("reports", nargs="+", type=Path)
    parser.add_argument("--output", type=Path)
    parser.add_argument("--genesis-prefix", type=Path,
                        help="Verified audit-world-capacity checkpoint-year report to prepend.")
    parser.add_argument("--verify-terminal-data-directory", type=Path,
                        help="Read-only probe directory used to verify the joined terminal checkpoint.")
    args = parser.parse_args()
    if args.genesis_prefix is None and len(args.reports) < 2:
        parser.error("At least two input reports are required to merge intervals.")
    if args.genesis_prefix is not None and not args.reports:
        parser.error("At least one suffix interval is required with --genesis-prefix.")
    if args.verify_terminal_data_directory is not None and args.genesis_prefix is None:
        parser.error("--verify-terminal-data-directory requires --genesis-prefix.")
    try:
        inputs = [(path, path.read_bytes()) for path in args.reports]
        suffix_reports = [json.loads(raw) for _, raw in inputs]
        prefix_input = None
        if args.genesis_prefix is None:
            summary = merge_intervals(suffix_reports)
        else:
            prefix_raw = args.genesis_prefix.read_bytes()
            prefix_input = (args.genesis_prefix, prefix_raw)
            summary = merge_prefix_and_intervals(json.loads(prefix_raw), suffix_reports)
            summary["genesis_prefix_report"] = {
                "path": str(args.genesis_prefix), "sha256": sha256(prefix_raw).hexdigest(),
            }
            if args.verify_terminal_data_directory is not None:
                _attach_terminal_data_proof(
                    summary, json.loads(prefix_raw), args.verify_terminal_data_directory,
                )
        summary["input_reports"] = [{"path": str(path), "sha256": sha256(raw).hexdigest()} for path, raw in inputs]
        serialized = json.dumps(summary, ensure_ascii=False, indent=2) + "\n"
        if args.output:
            protected = {path.resolve() for path, _ in inputs}
            if prefix_input is not None:
                protected.add(prefix_input[0].resolve())
            if args.output.resolve() in protected:
                raise MergeError("The output must not overwrite an input report or the genesis prefix.")
            output_path = args.output
            if args.verify_terminal_data_directory is not None:
                audit = _audit_helpers()
                protected_sources = [path for path, _ in inputs]
                if prefix_input is not None:
                    protected_sources.append(prefix_input[0])
                for source_path in protected_sources:
                    try:
                        if (output_path.exists() and source_path.exists()
                            and output_path.samefile(source_path)):
                            raise MergeError("The output must not overwrite an input report or the genesis prefix.")
                    except OSError:
                        pass
                try:
                    output_path = audit._safe_report_output_path(
                        output_path, args.verify_terminal_data_directory.expanduser().resolve(strict=True),
                    )
                except audit.AuditError as error:
                    raise MergeError(str(error)) from error
            output_path.write_text(serialized, encoding="utf-8")
        else:
            print(serialized, end="")
    except (OSError, KeyError, TypeError, ValueError) as error:
        parser.exit(2, f"capacity interval chain: {error}\n")


if __name__ == "__main__":
    main()

