#!/usr/bin/env python3
"""
RDS Instance Type Modification Script for AWS CloudShell

This script modifies RDS SQL Server instance types based on a CSV input file.
It supports dry-run mode, parallel execution with progress monitoring, and
comprehensive error handling.

Usage:
    python rds_modify_instance_type.py --csv instances.csv [--dry-run] [--yes] [--output results.csv]

CSV Format:
    db_instance_identifier,new_instance_type,apply_immediately
    my-database-1,db.r5.large,true
    prod-db,db.r5.xlarge,false
"""

import argparse
import csv
import sys
import time
from datetime import datetime, timedelta
from enum import Enum
from typing import Optional

try:
    import boto3
    from botocore.exceptions import ClientError, BotoCoreError
except ImportError:
    print("ERROR: boto3 is required. Install with: pip install boto3")
    sys.exit(1)


# Valid SQL Server instance type prefixes
VALID_SQLSERVER_INSTANCE_FAMILIES = [
    'db.t2', 'db.t3', 'db.t3',
    'db.m4', 'db.m5', 'db.m5d', 'db.m6i', 'db.m6idn', 'db.m6in', 'db.m7i',
    'db.r4', 'db.r5', 'db.r5b', 'db.r5d', 'db.r6i', 'db.r6idn', 'db.r6in', 'db.r7i',
    'db.x1', 'db.x1e', 'db.x2idn', 'db.x2iedn',
    'db.z1d'
]

# RDS statuses that indicate modification is in progress
MODIFYING_STATUSES = [
    'modifying', 'upgrading', 'rebooting', 'configuring-enhanced-monitoring',
    'configuring-iam-database-auth', 'configuring-log-exports', 'maintenance',
    'renaming', 'resetting-master-credentials', 'storage-optimization'
]

# RDS statuses that indicate a terminal failure
FAILED_STATUSES = [
    'failed', 'incompatible-parameters', 'incompatible-restore', 
    'incompatible-network', 'incompatible-option-group', 'storage-full',
    'inaccessible-encryption-credentials', 'upgrade_failed'
]


class ModificationPhase(Enum):
    """Tracks the phase of instance modification."""
    WAITING_TO_START = "waiting_to_start"
    IN_PROGRESS = "in_progress"
    VERIFYING = "verifying"
    VERIFIED = "verified"
    FAILED = "failed"
    TIMED_OUT = "timed_out"


class Colors:
    """ANSI color codes for terminal output."""
    RED = '\033[91m'
    GREEN = '\033[92m'
    YELLOW = '\033[93m'
    BLUE = '\033[94m'
    CYAN = '\033[96m'
    BOLD = '\033[1m'
    END = '\033[0m'


def print_info(message: str) -> None:
    """Print info message in blue."""
    print(f"{Colors.BLUE}[INFO]{Colors.END} {message}")


def print_success(message: str) -> None:
    """Print success message in green."""
    print(f"{Colors.GREEN}[SUCCESS]{Colors.END} {message}")


def print_warning(message: str) -> None:
    """Print warning message in yellow."""
    print(f"{Colors.YELLOW}[WARNING]{Colors.END} {message}")


def print_error(message: str) -> None:
    """Print error message in red."""
    print(f"{Colors.RED}[ERROR]{Colors.END} {message}")


def print_header(message: str) -> None:
    """Print header message in bold cyan."""
    print(f"\n{Colors.BOLD}{Colors.CYAN}{'='*60}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{message}{Colors.END}")
    print(f"{Colors.BOLD}{Colors.CYAN}{'='*60}{Colors.END}\n")


