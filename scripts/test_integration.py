#!/usr/bin/env python3
"""
test_integration.py - Arch Linux Upgrade Check — Script-Level Integration Tests (Layer 3)

Reads evals.json, runs arch_upgrade_check.py with mock data for each
test, checks script_assertions, and outputs results.

Fully deterministic: uses mock HTTP data, mock pacman.log, mock checkupdates.
No network required, no /var/log/pacman.log dependency.

Usage:
  # Run all tests
  python3 scripts/test_integration.py

  # Run specific tests
  python3 scripts/test_integration.py --tests 1,3

  # Output to custom directory
  python3 scripts/test_integration.py --output-dir /tmp/benchmark

  # With custom timeout
  python3 scripts/test_integration.py --timeout 60

For end-to-end Pi skill evaluation (Layer 4), see:
  python3 scripts/skill_eval.py
"""

import sys
import os
import json
import subprocess
import time
import argparse
from datetime import datetime, timezone

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
SCRIPT_PATH = os.path.join(SKILL_DIR, 'scripts', 'arch_upgrade_check.py')
EVALS_JSON = os.path.join(SKILL_DIR, 'evals', 'evals.json')


def load_evals():
    """Load eval definitions from evals.json."""
    with open(EVALS_JSON) as f:
        data = json.load(f)
    return data.get('evals', [])


def resolve_path(path):
    """Resolve path relative to SKILL_DIR if not absolute."""
    if os.path.isabs(path):
        return path
    return os.path.join(SKILL_DIR, path)


def run_script(args, mock_args, timeout=120):
    """
    Run arch_upgrade_check.py with given arguments and mock configuration.
    
    Args:
        args: list of CLI arguments (e.g., ["--json"])
        mock_args: dict with keys pacman_log, checkupdates, http_dir
        timeout: max seconds to wait
        
    Returns:
        dict with exit_code, stdout, stderr, elapsed
    """
    cmd = [sys.executable, SCRIPT_PATH] + args

    # Build mock arguments
    if mock_args.get('pacman_log'):
        cmd += ['--mock-pacman-log', resolve_path(mock_args['pacman_log'])]
    if mock_args.get('checkupdates'):
        cmd += ['--mock-checkupdates', resolve_path(mock_args['checkupdates'])]
    if mock_args.get('http_dir'):
        cmd += ['--mock-http-dir', resolve_path(mock_args['http_dir'])]

    start = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        elapsed = time.time() - start
        return {
            'exit_code': p.returncode,
            'stdout': p.stdout,
            'stderr': p.stderr,
            'elapsed': elapsed,
        }
    except subprocess.TimeoutExpired:
        elapsed = time.time() - start
        return {
            'exit_code': -1,
            'stdout': '',
            'stderr': f'TIMEOUT after {elapsed:.1f}s',
            'elapsed': elapsed,
        }
    except Exception as e:
        elapsed = time.time() - start
        return {
            'exit_code': -2,
            'stdout': '',
            'stderr': str(e),
            'elapsed': elapsed,
        }


def check_assertion(assertion, result):
    """
    Check a single assertion against the script result.
    
    Supported assertion types:
      - exit_code: checks result.exit_code == 0
      - json_valid: stdout parses as JSON
      - json_fields: JSON contains specific keys
      - json_field_value: JSON field equals expected value
      - text_contains: text in stdout/stderr contains expected string
      - timeout: checks elapsed <= max_seconds
    """
    name = assertion['name']
    a_type = assertion.get('type', '')

    try:
        json_data = json.loads(result['stdout']) if result['stdout'].strip() else None
    except json.JSONDecodeError:
        json_data = None

    if a_type == 'exit_code':
        passed = result['exit_code'] == 0
        evidence = f"exit code: {result['exit_code']}"
    
    elif a_type == 'json_valid':
        passed = json_data is not None
        evidence = 'JSON parsed successfully' if passed else 'Not valid JSON'
    
    elif a_type == 'json_fields':
        required = assertion.get('fields', ['status', 'since_date', 'matches'])
        if json_data:
            present = [f for f in required if f in json_data]
            missing = [f for f in required if f not in json_data]
            passed = len(missing) == 0
            evidence = f"present: {present}" if passed else f"missing: {missing}"
        else:
            passed = False
            evidence = 'No JSON data'
    
    elif a_type == 'json_field_value':
        field = assertion.get('field', '')
        expected = assertion.get('expected')
        if json_data:
            actual = json_data.get(field)
            passed = actual == expected
            evidence = f"{field}={actual}" if not passed else f"{field}={actual}"
        else:
            passed = False
            evidence = f'No JSON data for field {field}'
    
    elif a_type == 'text_contains':
        source = assertion.get('source', 'stderr')
        expected = assertion.get('expected', '')
        text = result.get(source, '')
        passed = expected in text
        evidence = f"'{expected}' found in {source}" if passed else f"'{expected}' not found in {source}"
    
    elif a_type == 'timeout':
        max_sec = assertion.get('max_seconds', 120)
        passed = result['elapsed'] <= max_sec
        evidence = f"Completed in {result['elapsed']:.1f}s (max: {max_sec}s)" if passed else f"Timed out at {result['elapsed']:.1f}s (max: {max_sec}s)"
    
    else:
        passed = False
        evidence = f"Unknown assertion type: {a_type}"

    return {
        'name': name,
        'description': assertion.get('description', ''),
        'type': a_type,
        'passed': passed,
        'evidence': evidence,
    }


