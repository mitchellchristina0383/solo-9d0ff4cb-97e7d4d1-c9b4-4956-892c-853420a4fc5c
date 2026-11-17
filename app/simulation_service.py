"""Downtime impact simulation ("what-if" analysis).

Given a service planned for maintenance, a set of key entry services and the
declared backup-upstream groups, compute entry availability *before* and
*after* the planned stop from one committed graph snapshot plus the latest
reported states.

Design notes
------------
* The whole calculation runs inside one read transaction, so the graph
  (nodes/edges/revision) and the reported service states always belong to the
  same committed snapshot.  Concurrent graph swaps or reports can never
  produce a mixed-revision result.
* Nothing here writes to the database: alarms, reports and the audit log are
  untouched.
* Availability propagates transitively along dependency edges: a service is
  available only when all of its ungrouped direct dependencies are available
  and every backup group still has at least one available member.  Faulty or
  never-reported nodes are unavailable, and so is every node whose upstream
  chain ultimately reaches such a node (including a backup member's own
  upstream failures).
"""
from __future__ import annotations

from .database import read_tx
from .errors import SimulationError
from .graph_service import _load_graph
from .schemas import BackupGroupIn, DowntimeSimulationPayload


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _validate_payload(
    payload: DowntimeSimulationPayload,
    nodes: set[str],
    upstream: dict[str, list[str]],
) -> dict[str, list[list[str]]]:
    """Validate every reference against the current committed graph.

    Returns ``groups`` mapping ``service -> list[group]`` where each group is
    a de-duplicated, order-preserving member list.  Raises
    :class:`SimulationError` (HTTP 422) on any invalid reference.
    """
    if payload.stopped_service not in nodes:
        raise SimulationError(
            f"stopped_service {payload.stopped_service!r} is not part of the "
            f"current graph"
        )

    unknown_entries = sorted({e for e in payload.entries if e not in nodes})
    if unknown_entries:
        raise SimulationError(
            f"entry services not in the current graph: {unknown_entries}"
        )

    seen_entries: set[str] = set()
    dup_entries: set[str] = set()
    for entry in payload.entries:
        if entry in seen_entries:
            dup_entries.add(entry)
        seen_entries.add(entry)
    if dup_entries:
        raise SimulationError(f"duplicate entry services: {sorted(dup_entries)}")

    groups: dict[str, list[list[str]]] = {}
    for bg in payload.backup_groups:
        if bg.service not in nodes:
            raise SimulationError(
                f"backup group targets unknown service {bg.service!r}"
            )
        if len(bg.members) != len(set(bg.members)):
            raise SimulationError(
                f"duplicate members in backup group of service {bg.service!r}: "
                f"{bg.members}"
            )
        direct = set(upstream[bg.service])
        not_direct = sorted(m for m in bg.members if m not in direct)
        if not_direct:
            raise SimulationError(
                f"backup group members {not_direct} of service {bg.service!r} "
                f"must be its direct dependencies in the current graph"
            )
        groups.setdefault(bg.service, []).append(list(bg.members))

    return groups


# ---------------------------------------------------------------------------
# Availability
# ---------------------------------------------------------------------------


def _compute_availability(
    upstream: dict[str, list[str]],
    available_base: set[str],
    groups: dict[str, list[list[str]]],
    forced_unavailable: set[str],
) -> set[str]:
    """Return the set of available nodes.

    A node is available when:

    * it is intrinsically available (reported healthy) and not forced down;
    * every ungrouped direct dependency is available;
    * every declared backup group keeps at least one available member.

    The graph is a committed DAG, so memoized recursion terminates.
    """
    memo: dict[str, bool] = {}

    def is_available(node: str) -> bool:
        if node in memo:
            return memo[node]
        if node in forced_unavailable or node not in available_base:
            memo[node] = False
            return False

        grouped: set[str] = set()
        for group in groups.get(node, ()):
            grouped.update(group)
            if not any(is_available(m) for m in group):
                memo[node] = False
                return False

        for dep in upstream.get(node, ()):
            if dep in grouped:
                continue
            if not is_available(dep):
                memo[node] = False
                return False

        memo[node] = True
        return True

    return {node for node in upstream if is_available(node)}


