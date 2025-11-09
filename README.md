# Inventory Guard

A semantic change guard for Ansible inventories that detects unexpected
infrastructure changes before they reach production.

## What It Does

Inventory Guard compares two Ansible inventory files (current vs. new) and
flags changes that exceed your configured thresholds. Instead of blindly
accepting inventory updates, you can:

- **Catch accidents**: Detect when a typo removes 50 hosts instead of 5
- **Prevent drift**: Alert when variable changes exceed expected patterns
- **Gate CI/CD**: Block merges or deployments when changes look suspicious
- **Audit changes**: Generate reports showing exactly what changed

## Installation

```sh
# With uv (recommended)
uv pip install inventory-guard

# With pip
pip install inventory-guard
```

## Quick Start

```sh
# Compare two inventory files
inventory-guard \
  --current inventory/prod.yml \
  --new inventory/prod-updated.yml \
  --max-host-change-pct 5.0 \
  --max-var-change-pct 2.0
```

If changes exceed thresholds, the tool exits with code 2. Otherwise, it exits 0.

## Configuration File

Create `inventory_semantic_guard.toml`:

```toml
[inventory_guard]
current = "inventory/prod.yml"
new = "inventory/prod-updated.yml"

max_host_change_pct = 5.0
max_var_change_pct = 2.0
max_host_change_abs = 10
max_var_change_abs = 50

# Ignore volatile keys that change frequently
ignore_key_regex = [
  "^build_id$",
  "^last_updated$",
  "^timestamp$"
]

# Treat these as unordered sets (order doesn't matter)
set_like_key_regex = [
  "^foreman_host_collections$"
]

# Optional outputs
json_out = "changes.json"
report = "changes.md"
```

Then run without arguments:

```sh
inventory-guard
```

## CLI Options

```
--config PATH              Path to TOML config (default:
                           ./inventory_semantic_guard.toml)
--current PATH             Current inventory file (required)
--new PATH                 New inventory file (required)
--max-host-change-pct N    Max % of hosts that can be added/removed
                           (default: 5.0)
--max-var-change-pct N     Max % of variable keys that can change
                           (default: 2.0)
--max-host-change-abs N    Absolute cap on host changes (default: 0 = disabled)
--max-var-change-abs N     Absolute cap on variable changes
                           (default: 0 = disabled)
--ignore-key-regex REGEX   Variable keys to ignore (repeatable)
--set-like-key-regex REGEX Treat list values as unordered sets (repeatable)
--json-out PATH            Write JSON summary to file
--report PATH              Write Markdown report to file
```

## How It Works

1. **Loads both inventories**: Parses YAML with Ansible vault tag support
2. **Computes effective variables**: Merges group vars and host vars following
   Ansible precedence
3. **Compares hosts**: Detects added/removed hosts
4. **Compares variables**: For common hosts, counts variable key additions,
   removals, and value changes
5. **Applies thresholds**: Fails if changes exceed configured limits
6. **Generates reports**: Outputs JSON summary and optional Markdown report

## Exit Codes

- `0`: Changes are within acceptable thresholds
- `2`: Changes exceed thresholds (semantic guard failure)
- Other: Errors (file not found, invalid YAML, etc.)

## Use Cases

### CI/CD Pipeline

```yaml
# .gitlab-ci.yml
inventory-check:
  script:
    - inventory-guard --current $CI_PROJECT_DIR/inventory/prod.yml
      --new $CI_PROJECT_DIR/inventory/prod-new.yml
  only:
    - merge_requests
```

### Pre-commit Hook

```yaml
# .pre-commit-config.yaml
repos:
  - repo: local
    hooks:
      - id: inventory-guard
        name: Check inventory changes
        entry: inventory-guard
        language: system
        pass_filenames: false
```

### Manual Review

```sh
# Generate a detailed report for manual review
inventory-guard \
  --current prod.yml \
  --new prod-updated.yml \
  --report changes.md \
  --json-out changes.json

# Review the report
less changes.md
```

## Configuration Precedence

1. **CLI arguments** (highest priority)
2. **Config file** values
3. **Built-in defaults** (lowest priority)

## Advanced Features

### Ansible Vault Support

Inventory Guard parses `!vault` tags as opaque strings. Encrypted values are
compared as-is:

```yaml
all:
  hosts:
    app-1:
      db_password: !vault |
        $ANSIBLE_VAULT;1.2;AES256;dev
        3061323363346134383765383366633364306163656130333837366131383833356565
        3263363434623733343538653462613064333634333464660a66363362393939343931
        6163623763653733393830633138333935326536323964393966663938653062633063
        6664656334373166630a36373639326266646566343261393261303630396334326362
        6330
```

### Set-like Keys

For variables like host collections where order doesn't matter:

```sh
--set-like-key-regex '^foreman_host_collections$'
```

This treats `[A, B, C]` and `[C, A, B]` as identical.

### Ignoring Volatile Keys

Some keys change on every run (timestamps, build IDs). Ignore them:

```sh
--ignore-key-regex '^build_id$' \
--ignore-key-regex '^generated_at$'
```

## Development

- Clone the repo

  ```sh
  git clone https://gitlab.com/maartenq/inventory_guard.git
  ```

- Cd into inventory_guard

```sh
cd inventory_guard
```

- Install dependencies

  ```sh
  task install
  ```

- Run tests

  ```sh
  task test
  ```

- Run type checking

  ```sh
  task type
  ```

- Run linting
  ```sh
  task lint
  ```

```

## License

MIT License (see LICENSE file)

## Contributing

Issues and merge requests welcome at https://gitlab.com/maartenq/inventory_guard
```
