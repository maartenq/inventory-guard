"""Core comparison logic for Ansible inventories."""

from __future__ import annotations

import argparse
import json
import logging
import re
from collections.abc import Mapping
from copy import deepcopy
from dataclasses import dataclass
from typing import Any

from ruamel.yaml import YAML  # type: ignore
from ruamel.yaml.nodes import ScalarNode

logger = logging.getLogger(__name__)

# Setup YAML parser with Ansible vault support
yaml = YAML(typ="safe")


def _vault_constructor(loader, node):
    """Allow Ansible !vault tags as plain strings."""
    if isinstance(node, ScalarNode):
        return loader.construct_scalar(node)
    return loader.construct_object(node)


yaml.constructor.add_constructor("!vault", _vault_constructor)


# ---------- Types ----------
VarsMap = dict[str, Any]
HostVars = dict[str, VarsMap]
YAMLNode = Mapping[str, Any]


@dataclass(slots=True)
class Limits:
    """Configuration limits for change thresholds."""

    max_host_change_pct: float
    max_var_change_pct: float
    max_host_change_abs: int
    max_var_change_abs: int
    ignored_key_regex: list[str]


@dataclass(slots=True)
class Summary:
    """Summary of inventory comparison results."""

    current_hosts: int
    new_hosts: int
    host_added: list[str]
    host_removed: list[str]
    host_delta: int
    host_delta_pct: float

    var_changes_total: int
    var_change_pct: float
    var_baseline_keys: int

    limits: Limits
    sample_per_host_changes: dict[str, dict[str, list[str]]]


# ---------- IO ----------
def load_yaml(path: str) -> dict[str, Any]:
    """Load and parse a YAML inventory file."""
    with open(path, encoding="utf-8") as f:
        data = yaml.load(f) or {}
    if not isinstance(data, dict):
        raise TypeError(f"{path} is not a YAML mapping at root")
    return dict(data)


# ---------- Core helpers ----------
def _merge(a: Mapping[str, Any] | None, b: Mapping[str, Any] | None) -> VarsMap:
    """Merge two variable dictionaries, with b taking precedence."""
    out: VarsMap = dict(a or {})
    if b:
        out.update(b)
    return out


def _is_scalar(x: Any) -> bool:
    """Check if a value is a scalar type."""
    return isinstance(x, (str, int, float, bool)) or x is None


def collect_effective_hostvars(inv_root: YAMLNode | None) -> HostVars:
    """
    Walk the inventory tree (group/host/children) and compute the effective
    host var mapping, merging group vars down to each host.

    Follows Ansible's variable precedence rules where child groups and hosts
    inherit variables from parent groups.
    """
    hosts: HostVars = {}
    if not inv_root:
        return hosts

    allnode: YAMLNode = inv_root.get("all", inv_root)

    def walk(group_node: YAMLNode, inherited_vars: VarsMap) -> None:
        if not isinstance(group_node, Mapping):
            return

        group_vars_raw = group_node.get("vars")
        group_vars = dict(group_vars_raw) if isinstance(group_vars_raw, Mapping) else {}
        merged = _merge(inherited_vars, group_vars)

        grp_hosts_raw = group_node.get("hosts")
        grp_hosts = dict(grp_hosts_raw) if isinstance(grp_hosts_raw, Mapping) else {}

        for host, hv in grp_hosts.items():
            hv_map = dict(hv) if isinstance(hv, Mapping) else {}
            eff = _merge(merged, hv_map)
            prev = hosts.get(host, {})
            hosts[host] = _merge(prev, eff)

        children_raw = group_node.get("children")
        children = dict(children_raw) if isinstance(children_raw, Mapping) else {}
        for _name, child in children.items():
            if isinstance(child, Mapping):
                walk(child, merged)

    walk(allnode, {})
    return hosts


def filter_vars(d: VarsMap, ignored_regexes: list[re.Pattern[str]]) -> VarsMap:
    """
    Return a copy of vars with keys matching any ignore regex removed.

    Args:
        d: Variable dictionary to filter
        ignored_regexes: List of compiled regex patterns to match against keys

    Returns:
        Filtered dictionary with ignored keys removed
    """
    if not ignored_regexes:
        return d
    out: VarsMap = {}
    for k, v in d.items():
        if any(rx.search(k) for rx in ignored_regexes):
            logger.debug("Ignoring key '%s' due to regex match", k)
            continue
        out[k] = v
    return out


def canon(v: Any) -> str:
    """
    Canonicalize a value for comparison by converting to sorted JSON string.

    This ensures consistent comparison of complex data structures.
    """
    try:
        return json.dumps(v, sort_keys=True, separators=(",", ":"), ensure_ascii=False)
    except TypeError:
        return repr(v)


def normalize_for_compare(
    key: str,
    value: Any,
    setlike_key_patterns: list[re.Pattern[str]],
) -> Any:
    """
    For keys configured as set-like, treat list-of-scalars as unordered:
    deduplicate and sort by canonical form before comparing.

    This allows treating certain lists (like host collections) as sets
    where order doesn't matter.
    """
    if (
        isinstance(value, list)
        and any(p.search(key) for p in setlike_key_patterns)
        and all(_is_scalar(i) for i in value)
    ):
        canon_items = sorted({canon(i) for i in value})
        logger.debug(
            "Normalized set-like key '%s' from %d to %d items",
            key,
            len(value),
            len(canon_items),
        )
        return canon_items
    return value