def _interruption_reasons(
    node: str,
    stopped: str,
    upstream: dict[str, list[str]],
    available_after: set[str],
    groups: dict[str, list[list[str]]],
    memo: dict[str, list[dict]],
) -> list[dict]:
    """Explain why ``node`` is unavailable in the after-stop scenario.

    Returns one reason per unsatisfied direct requirement (the stopped
    service itself yields a single ``self_stopped`` reason).  Recursing into
    a failed dependency pins the deepest failed requirement, which lets the
    answer distinguish "the backup group lost every member" from a plain
    ungrouped dependency failure -- and counts failures of a backup member's
    own upstreams as well, because group membership availability is itself
    transitive.
    """
    if node == stopped:
        return [
            {
                "type": "self_stopped",
                "detail": f"service {node!r} is the service being stopped",
                "dependency": None,
                "group_members": None,
            }
        ]
    if node in memo:
        return memo[node]
    reasons: list[dict] = []
    grouped: set[str] = set()
    for group in groups.get(node, ()):
        grouped.update(group)
        if not any(m in available_after for m in group):
            reasons.append(
                {
                    "type": "group_lost_available_member",
                    "detail": (
                        f"backup upstream group {group} of service {node!r} has "
                        f"no available member"
                    ),
                    "dependency": stopped if stopped in group else None,
                    "group_members": list(group),
                }
            )

    for dep in upstream.get(node, ()):
        if dep in grouped:
            continue
        if dep not in available_after:
            if dep == stopped:
                reasons.append(
                    {
                        "type": "dependency_unavailable",
                        "detail": (
                            f"direct dependency {dep!r} is the service being stopped"
                        ),
                        "dependency": dep,
                        "group_members": None,
                    }
                )
            else:
                child_reasons = _interruption_reasons(
                    dep, stopped, upstream, available_after, groups, memo
                )
                if child_reasons:
                    # Reuse the deepest, most precise explanation but keep the
                    # direct dependency identity for the caller.
                    reasons.append(
                        {
                            **child_reasons[0],
                            "dependency": dep,
                        }
                    )
                else:
                    reasons.append(
                        {
                            "type": "dependency_unavailable",
                            "detail": f"direct dependency {dep!r} is unavailable",
                            "dependency": dep,
                            "group_members": None,
                        }
                    )

    memo[node] = reasons
    return reasons


# ---------------------------------------------------------------------------
# Simulation entry point
# ---------------------------------------------------------------------------


def simulate_downtime(payload: DowntimeSimulationPayload) -> dict:
    """Compute entry availability before/after the planned downtime."""
    with read_tx() as conn:
        revision, version, upstream = _load_graph(conn)
        # Latest reported states inside the same snapshot transaction.
        up_rows = conn.execute(
            "SELECT service FROM service_state WHERE status = 'up'"
        ).fetchall()
        reported_healthy = {r["service"] for r in up_rows}

    nodes = set(upstream)
    groups = _validate_payload(payload, nodes, upstream)

    # Intrinsically available: reported up AND present in this graph.  Nodes
    # reported down or never reported are unavailable.
    available_base = {s for s in reported_healthy if s in nodes}

    before = _compute_availability(upstream, available_base, groups, set())
    after = _compute_availability(
        upstream, available_base, groups, {payload.stopped_service}
    )

    already_unavailable = [e for e in payload.entries if e not in before]

    new_interruptions: list[dict] = []
    reason_memo: dict[str, list[dict]] = {}
    for entry in payload.entries:
        if entry in before and entry not in after:
            reasons = _interruption_reasons(
                entry,
                payload.stopped_service,
                upstream,
                after,
                groups,
                reason_memo,
            )
            new_interruptions.append({"entry": entry, "reasons": reasons})

    after_unavailable = [e for e in payload.entries if e not in after]

    return {
        "graph_revision": revision,
        "graph_version": version,
        "stopped_service": payload.stopped_service,
        "already_unavailable_entries": already_unavailable,
        "newly_interrupted_entries": new_interruptions,
        "after_unavailable_entries": after_unavailable,
    }