class RDSInstanceModifier:
    """Handles RDS instance type modifications."""

    def __init__(self, region: Optional[str] = None):
        """Initialize the RDS client."""
        self.session = boto3.Session(region_name=region)
        self.rds_client = self.session.client('rds')
        self.region = self.session.region_name
        print_info(f"Connected to AWS region: {self.region}")

    def get_instance_details(self, db_instance_identifier: str) -> Optional[dict]:
        """
        Retrieve details for a specific RDS instance.
        
        Args:
            db_instance_identifier: The DB instance identifier
            
        Returns:
            Instance details dict or None if not found
        """
        try:
            response = self.rds_client.describe_db_instances(
                DBInstanceIdentifier=db_instance_identifier
            )
            if response['DBInstances']:
                return response['DBInstances'][0]
            return None
        except ClientError as e:
            if e.response['Error']['Code'] == 'DBInstanceNotFound':
                return None
            raise

    def validate_instance_type_for_sqlserver(self, instance_type: str) -> tuple[bool, str]:
        """
        Validate that the instance type is valid for SQL Server.
        
        Args:
            instance_type: The target instance type (e.g., db.r5.large)
            
        Returns:
            Tuple of (is_valid, message)
        """
        # Check format
        if not instance_type.startswith('db.'):
            return False, f"Invalid format: {instance_type}. Must start with 'db.'"
        
        # Check if instance family is valid for SQL Server
        for family in VALID_SQLSERVER_INSTANCE_FAMILIES:
            if instance_type.startswith(family):
                return True, "Valid instance type for SQL Server"
        
        return False, f"Instance type {instance_type} may not be valid for SQL Server"

    def validate_modification_request(self, row: dict) -> dict:
        """
        Validate a single modification request.
        
        Args:
            row: Dictionary containing db_instance_identifier, new_instance_type, apply_immediately
            
        Returns:
            Validation result dictionary
        """
        result = {
            'db_instance_identifier': row.get('db_instance_identifier', '').strip(),
            'new_instance_type': row.get('new_instance_type', '').strip(),
            'apply_immediately': row.get('apply_immediately', 'false').strip().lower() == 'true',
            'valid': False,
            'warnings': [],
            'errors': [],
            'current_instance_type': None,
            'current_status': None,
            'multi_az': False,
            'engine': None,
            'engine_version': None
        }

        identifier = result['db_instance_identifier']
        new_type = result['new_instance_type']

        # Check required fields
        if not identifier:
            result['errors'].append("Missing db_instance_identifier")
            return result
        
        if not new_type:
            result['errors'].append("Missing new_instance_type")
            return result

        # Validate instance type format for SQL Server
        type_valid, type_message = self.validate_instance_type_for_sqlserver(new_type)
        if not type_valid:
            result['errors'].append(type_message)
            return result

        # Get instance details
        try:
            instance = self.get_instance_details(identifier)
        except (ClientError, BotoCoreError) as e:
            result['errors'].append(f"AWS API error: {str(e)}")
            return result

        if not instance:
            result['errors'].append(f"Instance '{identifier}' not found")
            return result

        # Store instance details
        result['current_instance_type'] = instance.get('DBInstanceClass')
        result['current_status'] = instance.get('DBInstanceStatus')
        result['multi_az'] = instance.get('MultiAZ', False)
        result['engine'] = instance.get('Engine', '')
        result['engine_version'] = instance.get('EngineVersion', '')

        # Verify it's a SQL Server instance
        if not result['engine'].startswith('sqlserver'):
            result['errors'].append(
                f"Instance '{identifier}' is not SQL Server (engine: {result['engine']})"
            )
            return result

        # Check if instance is available
        if result['current_status'] != 'available':
            result['errors'].append(
                f"Instance '{identifier}' is not available (status: {result['current_status']})"
            )
            return result

        # Check for no-op
        if result['current_instance_type'] == new_type:
            result['errors'].append(
                f"Instance '{identifier}' is already {new_type} (no change needed)"
            )
            return result

        # Add Multi-AZ warning if applying immediately
        if result['multi_az'] and result['apply_immediately']:
            result['warnings'].append(
                f"Multi-AZ instance - modification will cause a failover and may take longer"
            )

        result['valid'] = True
        return result

    def modify_instance(self, db_instance_identifier: str, new_instance_type: str, 
                       apply_immediately: bool) -> dict:
        """
        Modify the instance type of an RDS instance.
        
        Args:
            db_instance_identifier: The DB instance identifier
            new_instance_type: The target instance type
            apply_immediately: Whether to apply changes immediately
            
        Returns:
            Result dictionary with status and details
        """
        result = {
            'db_instance_identifier': db_instance_identifier,
            'new_instance_type': new_instance_type,
            'apply_immediately': apply_immediately,
            'success': False,
            'error': None,
            'pending_modified_values': None
        }

        try:
            response = self.rds_client.modify_db_instance(
                DBInstanceIdentifier=db_instance_identifier,
                DBInstanceClass=new_instance_type,
                ApplyImmediately=apply_immediately
            )
            
            result['success'] = True
            result['pending_modified_values'] = response['DBInstance'].get(
                'PendingModifiedValues', {}
            )
            
        except ClientError as e:
            result['error'] = f"AWS API error: {e.response['Error']['Message']}"
        except BotoCoreError as e:
            result['error'] = f"AWS SDK error: {str(e)}"
        except Exception as e:
            result['error'] = f"Unexpected error: {str(e)}"

        return result

    def get_instance_status_and_type(self, db_instance_identifier: str) -> tuple[Optional[str], Optional[str]]:
        """
        Get the current status and instance type of an RDS instance.
        
        Args:
            db_instance_identifier: The DB instance identifier
            
        Returns:
            Tuple of (status, instance_type) or (None, None) if error
        """
        try:
            instance = self.get_instance_details(db_instance_identifier)
            if instance:
                return instance.get('DBInstanceStatus'), instance.get('DBInstanceClass')
            return None, None
        except Exception:
            return None, None


