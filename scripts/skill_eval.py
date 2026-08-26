#!/usr/bin/env python3
"""
skill_eval.py - Arch Linux Upgrade Check — End-to-End Skill Evaluation (Layer 4)

Runs eval prompts through pi -p --skill, then grades the LLM's output
against skill_assertions from evals.json.

Mock data is provided via ARCH_CHECK_MOCK_DIR environment variable. The LLM
uses predetermined mock data — it doesn't know it's being tested.

Usage:
  # Run all evals
  python3 scripts/skill_eval.py --model <your-model>

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


@register_handler('text_contains_any')
def _check_text_contains_any(result, assertion):
    expected = assertion.get('expected', [])
    source = assertion.get('source', 'stdout')
    text = result.get(source, '')
    if isinstance(expected, str):
        expected = [expected]
    matches = [kw for kw in expected if kw in text]
    if matches:
        return True, f"matched {matches} in {source}"
    else:
        return False, f"none of {expected} found in {source}"


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

def run_pi(prompt, model, skill_path, mock_dir, timeout=300, use_skill=True):
    cmd = [PI_CMD, '-p', '--model', model]
    if use_skill and skill_path:
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

def grade_eval(eval_def, model, skill_path, timeout=300, use_skill=True, repeat=1):
    """Run one eval through pi -p (with or without skill) and grade the LLM output.

    When repeat > 1, runs the same prompt that many times to dampen LLM output
    variance and reports the mean pass rate and timing.
    """
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

    label = 'with_skill' if use_skill else 'baseline'
    runs = []
    for i in range(repeat):
        tag = f"{label}#{i + 1}" if repeat > 1 else label
        print(f"  E{eid}: {name} [{tag}]...", end=' ', flush=True)
        result = run_pi(prompt, model, skill_path, mock_dir, timeout=timeout, use_skill=use_skill)
        print(f"done ({result['elapsed']:.1f}s, exit={result['exit_code']})")

        checked = [check_llm_assertion(a, result) for a in assertions]
        passed = sum(1 for r in checked if r['passed'])
        total = len(checked)
        runs.append({
            'pass_rate': passed / total if total > 0 else 0,
            'passed': passed,
            'failed': total - passed,
            'total': total,
            'time_seconds': round(result['elapsed'], 1),
            'expectations': checked,
            'llm_output_preview': result['stdout'][:500] if result['stdout'] else '(empty)',
        })

    # Aggregate across repeats
    import statistics
    pass_rates = [r['pass_rate'] for r in runs]
    times = [r['time_seconds'] for r in runs]
    mean_pr = sum(pass_rates) / len(pass_rates) if pass_rates else 0
    stdev_pr = statistics.stdev(pass_rates) if len(pass_rates) >= 2 else 0.0

    return {
        'eval_id': eid,
        'eval_name': name,
        'configuration': label,
        'result': {
            'pass_rate': round(mean_pr, 2),
            'pass_rate_stdev': round(stdev_pr, 2),
            'passed': sum(r['passed'] for r in runs),
            'failed': sum(r['failed'] for r in runs),
            'total': runs[0]['total'] if runs else 0,
            'time_seconds': round(sum(times) / len(times), 1) if times else 0,
            'repeat': repeat,
        },
        'runs': runs,
    }


# ──────────────────────────────────────────────────────
# CLI
# ──────────────────────────────────────────────────────

def main():
    parser = argparse.ArgumentParser(
        description='End-to-end skill evaluation for archlinux-upgrade-check-skill (Layer 4)')
    parser.add_argument('--model', type=str, required=True,
                        help='Model to use (e.g., <your-model>)')
    parser.add_argument('--evals', type=str, default=None,
                        help='Comma-separated eval IDs to run (default: all)')
    parser.add_argument('--skill', type=str, default=None,
                        help='Path to skill file/directory (default: SKILL_DIR)')
    parser.add_argument('--baseline', action='store_true',
                        help='Also run each eval WITHOUT the skill (no --skill), so you can compare the skill\'s incremental value. The skill scripts remain on disk for the baseline to discover.')
    parser.add_argument('--repeat', type=int, default=1,
                        help='Run each eval N times to dampen LLM variance; report mean pass rate (default: 1)')
    parser.add_argument('--output-dir', type=str, default=None,
                        help='Output directory (default: stdout only)')
    parser.add_argument('--timeout', type=int, default=300,
                        help='Per-eval timeout in seconds (default: 300)')
    args = parser.parse_args()

    skill_path = args.skill or SKILL_DIR

    print(f"Arch Linux Upgrade Check — Skill Evaluation (Layer 4)")
    print(f"{'=' * 60}")
    print(f"Model:    {args.model}")
    print(f"Skill:    {skill_path}")
    print(f"Timeout:  {args.timeout}s per run")
    print(f"Repeat:   {args.repeat}x")
    print(f"Baseline: {'yes (with-skill vs no-skill)' if args.baseline else 'no (with-skill only)'}")
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

    all_runs = []
    for eval_def in evals:
        all_runs.append(grade_eval(eval_def, args.model, skill_path,
                                   timeout=args.timeout, use_skill=True, repeat=args.repeat))
        if args.baseline:
            all_runs.append(grade_eval(eval_def, args.model, skill_path,
                                       timeout=args.timeout, use_skill=False, repeat=args.repeat))

    # Summary per configuration
    by_config = {}
    for r in all_runs:
        by_config.setdefault(r['configuration'], []).append(r)

    print()
    print(f"{'=' * 60}")
    print("Summary by configuration:")
    for cfg, rs in by_config.items():
        tp = sum(r['result']['passed'] for r in rs)
        tf = sum(r['result']['failed'] for r in rs)
        tt = sum(r['result']['total'] for r in rs)
        pr = tp / tt * 100 if tt else 0
        print(f"  {cfg:12}: {tp}/{tt} passed ({pr:.0f}%)")
    if args.baseline and 'with_skill' in by_config and 'baseline' in by_config:
        ws = sum(r['result']['passed'] for r in by_config['with_skill'])
        wt = sum(r['result']['total'] for r in by_config['with_skill'])
        bs = sum(r['result']['passed'] for r in by_config['baseline'])
        bt = sum(r['result']['total'] for r in by_config['baseline'])
        delta = (ws / wt - bs / bt) * 100 if wt and bt else 0
        print(f"  delta:     {delta:+.0f} percentage points (skill vs no-skill)")
    print()

    timestamp = time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime())

    def mean(vals):
        return sum(vals) / len(vals) if vals else 0

    benchmark = {
        'metadata': {
            'test_name': 'archlinux-upgrade-check-skill-skill-eval',
            'model': args.model,
            'timestamp': timestamp,
            'evals_run': [e['name'] for e in evals],
            'repeat': args.repeat,
            'baseline': args.baseline,
            'mock_data': True,
            'note': 'Skill evaluation via pi -p. with_skill uses --skill; baseline omits --skill (scripts still on disk).',
        },
        'runs': all_runs,
        'summary': {cfg: {
            'passed': sum(r['result']['passed'] for r in rs),
            'failed': sum(r['result']['failed'] for r in rs),
            'total': sum(r['result']['total'] for r in rs),
            'pass_rate': round(sum(r['result']['passed'] for r in rs) / sum(r['result']['total'] for r in rs), 2) if sum(r['result']['total'] for r in rs) else 0,
            'time_seconds': round(mean([r['result']['time_seconds'] for r in rs]), 1),
        } for cfg, rs in by_config.items()},
        'notes': [
            'Uses ARCH_CHECK_MOCK_DIR env var for deterministic mock data.',
            'with_skill tests LLM + SKILL.md orchestration; baseline tests the same prompt without the skill loaded.',
            'When repeat > 1, pass_rate is the mean across repeats; pass_rate_stdev shows variance.',
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
            f.write(f"Model: {args.model}  |  Run at: {timestamp}  |  Repeat: {args.repeat}x\n\n")
            f.write("| Eval | Configuration | Passed | Failed | Total | Rate | Time |\n")
            f.write("|------|---------------|--------|--------|-------|------|------|\n")
            for r in all_runs:
                res = r['result']
                stdev = f" ±{res.get('pass_rate_stdev', 0)*100:.0f}%" if args.repeat > 1 else ""
                f.write(f"| E{r['eval_id']} {r['eval_name']} | {r['configuration']} | {res['passed']} | {res['failed']} | {res['total']} | {res['pass_rate']*100:.0f}%{stdev} | {res['time_seconds']}s |\n")
            f.write("\n### Output Previews\n\n")
            for r in all_runs:
                preview = r['runs'][0].get('llm_output_preview', '(empty)') if r.get('runs') else r.get('llm_output_preview', '(empty)')
                header = "**E{} {} [{}]**".format(r['eval_id'], r['eval_name'], r['configuration'])
                f.write(header + chr(10) + chr(10) + "```" + chr(10) + preview + chr(10) + "```" + chr(10) + chr(10))
        print(f"Summary written to: {md_path}")
    else:
        print(json.dumps(benchmark, indent=2, ensure_ascii=False))

    total_failed = sum(r['result']['failed'] for r in all_runs)
    sys.exit(1 if total_failed > 0 else 0)


if __name__ == '__main__':
    main()