def grade_eval(eval_def, timeout=120):
    """Run and grade a single eval."""
    eid = eval_def['id']
    name = eval_def['name']
    mock_args = eval_def.get('mock_args', {})
    script_args = eval_def.get('script_args', [])
    assertions = eval_def.get('script_assertions', eval_def.get('assertions', []))

    print(f"  Running test {eid}: {name}...", end=' ', flush=True)
    result = run_script(script_args, mock_args, timeout=timeout)
    print(f"done ({result['elapsed']:.1f}s, exit={result['exit_code']})")

    results = []
    for assertion in assertions:
        check = check_assertion(assertion, result)
        results.append(check)

    passed = sum(1 for r in results if r['passed'])
    failed = sum(1 for r in results if not r['passed'])
    total = len(results)

    return {
        'eval_id': eid,
        'eval_name': name,
        'result': {
            'pass_rate': passed / total if total > 0 else 0,
            'passed': passed,
            'failed': failed,
            'total': total,
            'time_seconds': round(result['elapsed'], 1),
        },
        'expectations': results,
    }


def main():
    parser = argparse.ArgumentParser(description='Run script-level integration tests for archlinux-upgrade-check-skill')
    parser.add_argument('--evals', type=str, default=None,
                        help='Comma-separated eval IDs to run (default: all)')
    parser.add_argument('--tests', type=str, default=None,
                        help='Alias for --evals (comma-separated test IDs)')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Output directory (default: prints to stdout)')
    parser.add_argument('--timeout', type=int, default=120,
                        help='Per-eval timeout in seconds (default: 120)')
    args = parser.parse_args()

    print(f"Arch Linux Upgrade Check Skill — Eval Runner")
    print(f"{'=' * 60}")
    print(f"Skill dir: {SKILL_DIR}")
    print()

    # Support both --evals and --tests flags
    if args.tests and not args.evals:
        args.evals = args.tests

    # Load evals
    evals = load_evals()
    if not evals:
        print("ERROR: No evals found in evals.json")
        sys.exit(1)

    # Filter by IDs if specified
    if args.evals:
        selected_ids = [int(e.strip()) for e in args.evals.split(',')]
        evals = [e for e in evals if e['id'] in selected_ids]
        if not evals:
            print(f"ERROR: No evals match IDs: {args.evals}")
            sys.exit(1)

    print(f"Running {len(evals)} test case(s):")
    for e in evals:
        a_count = len(e.get('script_assertions', e.get('assertions', [])))
        print(f"  T{e['id']}: {e['name']} ({a_count} assertions)")
    print()

    # Run each eval
    runs = []
    total_passed = 0
    total_failed = 0
    total_assertions = 0

    for eval_def in evals:
        run = grade_eval(eval_def, timeout=args.timeout)
        runs.append(run)
        total_passed += run['result']['passed']
        total_failed += run['result']['failed']
        total_assertions += run['result']['total']

    # Summary
    print()
    print(f"{'=' * 60}")
    print(f"Summary: {total_passed}/{total_assertions} passed, {total_failed} failed")
    print(f"Pass rate: {total_passed/total_assertions*100:.0f}%" if total_assertions > 0 else "No assertions")
    print()

    # Build benchmark output
    benchmark = {
        'metadata': {
            'skill_name': 'archlinux-upgrade-check-skill',
            'skill_path': SKILL_DIR,
            'timestamp': datetime.now(timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ'),
            'evals_run': [e['name'] for e in evals],
            'runs_per_configuration': 1,
            'mock_data': True,
            'note': 'Script-level integration test with mock data — fully deterministic, no network required. For pi subagent (with/without skill) testing, see test-plan.md → Layer 4 or scripts/skill_eval.py',
        },
        'runs': runs,
        'run_summary': {
            'pass_rate': {
                'mean': total_passed / total_assertions if total_assertions > 0 else 0,
            },
            'time_seconds': {
                'mean': sum(r['result']['time_seconds'] for r in runs) / len(runs) if runs else 0,
            },
        },
        'notes': [
            'All tests use mock data (evals/mock/) — results are deterministic and reproducible.',
            'These tests validate script-level correctness: parsing, matching, JSON output, lookback cap.',
            'For end-to-end LLM + skill orchestration tests, use: python3 scripts/skill_eval.py',
        ],
    }

    # Output
    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        path = os.path.join(args.output_dir, 'benchmark.json')
        with open(path, 'w') as f:
            json.dump(benchmark, f, indent=2, ensure_ascii=False)
        print(f"Benchmark written to: {path}")

        # Also write a Markdown summary
        md_path = os.path.join(args.output_dir, 'benchmark.md')
        with open(md_path, 'w') as f:
            f.write("# Benchmark Results\n\n")
            f.write(f"Run at: {benchmark['metadata']['timestamp']}\n\n")
            f.write("| Eval | Passed | Failed | Total | Rate | Time |\n")
            f.write("|------|--------|--------|-------|------|------|\n")
            for r in runs:
                res = r['result']
                f.write(f"| T{r['eval_id']} {r['eval_name']} | {res['passed']} | {res['failed']} | {res['total']} | {res['pass_rate']*100:.0f}% | {res['time_seconds']}s |\n")
            f.write(f"\n**Total**: {total_passed}/{total_assertions} passed ({total_passed/total_assertions*100:.0f}%)\n")
            f.write("\n*Mock data: yes — fully deterministic, no network required.*\n")
        print(f"Summary written to: {md_path}")
    else:
        print(json.dumps(benchmark, indent=2, ensure_ascii=False))

    # Exit code
    sys.exit(1 if total_failed > 0 else 0)


if __name__ == '__main__':
    main()