def parse_csv(csv_path: str) -> list[dict]:
    """
    Parse the input CSV file.
    
    Args:
        csv_path: Path to the CSV file
        
    Returns:
        List of dictionaries representing CSV rows
    """
    rows = []
    try:
        with open(csv_path, 'r', newline='', encoding='utf-8') as csvfile:
            reader = csv.DictReader(csvfile)
            
            # Validate required columns
            required_columns = {'db_instance_identifier', 'new_instance_type', 'apply_immediately'}
            if not required_columns.issubset(set(reader.fieldnames or [])):
                missing = required_columns - set(reader.fieldnames or [])
                print_error(f"CSV missing required columns: {missing}")
                sys.exit(1)
            
            for row in reader:
                rows.append(row)
                
    except FileNotFoundError:
        print_error(f"CSV file not found: {csv_path}")
        sys.exit(1)
    except csv.Error as e:
        print_error(f"Error reading CSV file: {e}")
        sys.exit(1)
    
    return rows


def write_results_csv(results: list[dict], output_path: str) -> None:
    """
    Write results to a CSV file.
    
    Args:
        results: List of result dictionaries
        output_path: Path to output CSV file
    """
    fieldnames = [
        'db_instance_identifier', 'current_instance_type', 'new_instance_type',
        'apply_immediately', 'multi_az', 'engine', 'engine_version',
        'modification_submitted', 'final_status', 'verified_instance_type',
        'errors', 'warnings'
    ]
    
    try:
        with open(output_path, 'w', newline='', encoding='utf-8') as csvfile:
            writer = csv.DictWriter(csvfile, fieldnames=fieldnames)
            writer.writeheader()
            
            for result in results:
                row = {
                    'db_instance_identifier': result.get('db_instance_identifier', ''),
                    'current_instance_type': result.get('current_instance_type', ''),
                    'new_instance_type': result.get('new_instance_type', ''),
                    'apply_immediately': result.get('apply_immediately', ''),
                    'multi_az': result.get('multi_az', ''),
                    'engine': result.get('engine', ''),
                    'engine_version': result.get('engine_version', ''),
                    'modification_submitted': result.get('modification_submitted', False),
                    'final_status': result.get('final_status', ''),
                    'verified_instance_type': result.get('verified_instance_type', ''),
                    'errors': '; '.join(result.get('errors', [])),
                    'warnings': '; '.join(result.get('warnings', []))
                }
                writer.writerow(row)
                
        print_success(f"Results written to: {output_path}")
        
    except IOError as e:
        print_error(f"Error writing results CSV: {e}")


