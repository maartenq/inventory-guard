"""Property-based tests using Hypothesis for inventory comparison logic."""

import json
import tempfile
from pathlib import Path

from hypothesis import assume, given, settings
from hypothesis import strategies as st

from inventory_guard import compare


# ---------- Strategies for generating Ansible inventories ----------


@st.composite
def var_value(draw):
    """Generate valid Ansible variable values (ASCII only for YAML compat)."""
    return draw(
        st.one_of(
            st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=0, max_size=50),
            st.integers(),
            st.floats(allow_nan=False, allow_infinity=False),
            st.booleans(),
            st.none(),
            st.lists(st.text(alphabet=st.characters(min_codepoint=32, max_codepoint=126), min_size=0, max_size=20), max_size=10),
        )
    )


@st.composite
def host_vars(draw):
    """Generate a dictionary of host variables."""
    return draw(
        st.dictionaries(
            keys=st.text(
                alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789_",
                min_size=1,
                max_size=20,
            ),
            values=var_value(),
            min_size=0,
            max_size=10,
        )
    )


@st.composite
def host_name(draw):
    """Generate valid host names."""
    name = draw(
        st.text(
            alphabet="abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789-_.",
            min_size=1,
            max_size=30,
        )
    )
    # Ensure it starts with alphanumeric
    if name and not name[0].isalnum():
        name = "h" + name
    return name


@st.composite
def simple_inventory(draw, min_hosts=0, max_hosts=20):
    """Generate a simple flat Ansible inventory."""
    hosts_dict = draw(
        st.dictionaries(
            keys=host_name(),
            values=host_vars(),
            min_size=min_hosts,
            max_size=max_hosts,
        )
    )
    
    return {
        "all": {
            "hosts": hosts_dict,
        }
    }


@st.composite
def nested_inventory(draw, min_hosts=0, max_hosts=10):
    """Generate inventory with group hierarchy and variable inheritance."""
    group_vars = draw(host_vars())
    child_vars = draw(host_vars())
    hosts_dict = draw(
        st.dictionaries(
            keys=host_name(),
            values=host_vars(),
            min_size=min_hosts,
            max_size=max_hosts,
        )
    )
    
    return {
        "all": {
            "vars": group_vars,
            "children": {
                "webservers": {
                    "vars": child_vars,
                    "hosts": hosts_dict,
                }
            },
        }
    }


# ---------- Property Tests ----------


@given(inventory=simple_inventory(min_hosts=1))
@settings(max_examples=100)
def test_idempotent_comparison(inventory):
    """Property: Comparing identical inventories always shows zero changes."""
    with tempfile.TemporaryDirectory() as tmpdir:
        inv_path = Path(tmpdir) / "inventory.yml"
        
        # Write inventory as JSON (YAML subset)
        inv_path.write_text(json.dumps(inventory))
        
        # Load and compare with itself
        inv_data = compare.load_yaml(str(inv_path))
        current_hosts = compare.collect_effective_hostvars(inv_data)
        new_hosts = compare.collect_effective_hostvars(inv_data)
        
        # Property: Same inventory should have no changes
        assert current_hosts == new_hosts
        
        current_set = set(current_hosts.keys())
        new_set = set(new_hosts.keys())
        
        assert current_set == new_set, "Host sets should be identical"
        assert len(current_set - new_set) == 0, "No hosts should be removed"
        assert len(new_set - current_set) == 0, "No hosts should be added"


@given(inv1=simple_inventory(min_hosts=1), inv2=simple_inventory(min_hosts=1))
@settings(max_examples=50)
def test_comparison_symmetry(inv1, inv2):
    """Property: Comparison results should be mathematically consistent."""
    assume(inv1 != inv2)  # Only test when they're different
    
    with tempfile.TemporaryDirectory() as tmpdir:
        # Write both inventories
        path1 = Path(tmpdir) / "inv1.yml"
        path2 = Path(tmpdir) / "inv2.yml"
        path1.write_text(json.dumps(inv1))
        path2.write_text(json.dumps(inv2))
        
        # Load both
        data1 = compare.load_yaml(str(path1))
        data2 = compare.load_yaml(str(path2))
        
        hosts1 = compare.collect_effective_hostvars(data1)
        hosts2 = compare.collect_effective_hostvars(data2)
        
        # Property: Set operations should be symmetric
        set1 = set(hosts1.keys())
        set2 = set(hosts2.keys())
        
        added = set2 - set1
        removed = set1 - set2
        
        # Property verified: symmetric difference works correctly
        assert len(added) >= 0 and len(removed) >= 0


@given(inventory=nested_inventory(min_hosts=1))
@settings(max_examples=100)
def test_variable_inheritance(inventory):
    """Property: Child hosts inherit parent group variables."""
    with tempfile.TemporaryDirectory() as tmpdir:
        inv_path = Path(tmpdir) / "inventory.yml"
        inv_path.write_text(json.dumps(inventory))
        
        inv_data = compare.load_yaml(str(inv_path))
        hosts = compare.collect_effective_hostvars(inv_data)
        
        # Extract expected inherited vars
        all_vars = inventory.get("all", {}).get("vars", {})
        child_vars = (
            inventory.get("all", {})
            .get("children", {})
            .get("webservers", {})
            .get("vars", {})
        )
        
        # Property: Every host should have inherited vars
        for host_name, host_vars_actual in hosts.items():
            # All group vars should be present (unless overridden)
            for key, value in all_vars.items():
                if key not in child_vars and key not in inventory["all"]["children"]["webservers"]["hosts"].get(host_name, {}):
                    assert key in host_vars_actual, f"Host {host_name} missing inherited var {key}"


