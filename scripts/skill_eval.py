#!/usr/bin/env python3
"""
skill_eval.py - Arch Linux Upgrade Check — End-to-End Skill Evaluation (Layer 4)

Runs eval prompts through pi -p --skill, then grades the LLM's output
against skill_assertions from evals.json.

Mock data is provided via ARCH_CHECK_MOCK_DIR environment variable. The LLM
uses predetermined mock data — it doesn't know it's being tested.

Usage:
  # Run all evals
  python3 scripts/skill_eval.py --model opencode-go/deepseek-chat

  # Specific evals
  python3 scripts/skill_eval.py --model ... --evals 1,3

  # Output to directory
  python3 scripts/skill_eval.py --model ... --output-dir /tmp/skill-eval
"""

import sys
import os
import json
import subprocess
import time
import argparse
import math

SKILL_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EVALS_JSON = os.path.join(SKILL_DIR, 'evals', 'evals.json')
PI_CMD = 'pi'

# ──────────────────────────────────────────────────────
# Assertion handlers
# ──────────────────────────────────────────────────────

LLM_ASSERTION_HANDLERS = {}


def register_handler(atype):
    def wrapper(fn):
        LLM_ASSERTION_HANDLERS[atype] = fn
        return fn
    return wrapper


@register_handler('exit_code')
def _check_exit_code(result, _assertion):
    passed = result['exit_code'] == 0
    return passed, f"pi exit code: {result['exit_code']}"


@register_handler('text_contains')
def _check_text_contains(result, assertion):
    expected = assertion.get('expected', '')
    source = assertion.get('source', 'stdout')
    text = result.get(source, '')
    passed = expected in text
    if passed:
        return True, f"'{expected}' found in {source}"
    else:
        return False, f"'{expected}' not found in {source}"


@register_handler('timeout')
def _check_timeout(result, assertion):
    max_sec = assertion.get('max_seconds', 300)
    passed = result['elapsed'] <= max_sec
    return passed, f"Completed in {result['elapsed']:.1f}s (max: {max_sec}s)"


def check_llm_assertion(assertion, result):
    """Check a single assertion against the LLM's output."""
    name = assertion.get('name', 'unnamed')
    atype = assertion.get('type', '')

    handler = LLM_ASSERTION_HANDLERS.get(atype)
    if handler:
        try:
            passed, evidence = handler(result, assertion)
        except Exception as e:
            passed = False
            evidence = f"Error: {e}"
    else:
        passed = False
        evidence = f"Unknown assertion type: {atype}"

    return {
        'name': name,
        'description': assertion.get('description', ''),
        'type': atype,
        'passed': passed,
        'evidence': evidence,
    }


# ──────────────────────────────────────────────────────
# Eval loading
# ──────────────────────────────────────────────────────

def load_evals():
    with open(EVALS_JSON) as f:
        data = json.load(f)
    return data.get('evals', [])


def resolve_path(path):
    if os.path.isabs(path):
        return path
    return os.path.join(SKILL_DIR, path)


# ──────────────────────────────────────────────────────
# pi -p --skill runner
# ──────────────────────────────────────────────────────

def run_pi(prompt, model, skill_path, mock_dir, timeout=300):
    cmd = [PI_CMD, '-p', '--model', model]
    cmd += ['--skill', skill_path]
    cmd.append(prompt)

    env = os.environ.copy()
    mock_dir_resolved = resolve_path(mock_dir) if mock_dir else None
    if mock_dir_resolved and os.path.isdir(mock_dir_resolved):
        env['ARCH_CHECK_MOCK_DIR'] = mock_dir_resolved
    env['PI_OFFLINE'] = '1'

    start = time.time()
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=env)
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
    except FileNotFoundError:
        return {
            'exit_code': -2,
            'stdout': '',
            'stderr': f'ERROR: "{PI_CMD}" not found',
            'elapsed': time.time() - start,
        }
    except Exception as e:
        return {
            'exit_code': -3,
            'stdout': '',
            'stderr': str(e),
            'elapsed': time.time() - start,
        }


# ──────────────────────────────────────────────────────
# Grading
# ──────────────────────────────────────────────────────

def grade_eval(eval_def, model, skill_path, timeout=300):
    """Run one eval through pi -p --skill and grade the LLM output."""
    eid = eval_def['id']
    name = eval_def['name']
    prompt = eval_def['prompt']
    mock_args = eval_def.get('mock_args', {})
    assertions = eval_def.get('skill_assertions', eval_def.get('assertions', []))

    mock_dir = None
    if mock_args.get('http_dir'):
        http_dir = resolve_path(mock_args['http_dir'])
        mock_dir = os.path.dirname(http_dir)
    elif mock_args.get('pacman_log'):
        mock_dir = os.path.dirname(resolve_path(mock_args['pacman_log']))

    print(f"  E{eid}: {name}...", end=' ', flush=True)
    result = run_pi(prompt, model, skill_path, mock_dir, timeout=timeout)
    print(f"done ({result['elapsed']:.1f}s, exit={result['exit_code']})")

    results = []
    for assertion in assertions:
        check = check_llm_assertion(assertion, result)
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
        'llm_output_preview': result['stdout'][:500] if result['stdout'] else '(empty)',
    }