def monitor_modifications(modifier: RDSInstanceModifier, pending_instances: list[dict],
                         poll_interval: int = 15, timeout_minutes: int = 30,
                         initial_delay: int = 15) -> dict:
    """
    Monitor the progress of pending modifications with state tracking.
    
    Args:
        modifier: RDSInstanceModifier instance
        pending_instances: List of instances being modified
        poll_interval: Seconds between status checks
        timeout_minutes: Maximum minutes to wait for modifications
        initial_delay: Seconds to wait before first status check
        
    Returns:
        Dictionary mapping instance identifier to result dict
    """
    if not pending_instances:
        return {}
    
    print_header("Monitoring Modification Progress")
    
    # Initialize tracking state for each instance
    tracking = {}
    for inst in pending_instances:
        identifier = inst['db_instance_identifier']
        tracking[identifier] = {
            'phase': ModificationPhase.WAITING_TO_START,
            'target_type': inst['new_instance_type'],
            'original_type': inst['current_instance_type'],
            'has_left_available': False,
            'final_status': None,
            'verified_instance_type': None,
            'last_rds_status': None
        }
    
    # Calculate timeout deadline
    start_time = datetime.now()
    timeout_deadline = start_time + timedelta(minutes=timeout_minutes)
    
    # Initial delay to allow AWS to begin the modification
    print_info(f"Waiting {initial_delay} seconds for modifications to initiate...")
    time.sleep(initial_delay)
    
    # Continue until all instances reach terminal state or timeout
    while True:
        # Check for timeout
        if datetime.now() > timeout_deadline:
            print_warning(f"Timeout reached ({timeout_minutes} minutes)")
            for identifier, state in tracking.items():
                if state['phase'] not in [ModificationPhase.VERIFIED, ModificationPhase.FAILED]:
                    state['phase'] = ModificationPhase.TIMED_OUT
                    state['final_status'] = 'TIMED_OUT'
            break
        
        # Get instances still in progress
        in_progress = {
            identifier: state for identifier, state in tracking.items()
            if state['phase'] not in [ModificationPhase.VERIFIED, ModificationPhase.FAILED, ModificationPhase.TIMED_OUT]
        }
        
        if not in_progress:
            break
        
        # Calculate elapsed time
        elapsed = datetime.now() - start_time
        elapsed_str = str(elapsed).split('.')[0]  # Remove microseconds
        
        print_info(f"Checking status of {len(in_progress)} instance(s)... [Elapsed: {elapsed_str}]")
        
        for identifier, state in in_progress.items():
            rds_status, current_type = modifier.get_instance_status_and_type(identifier)
            state['last_rds_status'] = rds_status
            
            if rds_status is None:
                print_warning(f"       {identifier}: Unable to retrieve status")
                continue
            
            # State machine logic
            if state['phase'] == ModificationPhase.WAITING_TO_START:
                if rds_status in MODIFYING_STATUSES:
                    # Transition: Instance has started modifying
                    state['phase'] = ModificationPhase.IN_PROGRESS
                    state['has_left_available'] = True
                    print(f"       {identifier}: Modification in progress (status: {rds_status})")
                elif rds_status in FAILED_STATUSES:
                    # Transition: Failed before starting
                    state['phase'] = ModificationPhase.FAILED
                    state['final_status'] = f'FAILED ({rds_status})'
                    print_error(f"       {identifier}: Failed (status: {rds_status})")
                else:
                    # Still waiting to start
                    print(f"       {identifier}: Waiting for modification to start (status: {rds_status})")
            
            elif state['phase'] == ModificationPhase.IN_PROGRESS:
                if rds_status == 'available':
                    # Transition: Back to available, need to verify
                    state['phase'] = ModificationPhase.VERIFYING
                    print(f"       {identifier}: Instance available, verifying instance type...")
                    
                    # Verify the instance type changed
                    if current_type == state['target_type']:
                        state['phase'] = ModificationPhase.VERIFIED
                        state['final_status'] = 'VERIFIED'
                        state['verified_instance_type'] = current_type
                        print_success(
                            f"       {identifier}: Verified - {state['original_type']} → {current_type} ✓"
                        )
                    else:
                        # Available but type didn't change - something went wrong
                        state['phase'] = ModificationPhase.FAILED
                        state['final_status'] = f'TYPE_MISMATCH (expected: {state["target_type"]}, actual: {current_type})'
                        state['verified_instance_type'] = current_type
                        print_error(
                            f"       {identifier}: Type mismatch! Expected {state['target_type']}, got {current_type}"
                        )
                elif rds_status in FAILED_STATUSES:
                    # Transition: Failed during modification
                    state['phase'] = ModificationPhase.FAILED
                    state['final_status'] = f'FAILED ({rds_status})'
                    print_error(f"       {identifier}: Failed (status: {rds_status})")
                else:
                    # Still modifying
                    print(f"       {identifier}: Modification in progress (status: {rds_status})")
        
        # Check if all complete
        still_pending = [
            identifier for identifier, state in tracking.items()
            if state['phase'] not in [ModificationPhase.VERIFIED, ModificationPhase.FAILED, ModificationPhase.TIMED_OUT]
        ]
        
        if not still_pending:
            break
        
        # Wait before next poll
        print_info(f"Waiting {poll_interval} seconds before next check...")
        print()
        time.sleep(poll_interval)
    
    # Summary
    verified_count = sum(1 for s in tracking.values() if s['phase'] == ModificationPhase.VERIFIED)
    failed_count = sum(1 for s in tracking.values() if s['phase'] == ModificationPhase.FAILED)
    timeout_count = sum(1 for s in tracking.values() if s['phase'] == ModificationPhase.TIMED_OUT)
    
    if verified_count == len(tracking):
        print_success("All modifications verified complete")
    else:
        print_warning(
            f"Monitoring complete: {verified_count} verified, {failed_count} failed, {timeout_count} timed out"
        )
    
    return tracking