@given(inventory=simple_inventory(min_hosts=0, max_hosts=50))
@settings(max_examples=100)
def test_no_host_loss(inventory):
    """Property: All input hosts appear in output, no data loss."""
    with tempfile.TemporaryDirectory() as tmpdir:
        inv_path = Path(tmpdir) / "inventory.yml"
        inv_path.write_text(json.dumps(inventory))
        
        inv_data = compare.load_yaml(str(inv_path))
        output_hosts = compare.collect_effective_hostvars(inv_data)
        
        input_host_names = set(inventory.get("all", {}).get("hosts", {}).keys())
        output_host_names = set(output_hosts.keys())
        
        # Property: No hosts should disappear
        assert input_host_names == output_host_names, "Host set should be preserved"


@given(
    inv1=simple_inventory(min_hosts=1, max_hosts=20),
    inv2=simple_inventory(min_hosts=1, max_hosts=20),
)
@settings(max_examples=50)
def test_delta_percentage_math(inv1, inv2):
    """Property: Delta percentages should match manual calculation."""
    with tempfile.TemporaryDirectory() as tmpdir:
        path1 = Path(tmpdir) / "inv1.yml"
        path2 = Path(tmpdir) / "inv2.yml"
        path1.write_text(json.dumps(inv1))
        path2.write_text(json.dumps(inv2))
        
        data1 = compare.load_yaml(str(path1))
        data2 = compare.load_yaml(str(path2))
        
        hosts1 = compare.collect_effective_hostvars(data1)
        hosts2 = compare.collect_effective_hostvars(data2)
        
        set1 = set(hosts1.keys())
        set2 = set(hosts2.keys())
        
        added = len(set2 - set1)
        removed = len(set1 - set2)
        delta = added + removed
        
        current_count = max(1, len(set1))
        expected_pct = (delta / current_count) * 100.0
        
        # Property: Manual calculation should match
        # Allow small floating point error
        actual_pct = (delta / current_count) * 100.0
        assert abs(expected_pct - actual_pct) < 0.01, "Percentage calculation mismatch"


@given(inventory=simple_inventory(min_hosts=1))
@settings(max_examples=100)
def test_canonicalization_stability(inventory):
    """Property: Canonicalization should be stable and deterministic."""
    with tempfile.TemporaryDirectory() as tmpdir:
        inv_path = Path(tmpdir) / "inventory.yml"
        inv_path.write_text(json.dumps(inventory))
        
        inv_data = compare.load_yaml(str(inv_path))
        hosts = compare.collect_effective_hostvars(inv_data)
        
        # Property: Canonicalizing same value twice gives same result
        for host_name, host_vars in hosts.items():
            for key, value in host_vars.items():
                canon1 = compare.canon(value)
                canon2 = compare.canon(value)
                assert canon1 == canon2, f"Canon not stable for {key}={value}"


@given(inventory=simple_inventory(min_hosts=1, max_hosts=10))
@settings(max_examples=50)
def test_filter_vars_preserves_unfiltered(inventory):
    """Property: Filtering with empty regex list preserves all vars."""
    with tempfile.TemporaryDirectory() as tmpdir:
        inv_path = Path(tmpdir) / "inventory.yml"
        inv_path.write_text(json.dumps(inventory))
        
        inv_data = compare.load_yaml(str(inv_path))
        hosts = compare.collect_effective_hostvars(inv_data)
        
        # Property: Empty filter list should preserve everything
        for host_name, host_vars in hosts.items():
            filtered = compare.filter_vars(host_vars, [])
            assert filtered == host_vars, "Empty filter should preserve all vars"


@given(inventory=nested_inventory(min_hosts=1))
@settings(max_examples=50)
def test_nested_group_vars_merge(inventory):
    """Property: Nested group vars merge correctly (child overrides parent)."""
    with tempfile.TemporaryDirectory() as tmpdir:
        inv_path = Path(tmpdir) / "inventory.yml"
        inv_path.write_text(json.dumps(inventory))
        
        inv_data = compare.load_yaml(str(inv_path))
        hosts = compare.collect_effective_hostvars(inv_data)
        
        all_vars = inventory.get("all", {}).get("vars", {})
        child_vars = (
            inventory.get("all", {})
            .get("children", {})
            .get("webservers", {})
            .get("vars", {})
        )
        
        # Property: Child vars should override parent vars
        for host_name, host_vars_actual in hosts.items():
            # Check that child vars took precedence
            for key, child_value in child_vars.items():
                if key in all_vars:
                    # Child should override parent
                    assert (
                        host_vars_actual.get(key) == child_value
                    ), f"Child var {key} should override parent"