# ──────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='End-to-end skill evaluation for archlinux-upgrade-check-skill (Layer 4)')
    parser.add_argument('--model', type=str, required=True,
                        help='Model to use (e.g., opencode-go/deepseek-chat)')
    parser.add_argument('--evals', type=str, default=None,
                        help='Comma-separated eval IDs to run (default: all)')
    parser.add_argument('--skill', type=str, default=None,
                        help='Path to skill file/directory (default: SKILL_DIR)')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Output directory (default: stdout only)')
    parser.add_argument('--timeout', type=int, default=300,
                        help='Per-eval timeout in seconds (default: 300)')
    args = parser.parse_args()

    skill_path = args.skill or SKILL_DIR

    print(f"Arch Linux Upgrade Check — Skill Evaluation (Layer 4)")
    print(f"{'=' * 50}")
    print(f"Model:  {args.model}")
    print(f"Skill:  {skill_path}")
    print(f"Timeout: {args.timeout}s per eval")
    print()

    evals = load_evals()
    if not evals:
        print("ERROR: No evals found in evals.json")
        sys.exit(1)
    if args.evals:
        selected = [int(e.strip()) for e in args.evals.split(',')]
        evals = [e for e in evals if e['id'] in selected]
        if not evals:
            print(f"ERROR: No evals match IDs: {args.evals}")
            sys.exit(1)

    print(f"Running {len(evals)} eval(s):")
    for e in evals:
        a_count = len(e.get('skill_assertions', e.get('assertions', [])))
        print(f"  E{e['id']}: {e['name']} ({a_count} assertions)")
    print()

    runs = []
    total_passed = 0
    total_failed = 0
    total_assertions = 0

    for eval_def in evals:
        run = grade_eval(eval_def, args.model, skill_path, timeout=args.timeout)
        runs.append(run)
        total_passed += run['result']['passed']
        total_failed += run['result']['failed']
        total_assertions += run['result']['total']

    print()
    print(f"{'=' * 50}")
    print(f"Summary: {total_passed}/{total_assertions} passed, {total_failed} failed")
    if total_assertions > 0:
        print(f"Pass rate: {total_passed/total_assertions*100:.0f}%")
    print()

    timestamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    passes = [r['result']['pass_rate'] for r in runs]
    times = [r['result']['time_seconds'] for r in runs]

    def mean(vals):
        return sum(vals) / len(vals) if vals else 0

    benchmark = {
        'metadata': {
            'test_name': 'archlinux-upgrade-check-skill-skill-eval',
            'model': args.model,
            'timestamp': timestamp,
            'evals_run': [e['name'] for e in evals],
            'mock_data': True,
            'note': 'Skill evaluation via pi -p --skill. Tests LLM + SKILL.md orchestration with mock data.',
        },
        'runs': runs,
        'summary': {
            'passed': total_passed,
            'failed': total_failed,
            'total': total_assertions,
            'pass_rate': round(total_passed / total_assertions, 2) if total_assertions > 0 else 0,
            'time_seconds': round(mean(times), 1),
        },
        'notes': [
            'Uses ARCH_CHECK_MOCK_DIR env var for deterministic mock data.',
            'Tests that LLM + SKILL.md can correctly invoke the script and interpret results.',
        ],
    }

    if args.output_dir:
        os.makedirs(args.output_dir, exist_ok=True)
        path = os.path.join(args.output_dir, 'benchmark.json')
        with open(path, 'w') as f:
            json.dump(benchmark, f, indent=2, ensure_ascii=False)
        print(f"Results written to: {path}")

        md_path = os.path.join(args.output_dir, 'benchmark.md')
        with open(md_path, 'w') as f:
            f.write("# Skill Evaluation Results (Layer 4)\n\n")
            f.write(f"Model: {args.model}  |  Run at: {timestamp}\n\n")
            f.write("| Eval | Passed | Failed | Total | Rate | Time |\n")
            f.write("|------|--------|--------|-------|------|------|\n")
            for r in runs:
                res = r['result']
                f.write(f"| E{r['eval_id']} {r['eval_name']} | {res['passed']} | {res['failed']} | {res['total']} | {res['pass_rate']*100:.0f}% | {res['time_seconds']}s |\n")
            f.write(f"\n**Total**: {total_passed}/{total_assertions} ({total_passed/total_assertions*100:.0f}%)\n\n")
            f.write("### Output Preview\n\n")
            for r in runs:
                f.write(f"**E{r['eval_id']} {r['eval_name']}**\n\n```\n{r.get('llm_output_preview', '(empty)')}\n```\n\n")
        print(f"Summary written to: {md_path}")
    else:
        print(json.dumps(benchmark, indent=2, ensure_ascii=False))

    sys.exit(1 if total_failed > 0 else 0)


if __name__ == '__main__':
    main()