def display_validation_summary(validation_results: list[dict]) -> tuple[list[dict], list[dict]]:
    """
    Display validation summary and return valid/invalid instances.
    
    Args:
        validation_results: List of validation result dictionaries
        
    Returns:
        Tuple of (valid_instances, invalid_instances)
    """
    valid = [r for r in validation_results if r['valid']]
    invalid = [r for r in validation_results if not r['valid']]
    
    print_header("Validation Summary")
    
    print(f"Total instances in CSV: {len(validation_results)}")
    print(f"Valid for modification: {Colors.GREEN}{len(valid)}{Colors.END}")
    print(f"Invalid/skipped: {Colors.RED}{len(invalid)}{Colors.END}")
    print()
    
    # Show invalid instances
    if invalid:
        print(f"{Colors.BOLD}Instances that will be SKIPPED:{Colors.END}")
        for result in invalid:
            print(f"  • {result['db_instance_identifier']}")
            for error in result['errors']:
                print(f"    {Colors.RED}✗ {error}{Colors.END}")
        print()
    
    # Show valid instances
    if valid:
        print(f"{Colors.BOLD}Instances that will be MODIFIED:{Colors.END}")
        for result in valid:
            apply_timing = "IMMEDIATELY" if result['apply_immediately'] else "during maintenance window"
            print(f"  • {result['db_instance_identifier']}")
            print(f"    {result['current_instance_type']} → {result['new_instance_type']} ({apply_timing})")
            print(f"    Engine: {result['engine']} {result['engine_version']}")
            print(f"    Multi-AZ: {result['multi_az']}")
            for warning in result['warnings']:
                print(f"    {Colors.YELLOW}⚠ {warning}{Colors.END}")
        print()
    
    return valid, invalid


