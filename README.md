# RDS SQL Server instance type modifier

Automate RDS SQL Server instance type changes from a CSV file. The script validates each row, submits modifications, optionally monitors immediate changes, and writes a detailed results CSV.

## Features

- **CSV-driven changes** with required columns: `db_instance_identifier`, `new_instance_type`, `apply_immediately`.
- **SQL Server safety checks**:
  - Ensures the instance engine starts with `sqlserver`.
  - Confirms the instance is `available`.
  - Rejects no-op changes (already on target type).
- **Instance type validation** for supported SQL Server families.
- **Dry-run mode** for validation-only workflows.
- **Interactive confirmation** (skippable with `--yes`).
- **Progress monitoring** when `apply_immediately=true`, with timeouts and polling.
- **Detailed results CSV** including warnings/errors, verification state, and engine metadata.

## Requirements

- Python 3.10+
- `boto3` installed (`pip install boto3`)
- AWS credentials configured (for example via `aws configure`, environment variables, or CloudShell)

## Usage

### Basic run (with confirmation prompt)

```bash
python rds_modify_instance_type.py --csv instances.csv
```

### Dry run (validation only)

```bash
python rds_modify_instance_type.py --csv instances.csv --dry-run
```

### Non-interactive execution

```bash
python rds_modify_instance_type.py --csv instances.csv --yes
```

### Custom output file and monitoring parameters

```bash
python rds_modify_instance_type.py \
  --csv instances.csv \
  --output my_results.csv \
  --poll-interval 30 \
  --timeout 45 \
  --initial-delay 20
```

### Use a specific region

```bash
python rds_modify_instance_type.py --csv instances.csv --region us-east-1
```

## CSV input format

Minimum required columns:

```csv
db_instance_identifier,new_instance_type,apply_immediately
my-database-1,db.r5.large,true
prod-db,db.r5.xlarge,false
```

You can use the included `sample_instances.csv` as a starting template.

## Output CSV columns

The output file includes:

- `db_instance_identifier`
- `current_instance_type`
- `new_instance_type`
- `apply_immediately`
- `multi_az`
- `engine`
- `engine_version`
- `modification_submitted`
- `final_status` (e.g. `VERIFIED`, `SCHEDULED_FOR_MAINTENANCE_WINDOW`, `FAILED`, `TIMED_OUT`, `SKIPPED`)
- `verified_instance_type`
- `errors`
- `warnings`

## Validation and monitoring behavior

- **Validation** ensures required fields exist, instance type family is valid for SQL Server, engine starts with `sqlserver`, and instance status is `available`.
- **Multi-AZ warning** is emitted if `apply_immediately=true`.
- **Monitoring** happens only for rows with `apply_immediately=true`. Scheduled modifications are reported as `SCHEDULED_FOR_MAINTENANCE_WINDOW`.
- **Timeouts** mark instances as `TIMED_OUT` if they do not complete within the configured window.

## Notes and limitations

- The script validates instance families against a static list for SQL Server. If AWS releases new families, the list may need updating.
- The script does not validate licensing model or storage compatibility. Ensure compatibility before applying changes.
- CloudShell and local environments both work if AWS credentials are configured properly.

## Troubleshooting

- **`boto3` import error**: Install it with `pip install boto3`.
- **Instance not found**: Confirm identifier spelling and region (`--region`).
- **Instance not available**: Wait for the instance to return to `available` state before re-running.