def run_comparison(args: argparse.Namespace) -> Summary:
    """
    Run the full inventory comparison.

    Args:
        args: Parsed and merged configuration

    Returns:
        Summary object with comparison results

    Raises:
        FileNotFoundError: If inventory files don't exist
        ValueError: If YAML is invalid
        re.error: If regex patterns are invalid
    """
    logger.info("Starting inventory comparison")
    logger.debug("Config: current=%s, new=%s", args.current, args.new)

    # Compile regex patterns
    try:
        ignored_regexes: list[re.Pattern[str]] = [re.compile(p) for p in args.ignore_key_regex]
    except re.error as e:
        logger.error("Invalid ignore_key_regex pattern: %s", e)
        raise

    try:
        setlike_key_patterns: list[re.Pattern[str]] = [
            re.compile(p) for p in args.set_like_key_regex
        ]
    except re.error as e:
        logger.error("Invalid set_like_key_regex pattern: %s", e)
        raise

    # Load inventories
    logger.info("Loading current inventory: %s", args.current)
    try:
        current = load_yaml(args.current)
    except FileNotFoundError:
        logger.error("Current inventory file not found: %s", args.current)
        raise
    except Exception as e:
        logger.error("Failed to load current inventory: %s", e)
        raise

    logger.info("Loading new inventory: %s", args.new)
    try:
        new = load_yaml(args.new)
    except FileNotFoundError:
        logger.error("New inventory file not found: %s", args.new)
        raise
    except Exception as e:
        logger.error("Failed to load new inventory: %s", e)
        raise

    # Compute effective variables
    logger.info("Computing effective host variables")
    current_hosts = collect_effective_hostvars(current)
    new_hosts = collect_effective_hostvars(new)

    current_host_set: set[str] = set(current_hosts)
    new_host_set: set[str] = set(new_hosts)

    logger.debug("Current hosts: %d, New hosts: %d", len(current_host_set), len(new_host_set))

    # Compare hosts
    added_hosts: list[str] = sorted(new_host_set - current_host_set)
    removed_hosts: list[str] = sorted(current_host_set - new_host_set)

    host_delta: int = len(added_hosts) + len(removed_hosts)
    current_host_count: int = max(1, len(current_host_set))
    host_delta_pct: float = (host_delta / current_host_count) * 100.0

    if added_hosts:
        logger.info("Hosts added: %d", len(added_hosts))
        logger.debug("Added hosts: %s", added_hosts[:5])
    if removed_hosts:
        logger.info("Hosts removed: %d", len(removed_hosts))
        logger.debug("Removed hosts: %s", removed_hosts[:5])

    # Compare variables for common hosts
    common_hosts: list[str] = sorted(current_host_set & new_host_set)
    logger.info("Comparing variables for %d common hosts", len(common_hosts))

    var_changes: int = 0
    var_baseline_keys: int = 0
    per_host_changes: dict[str, dict[str, list[str]]] = {}

    for h in common_hosts:
        cvars = filter_vars(deepcopy(current_hosts[h]), ignored_regexes)
        nvars = filter_vars(deepcopy(new_hosts[h]), ignored_regexes)

        ckeys: set[str] = set(cvars.keys())
        nkeys: set[str] = set(nvars.keys())

        added_keys: list[str] = sorted(nkeys - ckeys)
        removed_keys: list[str] = sorted(ckeys - nkeys)
        common_keys: set[str] = ckeys & nkeys

        changed_values: list[str] = []
        for k in sorted(common_keys):
            cv = normalize_for_compare(k, cvars.get(k), setlike_key_patterns)
            nv = normalize_for_compare(k, nvars.get(k), setlike_key_patterns)
            if canon(cv) != canon(nv):
                changed_values.append(k)
                logger.debug("Host %s: variable '%s' changed", h, k)

        changes_for_h: int = len(added_keys) + len(removed_keys) + len(changed_values)
        var_changes += changes_for_h
        var_baseline_keys += len(ckeys)

        if changes_for_h:
            logger.debug(
                "Host %s: %d changes (%d added, %d removed, %d modified)",
                h,
                changes_for_h,
                len(added_keys),
                len(removed_keys),
                len(changed_values),
            )
            per_host_changes[h] = {
                "added_keys": added_keys,
                "removed_keys": removed_keys,
                "changed_values": changed_values,
            }

    var_baseline_keys = max(1, var_baseline_keys)
    var_change_pct: float = (var_changes / var_baseline_keys) * 100.0

    logger.info(
        "Summary: %d host changes (%.2f%%), %d variable changes (%.2f%%)",
        host_delta,
        host_delta_pct,
        var_changes,
        var_change_pct,
    )

    # Build summary
    limits = Limits(
        max_host_change_pct=args.max_host_change_pct,
        max_var_change_pct=args.max_var_change_pct,
        max_host_change_abs=args.max_host_change_abs,
        max_var_change_abs=args.max_var_change_abs,
        ignored_key_regex=args.ignore_key_regex,
    )

    summary = Summary(
        current_hosts=len(current_host_set),
        new_hosts=len(new_host_set),
        host_added=added_hosts,
        host_removed=removed_hosts,
        host_delta=host_delta,
        host_delta_pct=round(host_delta_pct, 3),
        var_changes_total=var_changes,
        var_change_pct=round(var_change_pct, 3),
        var_baseline_keys=var_baseline_keys,
        limits=limits,
        sample_per_host_changes={h: per_host_changes[h] for h in list(per_host_changes)[:20]},
    )

    return summary