def confirm_execution() -> bool:
    """
    Prompt user for confirmation.
    
    Returns:
        True if user confirms, False otherwise
    """
    while True:
        response = input(f"{Colors.BOLD}Do you want to proceed with these modifications? (y/n): {Colors.END}").strip().lower()
        if response in ('y', 'yes'):
            return True
        elif response in ('n', 'no'):
            return False
        else:
            print("Please enter 'y' or 'n'")


def main():
    """Main entry point."""
    parser = argparse.ArgumentParser(
        description='Modify RDS SQL Server instance types based on CSV input',
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
CSV Format:
    db_instance_identifier,new_instance_type,apply_immediately
    my-database-1,db.r5.large,true
    prod-db,db.r5.xlarge,false

Examples:
    # Dry run (validation only)
    python rds_modify_instance_type.py --csv instances.csv --dry-run

    # Execute with confirmation prompt
    python rds_modify_instance_type.py --csv instances.csv

    # Execute without confirmation (for automation)
    python rds_modify_instance_type.py --csv instances.csv --yes

    # Specify output file and custom timeout
    python rds_modify_instance_type.py --csv instances.csv --output my_results.csv --timeout 45
        """
    )
    
    parser.add_argument('--csv', required=True, help='Path to input CSV file')
    parser.add_argument('--dry-run', action='store_true', 
                       help='Validate only, do not execute modifications')
    parser.add_argument('--yes', '-y', action='store_true',
                       help='Skip confirmation prompt')
    parser.add_argument('--output', '-o', 
                       help='Output CSV file path (default: auto-generated with timestamp)')
    parser.add_argument('--region', help='AWS region (default: use AWS CLI/environment config)')
    parser.add_argument('--poll-interval', type=int, default=15,
                       help='Seconds between status checks (default: 15)')
    parser.add_argument('--timeout', type=int, default=30,
                       help='Maximum minutes to wait for modifications (default: 30)')
    parser.add_argument('--initial-delay', type=int, default=15,
                       help='Seconds to wait before first status check (default: 15)')
    
    args = parser.parse_args()
    
    # Generate default output filename if not specified
    if not args.output:
        timestamp = datetime.now().strftime('%Y%m%d_%H%M%S')
        args.output = f'rds_modifications_{timestamp}.csv'
    
    print_header("RDS Instance Type Modification Script")
    
    if args.dry_run:
        print(f"{Colors.YELLOW}{Colors.BOLD}*** DRY RUN MODE - No changes will be made ***{Colors.END}\n")
    
    # Parse CSV
    print_info(f"Reading CSV file: {args.csv}")
    rows = parse_csv(args.csv)
    print_info(f"Found {len(rows)} row(s) in CSV")
    print()
    
    if not rows:
        print_warning("No rows found in CSV file. Exiting.")
        sys.exit(0)
    
    # Initialize RDS modifier
    try:
        modifier = RDSInstanceModifier(region=args.region)
    except Exception as e:
        print_error(f"Failed to initialize AWS connection: {e}")
        sys.exit(1)
    
    print()
    
    # Validate all rows
    print_header("Validating Instances")
    validation_results = []
    for i, row in enumerate(rows, 1):
        identifier = row.get('db_instance_identifier', 'UNKNOWN').strip()
        print_info(f"Validating [{i}/{len(rows)}]: {identifier}")
        result = modifier.validate_modification_request(row)
        validation_results.append(result)
    
    # Display summary
    valid_instances, invalid_instances = display_validation_summary(validation_results)
    
    # Prepare final results list
    final_results = []
    
    # Add invalid instances to results
    for result in invalid_instances:
        result['modification_submitted'] = False
        result['final_status'] = 'SKIPPED'
        result['verified_instance_type'] = None
        final_results.append(result)
    
    # Exit if dry run
    if args.dry_run:
        print_header("Dry Run Complete")
        print_info("No modifications were made.")
        print_info(f"Timeout setting: {args.timeout} minutes")
        print_info(f"Poll interval: {args.poll_interval} seconds")
        print_info(f"Initial delay: {args.initial_delay} seconds")
        
        # Add valid instances to results with dry-run status
        for result in valid_instances:
            result['modification_submitted'] = False
            result['final_status'] = 'DRY_RUN'
            result['verified_instance_type'] = None
            final_results.append(result)
        
        write_results_csv(final_results, args.output)
        sys.exit(0)
    
    # Exit if no valid instances
    if not valid_instances:
        print_warning("No valid instances to modify. Exiting.")
        write_results_csv(final_results, args.output)
        sys.exit(0)
    
    # Confirm execution
    if not args.yes:
        if not confirm_execution():
            print_info("Operation cancelled by user.")
            for result in valid_instances:
                result['modification_submitted'] = False
                result['final_status'] = 'CANCELLED'
                result['verified_instance_type'] = None
                final_results.append(result)
            write_results_csv(final_results, args.output)
            sys.exit(0)
    
    # Execute modifications
    print_header("Executing Modifications")
    
    pending_modifications = []
    
    for result in valid_instances:
        identifier = result['db_instance_identifier']
        new_type = result['new_instance_type']
        apply_immediately = result['apply_immediately']
        
        print_info(f"Modifying {identifier}: {result['current_instance_type']} → {new_type}")
        
        mod_result = modifier.modify_instance(identifier, new_type, apply_immediately)
        
        if mod_result['success']:
            print_success(f"  Modification submitted successfully")
            result['modification_submitted'] = True
            
            # Only monitor if applying immediately
            if apply_immediately:
                pending_modifications.append(result)
            else:
                result['final_status'] = 'SCHEDULED_FOR_MAINTENANCE_WINDOW'
                result['verified_instance_type'] = None
                final_results.append(result)
                print_info(f"  Change will be applied during next maintenance window")
        else:
            print_error(f"  Failed: {mod_result['error']}")
            result['modification_submitted'] = False
            result['errors'].append(mod_result['error'])
            result['final_status'] = 'SUBMISSION_FAILED'
            result['verified_instance_type'] = None
            final_results.append(result)
    
    # Monitor pending modifications (only those applied immediately)
    if pending_modifications:
        tracking_results = monitor_modifications(
            modifier, 
            pending_modifications, 
            poll_interval=args.poll_interval,
            timeout_minutes=args.timeout,
            initial_delay=args.initial_delay
        )
        
        # Update final results with monitored statuses
        for result in pending_modifications:
            identifier = result['db_instance_identifier']
            if identifier in tracking_results:
                tracking = tracking_results[identifier]
                result['final_status'] = tracking.get('final_status', 'UNKNOWN')
                result['verified_instance_type'] = tracking.get('verified_instance_type')
            final_results.append(result)
    
    # Write results
    print()
    write_results_csv(final_results, args.output)
    
    # Print summary
    print_header("Execution Summary")
    
    submitted = sum(1 for r in final_results if r.get('modification_submitted'))
    verified = sum(1 for r in final_results if r.get('final_status') == 'VERIFIED')
    scheduled = sum(1 for r in final_results if r.get('final_status') == 'SCHEDULED_FOR_MAINTENANCE_WINDOW')
    failed = sum(1 for r in final_results if r.get('final_status', '').startswith('FAILED') or r.get('final_status') == 'SUBMISSION_FAILED')
    timed_out = sum(1 for r in final_results if r.get('final_status') == 'TIMED_OUT')
    skipped = sum(1 for r in final_results if r.get('final_status') == 'SKIPPED')
    
    print(f"Total instances processed: {len(final_results)}")
    print(f"Modifications submitted: {submitted}")
    print(f"  - Verified successful: {Colors.GREEN}{verified}{Colors.END}")
    print(f"  - Scheduled for maintenance: {Colors.BLUE}{scheduled}{Colors.END}")
    print(f"  - Failed: {Colors.RED}{failed}{Colors.END}")
    print(f"  - Timed out: {Colors.YELLOW}{timed_out}{Colors.END}")
    print(f"Skipped: {Colors.YELLOW}{skipped}{Colors.END}")
    print()
    print(f"Results saved to: {args.output}")


if __name__ == '__main__':
    main()
