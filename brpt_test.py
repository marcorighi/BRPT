#!/usr/bin/env python3
"""Unified and explicit command-line interface for the BRPT test suites."""
from __future__ import annotations
import argparse
import bz2
import csv
import gzip
import hashlib
import html
import importlib.util
import json
import math
import multiprocessing as mp
import os
import platform
import re
import signal
import shutil
import sys
import textwrap
import time
import traceback
from collections import Counter
from collections.abc import Callable, Iterable, Sequence
from pathlib import Path
from types import FunctionType, ModuleType
from typing import Any, TextIO, cast
file_NUMBER_RE = re.compile('^\\s*(\\d+)')
file_BRPTFunction = Callable[[int], bool]
file__BRPT: file_BRPTFunction | None = None

def file_load_brpt(module_path: str) -> file_BRPTFunction:
    spec = importlib.util.spec_from_file_location('brpt_target', module_path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load module: {module_path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    brpt_function = getattr(module, 'brpt', None)
    if not callable(brpt_function):
        raise RuntimeError('The module does not expose a brpt(n) function')
    return cast(file_BRPTFunction, brpt_function)

def file_worker_init(module_path: str) -> None:
    global file__BRPT
    file__BRPT = file_load_brpt(module_path)

def file_get_brpt() -> file_BRPTFunction:
    """Returns the BRPT function initialized in the worker process."""
    if file__BRPT is None:
        raise RuntimeError('BRPT worker not initialized')
    return file__BRPT

def file_test_number(item: tuple[int, str]) -> dict:
    n, expected = item
    started = time.perf_counter()
    error = ''
    try:
        actual = 'PRIME' if file_get_brpt()(n) else 'COMPOSITE'
    except Exception as exc:
        actual = 'ERROR'
        error = f'{type(exc).__name__}: {exc}'
    elapsed_ms = (time.perf_counter() - started) * 1000.0
    return {'n': str(n), 'expected': expected, 'actual': actual, 'match': actual == expected, 'elapsed_ms': f'{elapsed_ms:.6f}', 'error': error}

def file_open_text(path: Path):
    suffix = path.suffix.lower()
    if suffix == '.gz':
        return gzip.open(path, 'rt', encoding='utf-8', errors='ignore')
    if suffix == '.bz2':
        return bz2.open(path, 'rt', encoding='utf-8', errors='ignore')
    return path.open('rt', encoding='utf-8', errors='ignore')

def file_read_numbers(path: Path, sample_every: int, limit: int | None, display_name: str):
    valid_lines = 0
    yielded = 0
    with file_open_text(path) as handle:
        for line_number, line in enumerate(handle, 1):
            match = file_NUMBER_RE.match(line)
            if match is None:
                continue
            valid_lines += 1
            if (valid_lines - 1) % sample_every:
                continue
            try:
                yield int(match.group(1))
            except ValueError:
                print(f'[{display_name}] Line {line_number:_} skipped: invalid integer', file=sys.stderr)
                continue
            yielded += 1
            if limit is not None and yielded >= limit:
                return

def file_main() -> None:
    parser = argparse.ArgumentParser(description='Parallel test of numbers with brpt.py')
    parser.add_argument('input_file', type=Path, help='.gz, .bz2, or text file')
    parser.add_argument('--brpt-module', type=Path, default=Path(__file__).with_name('brpt.py'), help='Module containing brpt(n) (default: brpt.py next to this script)')
    parser.add_argument('--output-dir', type=Path, default=Path('brpt_test_results'), help='Results directory')
    parser.add_argument('--expected', choices=('COMPOSITE', 'PRIME'), default='COMPOSITE', help='Expected result for all numbers (default: COMPOSITE)')
    parser.add_argument('--workers', type=int, default=None)
    parser.add_argument('--chunksize', type=int, default=64)
    parser.add_argument('--progress-every', type=int, default=5000)
    parser.add_argument('--sample-every', type=int, default=1)
    parser.add_argument('--limit', type=int, default=None)
    args = parser.parse_args()
    if not args.input_file.is_file():
        parser.error(f'Input file not found: {args.input_file}')
    if not args.brpt_module.is_file():
        parser.error(f'BRPT module not found: {args.brpt_module}')
    if args.workers is not None and args.workers < 1:
        parser.error('--workers must be >= 1')
    if args.chunksize < 1 or args.sample_every < 1:
        parser.error('--chunksize and --sample-every must be >= 1')
    if args.limit is not None and args.limit < 1:
        parser.error('--limit must be >= 1')
    module_path = str(args.brpt_module.resolve())
    file_load_brpt(module_path)
    args.output_dir.mkdir(parents=True, exist_ok=True)
    input_name = re.sub('[^A-Za-z0-9_-]+', '_', args.input_file.stem).strip('_')
    if not input_name:
        input_name = 'input'
    results_path = args.output_dir / f'results_{input_name}.csv'
    counterexamples_path = args.output_dir / f'counterexamples_{input_name}.csv'
    summary_path = args.output_dir / f'summary_{input_name}.json'
    fields = ('n', 'expected', 'actual', 'match', 'elapsed_ms', 'error')
    tested = mismatches = errors = 0
    max_elapsed_ms = 0.0
    started = time.perf_counter()
    tasks = ((n, args.expected) for n in file_read_numbers(args.input_file, args.sample_every, args.limit, input_name))
    with results_path.open('w', newline='', encoding='utf-8') as results_file, counterexamples_path.open('w', newline='', encoding='utf-8') as bad_file, mp.Pool(args.workers, initializer=file_worker_init, initargs=(module_path,)) as pool:
        results = csv.DictWriter(results_file, fieldnames=fields)
        bad = csv.DictWriter(bad_file, fieldnames=fields)
        results.writeheader()
        bad.writeheader()
        for record in pool.imap_unordered(file_test_number, tasks, args.chunksize):
            tested += 1
            results.writerow(record)
            max_elapsed_ms = max(max_elapsed_ms, float(record['elapsed_ms']))
            if record['actual'] == 'ERROR':
                errors += 1
            if not record['match']:
                mismatches += 1
                bad.writerow(record)
            if args.progress_every and tested % args.progress_every == 0:
                elapsed = time.perf_counter() - started
                print(f'[{input_name}] tested={tested:_} mismatch={mismatches:_} errors={errors:_} rate={tested / elapsed:_.1f} n/s', file=sys.stderr, flush=True)
    elapsed = time.perf_counter() - started
    summary = {'input_file': str(args.input_file.resolve()), 'brpt_module': module_path, 'expected': args.expected, 'tested': tested, 'mismatches': mismatches, 'errors': errors, 'elapsed_seconds': round(elapsed, 3), 'rate_per_second': round(tested / elapsed, 3) if elapsed else 0.0, 'max_elapsed_ms': round(max_elapsed_ms, 6)}
    summary_path.write_text(json.dumps(summary, indent=2), encoding='utf-8')
    print(f'[{input_name}] done: tested={tested:_} mismatch={mismatches:_} errors={errors:_} time={elapsed:_.3f} s rate={(tested / elapsed if elapsed else 0.0):_.1f} n/s max={max_elapsed_ms:_.6f} ms')
    print(f'[{input_name}] results: {args.output_dir.resolve()}')
ring_Record = dict[str, Any]
ring_PrimePredicate = Callable[[int], bool]
ring_RingLimit = Callable[[int], int]

def ring_uninitialized_function(_n: int) -> bool:
    """Raise an explicit error if a worker function is used too early."""
    raise RuntimeError('Worker process was not initialized')

def ring_uninitialized_limit(_n: int) -> int:
    """Raise an explicit error if the BRPT ring limit is used too early."""
    raise RuntimeError('BRPT ring-limit function was not initialized')

ring_BRPT: ring_PrimePredicate = ring_uninitialized_function
ring_ISPRIME: ring_PrimePredicate = ring_uninitialized_function
ring_AVAILABLE_RING: ring_RingLimit = ring_uninitialized_limit
ring_LAST_A: int | None = None
ring_LAST_B: int | None = None
ring_ALLOW_PROBABLE_REFERENCE = False
ring_STOP_REQUESTED = False
ring_GENERATOR = 'integers_congruent_1_or_5_mod_6'
ring_STATE_VERSION = 4
ring_SYMPY_DEFINITIVE_LIMIT = 1 << 64
ring_FIELDS = ('index', 'n', 'residue_mod_6', 'actual', 'passed', 'sympy_result', 'sympy_prime', 'match', 'mismatch_type', 'reference_certainty', 'a', 'b', 'ring', 'elapsed_ms', 'error')
ring_STAT_KEYS = ('tested', 'accepted', 'rejected', 'errors', 'records_without_pair', 'verified', 'matches', 'mismatches', 'false_positives', 'false_negatives', 'sympy_primes', 'sympy_composites')

def ring_load_brpt(path: str) -> ring_PrimePredicate:
    """Load BRPT, instrument ``find_pair``, and expose its available ring."""
    global ring_AVAILABLE_RING

    spec = importlib.util.spec_from_file_location('brpt_tracked', path)
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load module: {path}')
    module: ModuleType = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    raw_brpt = module.__dict__.get('brpt')
    raw_find = module.__dict__.get('find_pair')
    if not isinstance(raw_brpt, FunctionType):
        raise RuntimeError('The module does not expose brpt(n)')
    if not isinstance(raw_find, FunctionType):
        raise RuntimeError('The module does not expose find_pair(n)')
    brpt_callable = cast(Callable[[int], Any], raw_brpt)
    find_callable = cast(Callable[[int], Any], raw_find)

    # Prefer a limit function exported by brpt.py. This keeps the displayed
    # ring synchronized when the window formula is changed in the BRPT module.
    raw_limit = None
    limit_name = ''
    for candidate_name in (
        'available_ring',
        'ring_limit',
        'window_radius',
        'search_radius',
        'search_limit',
        '_limit_for',
    ):
        candidate = module.__dict__.get(candidate_name)
        if callable(candidate):
            raw_limit = cast(Callable[[int], Any], candidate)
            limit_name = candidate_name
            break

    if raw_limit is not None:
        def available_ring(n: int) -> int:
            value = int(raw_limit(n))
            if value < 1:
                raise RuntimeError(
                    f'{limit_name}(n) returned invalid ring {value}'
                )
            return value
    else:
        # Compatibility fallback synchronized with the window formula
        # embedded directly in the current brpt.py:
        #
        #   for radius in range(
        #       1,
        #       max(
        #           5,
        #           3 + floor(
        #               1.3 * isqrt(int(n.bit_length() * LN2))
        #           ) + 1,
        #       ),
        #   ):
        #
        # Since range() excludes its upper bound, the largest ring actually
        # tested is:
        #
        #   max(
        #       4,
        #       3 + floor(
        #           1.3 * isqrt(int(n.bit_length() * LN2))
        #       ),
        #   )
        #
        # LN2 is read directly from the loaded brpt.py module.
        module_ln2 = float(module.__dict__.get('LN2', math.log(2.0)))

        def available_ring(n: int) -> int:
            if n < 1:
                return 4

            logarithm_floor = int(n.bit_length() * module_ln2)
            logarithm_root = math.isqrt(logarithm_floor)

            return max(
                4,
                3 + math.floor(1.3 * logarithm_root),
            )

    ring_AVAILABLE_RING = available_ring

    def tracked_find_pair(n: int):
        """Call BRPT's search function and retain only its coefficient pair."""
        global ring_LAST_A, ring_LAST_B
        result = find_callable(n)
        ring_LAST_A = None
        ring_LAST_B = None
        if result is not None:
            try:
                pair = result[0]
                ring_LAST_A = int(pair[0])
                ring_LAST_B = int(pair[1])
            except (IndexError, TypeError, ValueError) as exc:
                raise RuntimeError('find_pair(n) returned an invalid result') from exc
        return result
    module.find_pair = tracked_find_pair

    def brpt_function(n: int) -> bool:
        return bool(brpt_callable(n))
    return brpt_function

def ring_load_sympy_isprime() -> ring_PrimePredicate:
    """Return a concrete Boolean wrapper around ``sympy.isprime``."""
    try:
        from sympy import isprime
    except ImportError as exc:
        raise RuntimeError('SymPy is not installed. Install it with: python -m pip install sympy') from exc

    def predicate(n: int) -> bool:
        return bool(isprime(n))
    return predicate

def ring_request_stop(_signum, _frame) -> None:
    """Request an orderly shutdown without raising ``KeyboardInterrupt``.

    The main process completes the current result, terminates the worker pool,
    flushes all open CSV streams, and writes the final checkpoint. Repeated
    interrupt signals leave the shutdown sequence undisturbed.
    """
    global ring_STOP_REQUESTED
    if not ring_STOP_REQUESTED:
        ring_STOP_REQUESTED = True
        print('\nInterrupt requested: completing the current result and saving...', flush=True)

def ring_worker_init(module_path: str, allow_probable_reference: bool) -> None:
    global ring_BRPT, ring_ISPRIME, ring_ALLOW_PROBABLE_REFERENCE
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    ring_BRPT = ring_load_brpt(module_path)
    ring_ISPRIME = ring_load_sympy_isprime()
    ring_ALLOW_PROBABLE_REFERENCE = allow_probable_reference

def ring_test_candidate(item: tuple[int, int]) -> ring_Record:
    """Run BRPT and SymPy and return a record ready for CSV and JSON output."""
    global ring_LAST_A, ring_LAST_B
    index, n = item
    ring_LAST_A = None
    ring_LAST_B = None
    started = time.perf_counter()
    certainty = 'DEFINITIVE' if n < ring_SYMPY_DEFINITIVE_LIMIT else 'PROBABLE'
    try:
        if n >= ring_SYMPY_DEFINITIVE_LIMIT and (not ring_ALLOW_PROBABLE_REFERENCE):
            raise RuntimeError('n >= 2**64: use --allow-probable-reference to continue')
        passed = ring_BRPT(n)
        sympy_prime = ring_ISPRIME(n)
        match = passed == sympy_prime
        if match:
            mismatch_type = ''
        elif passed:
            mismatch_type = 'FALSE_POSITIVE'
        else:
            mismatch_type = 'FALSE_NEGATIVE'
        actual = 'PRIME' if passed else 'COMPOSITE'
        sympy_result = 'PRIME' if sympy_prime else 'COMPOSITE'
        error = ''
    except Exception as exc:
        passed = False
        sympy_prime = ''
        match = False
        mismatch_type = 'ERROR'
        actual = 'ERROR'
        sympy_result = 'ERROR'
        error = f'{type(exc).__name__}: {exc}'
    a: int | str = ''
    b: int | str = ''
    ring: int | str = ''
    if ring_LAST_A is not None and ring_LAST_B is not None:
        a_value = int(ring_LAST_A)
        b_value = int(ring_LAST_B)
        a = a_value
        b = b_value
        ring = max(abs(a_value), abs(b_value))
    return {'index': index, 'n': str(n), 'residue_mod_6': n % 6, 'actual': actual, 'passed': passed, 'sympy_result': sympy_result, 'sympy_prime': sympy_prime, 'match': match, 'mismatch_type': mismatch_type, 'reference_certainty': certainty, 'a': a, 'b': b, 'ring': ring, 'elapsed_ms': f'{(time.perf_counter() - started) * 1000:.6f}', 'error': error}

def ring_generate_candidates(start: int, count: int | None, first_index: int):
    """Generate integers n > start congruent to 1 or 5 modulo 6, in order."""
    n = start + 1
    while n % 6 not in (1, 5):
        n += 1
    generated = 0
    while count is None or generated < count:
        yield first_index + generated, n
        generated += 1
        n += 4 if n % 6 == 1 else 2

def ring_create_statistics() -> dict[str, int]:
    return {key: 0 for key in ring_STAT_KEYS}

def ring_encode_counter(counter: Counter) -> dict[str, int]:
    return {f'{key[0]},{key[1]}' if isinstance(key, tuple) else str(key): value for key, value in counter.items()}

def ring_atomic_write_json(path: Path, data: dict) -> None:
    temporary = path.with_suffix(path.suffix + '.tmp')
    temporary.write_text(json.dumps(data, indent=2), encoding='utf-8')
    temporary.replace(path)

def ring_build_checkpoint_data(last_candidate: int, stats: dict, pair_first: dict, ring_first: dict, pair_counts: Counter, ring_counts: Counter) -> dict:
    return {
        'state_version': ring_STATE_VERSION,
        'generator': ring_GENERATOR,
        'last_candidate': last_candidate,
        'available_ring': ring_AVAILABLE_RING(last_candidate),
        **stats,
        'reference': 'sympy.isprime',
        'sympy_definitive_below': ring_SYMPY_DEFINITIVE_LIMIT,
        'pair_first': {
            f'{a},{b}': record for (a, b), record in pair_first.items()
        },
        'ring_first': {
            str(ring): record for ring, record in ring_first.items()
        },
        'pair_counts': ring_encode_counter(pair_counts),
        'ring_counts': ring_encode_counter(ring_counts),
    }

def ring_save_checkpoint(path: Path, last_candidate: int, stats: dict, pair_first: dict, ring_first: dict, pair_counts: Counter, ring_counts: Counter) -> None:
    ring_atomic_write_json(path, ring_build_checkpoint_data(last_candidate, stats, pair_first, ring_first, pair_counts, ring_counts))

def ring_load_checkpoint(path: Path):
    """Load an English-only version-4 checkpoint."""
    data = json.loads(path.read_text(encoding='utf-8'))
    if data.get('generator') != ring_GENERATOR:
        raise RuntimeError('Incompatible checkpoint generator; use another output directory or pass --restart')
    version = int(data.get('state_version', 0))
    if version != ring_STATE_VERSION:
        raise RuntimeError(f'Incompatible checkpoint version {version}; expected {ring_STATE_VERSION}. Use --restart.')
    stats = ring_create_statistics()
    for key in ring_STAT_KEYS:
        stats[key] = int(data.get(key, 0))
    pair_first = {tuple(map(int, key.split(','))): value for key, value in data.get('pair_first', {}).items()}
    ring_first = {int(key): value for key, value in data.get('ring_first', {}).items()}
    pair_counts = Counter({tuple(map(int, key.split(','))): int(value) for key, value in data.get('pair_counts', {}).items()})
    ring_counts = Counter({int(key): int(value) for key, value in data.get('ring_counts', {}).items()})
    return int(data['last_candidate']), stats, pair_first, ring_first, pair_counts, ring_counts

def ring_update_results(record: dict, stats: dict, pair_first: dict, ring_first: dict, pair_counts: Counter, ring_counts: Counter, error_writer, mismatch_writer) -> bool:
    """Update counters and return True when a previously unseen ring appears."""
    stats['tested'] += 1
    if record['actual'] == 'ERROR':
        stats['errors'] += 1
        error_writer.writerow(record)
    else:
        stats['verified'] += 1
        stats['accepted' if record['passed'] else 'rejected'] += 1
        stats['sympy_primes' if record['sympy_prime'] else 'sympy_composites'] += 1
        if record['match']:
            stats['matches'] += 1
        else:
            stats['mismatches'] += 1
            mismatch_writer.writerow(record)
            key = 'false_positives' if record['mismatch_type'] == 'FALSE_POSITIVE' else 'false_negatives'
            stats[key] += 1
    if record['ring'] == '':
        stats['records_without_pair'] += 1
        return False
    pair = (int(record['a']), int(record['b']))
    ring = int(record['ring'])
    new_ring = ring not in ring_first
    pair_counts[pair] += 1
    ring_counts[ring] += 1
    pair_first.setdefault(pair, dict(record))
    ring_first.setdefault(ring, dict(record))
    return new_ring

def ring_format_ring_summary(ring_counts: Counter) -> str:
    if not ring_counts:
        return 'no rings'
    rings = sorted(int(key) for key in ring_counts)
    return ' '.join(f'R{ring:_}:{int(ring_counts[ring]):_}' for ring in rings)

def ring_write_first_seen_files(pair_path: Path, ring_path: Path, pair_first: dict, ring_first: dict, pair_counts: Counter, ring_counts: Counter) -> None:
    with pair_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(('a', 'b', 'ring', 'first_index', 'first_n', 'first_result', 'occurrences'))
        for (a, b), first in sorted(pair_first.items(), key=lambda item: int(item[1]['index'])):
            writer.writerow((a, b, first['ring'], first['index'], first['n'], first['actual'], pair_counts[a, b]))
    with ring_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(('ring', 'first_index', 'first_n', 'first_result', 'a', 'b', 'occurrences'))
        for ring, first in sorted(ring_first.items()):
            writer.writerow((ring, first['index'], first['n'], first['actual'], first['a'], first['b'], ring_counts[ring]))

def ring_print_mismatch(record: dict) -> None:
    print(f"MISMATCH {record['mismatch_type']} n={int(record['n']):_} BRPT={record['actual']} SymPy={record['sympy_result']} pair=({record['a']},{record['b']}) ring={record['ring']}", flush=True)

def ring_print_progress(last_candidate: int, stats: dict, initial_tested: int, started: float, pair_first: dict, ring_first: dict, ring_counts: Counter) -> None:
    elapsed = time.perf_counter() - started
    tested_this_run = stats['tested'] - initial_tested
    rate = tested_this_run / elapsed if elapsed else 0.0
    available_ring = ring_AVAILABLE_RING(last_candidate)
    maximum_found_ring = max(ring_first, default=0)

    print(
        f"last_n={last_candidate:_} "
        f"total_tested={int(stats['tested']):_} "
        f"accepted={int(stats['accepted']):_} "
        f"rejected={int(stats['rejected']):_} "
        f"verified={int(stats['verified']):_} "
        f"match={int(stats['matches']):_} "
        f"mismatch={int(stats['mismatches']):_} "
        f"FP={int(stats['false_positives']):_} "
        f"FN={int(stats['false_negatives']):_} "
        f"errors={int(stats['errors']):_} "
        f"pairs={len(pair_first):_} "
        f"available_ring=R{available_ring:_} "
        f"max_found_ring=R{maximum_found_ring:_} "
        f"distinct_rings={len(ring_first):_} "
        f"rate={float(rate):_.1f} candidates/s\n"
        f"summary: {ring_format_ring_summary(ring_counts)}\n",
        flush=True,
    )

def ring_parse_args():
    """Parse and validate command-line arguments."""
    parser = argparse.ArgumentParser(description='Compare BRPT with SymPy on 6k±1 integers and track the winning coefficient pairs (A,B) and rings.')
    parser.add_argument('--start', type=int, default=1, help='Exclusive lower bound. Testing starts at the next integer congruent to 1 or 5 modulo 6 (default: 1).')
    parser.add_argument('--count', type=int, help='Number of candidates to test; omit to continue indefinitely.')
    parser.add_argument('--brpt-module', type=Path, default=Path('brpt.py'), help='Path to the BRPT module (default: brpt.py).')
    parser.add_argument('--output-dir', type=Path, default=Path('result_test_ring'), help='Directory for checkpoints and reports (default: result_test_ring).')
    parser.add_argument('--workers', type=int, help='Number of worker processes; omit to use the system default.')
    parser.add_argument('--chunksize', type=int, default=64, help='Candidates assigned to each worker per batch (default: 64).')
    parser.add_argument('--progress-every', type=int, default=5000, help='Print progress every N completed candidates; 0 disables it.')
    parser.add_argument('--save-every', type=int, default=1000, help='Save the checkpoint every N completed candidates (default: 1000).')
    parser.add_argument('--restart', action='store_true', help='Ignore an existing checkpoint and restart from --start.')
    parser.add_argument('--save-events', action='store_true', help='Write one CSV row for every tested candidate.')
    parser.add_argument('--allow-probable-reference', action='store_true', help='Allow SymPy comparisons for n >= 2**64, where the reference is classified as probable rather than definitive.')
    args = parser.parse_args()
    if not args.brpt_module.is_file():
        parser.error(f'BRPT module not found: {args.brpt_module}')
    if args.start < 0:
        parser.error('--start must be >= 0')
    if args.count is not None and args.count < 1:
        parser.error('--count must be >= 1')
    if args.workers is not None and args.workers < 1:
        parser.error('--workers must be >= 1')
    if args.chunksize < 1:
        parser.error('--chunksize must be >= 1')
    if args.progress_every < 0:
        parser.error('--progress-every must be >= 0')
    if args.save_every < 1:
        parser.error('--save-every must be >= 1')
    return parser, args

def ring_main() -> None:
    global ring_STOP_REQUESTED
    ring_STOP_REQUESTED = False
    signal.signal(signal.SIGINT, ring_request_stop)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, ring_request_stop)
    parser, args = ring_parse_args()
    module_path = str(args.brpt_module.resolve())
    ring_load_brpt(module_path)
    ring_load_sympy_isprime()
    output = args.output_dir
    output.mkdir(parents=True, exist_ok=True)
    paths = {'events': output / 'mod6_events_ring.csv', 'pairs': output / 'pair_first_seen_ring.csv', 'rings': output / 'ring_first_seen_ring.csv', 'errors': output / 'errors_ring.csv', 'mismatches': output / 'mismatches_ring.csv', 'summary': output / 'summary_ring.json', 'state': output / 'state_ring.json'}
    resumed = paths['state'].is_file() and (not args.restart)
    print('[Modulo-6 ring scan]')
    if resumed:
        try:
            generation_start, stats, pair_first, ring_first, pair_counts, ring_counts = ring_load_checkpoint(paths['state'])
        except RuntimeError as exc:
            parser.error(str(exc))
        last_candidate = generation_start
        print(f"Resuming after candidate {last_candidate:_}, total tested={stats['tested']:_}, SymPy verified={stats['verified']:_}")
    else:
        generation_start = args.start
        last_candidate = args.start
        stats = ring_create_statistics()
        pair_first = {}
        ring_first = {}
        pair_counts = Counter()
        ring_counts = Counter()
    initial_tested = stats['tested']
    started = time.perf_counter()
    interrupted = False
    mode = 'a' if resumed else 'w'
    tasks = ring_generate_candidates(generation_start, args.count, stats['tested'] + 1)
    events_file: TextIO | None = None
    if args.save_events:
        events_file = paths['events'].open(mode, newline='', encoding='utf-8')
    try:
        with paths['errors'].open(mode, newline='', encoding='utf-8') as errors_file, paths['mismatches'].open(mode, newline='', encoding='utf-8') as mismatches_file:
            events_writer = csv.DictWriter(events_file, fieldnames=ring_FIELDS) if events_file is not None else None
            error_writer = csv.DictWriter(errors_file, fieldnames=ring_FIELDS)
            mismatch_writer = csv.DictWriter(mismatches_file, fieldnames=ring_FIELDS)
            if events_writer is not None and (not resumed or paths['events'].stat().st_size == 0):
                events_writer.writeheader()
            if not resumed or paths['errors'].stat().st_size == 0:
                error_writer.writeheader()
            if not resumed or paths['mismatches'].stat().st_size == 0:
                mismatch_writer.writeheader()
            pool = mp.Pool(args.workers, initializer=ring_worker_init, initargs=(module_path, args.allow_probable_reference))
            try:
                results_iterator = pool.imap(ring_test_candidate, tasks, chunksize=args.chunksize)
                supports_timeout = hasattr(results_iterator, 'next')
                while True:
                    if ring_STOP_REQUESTED:
                        interrupted = True
                        break
                    try:
                        if supports_timeout:
                            record = results_iterator.next(timeout=1.0)
                        else:
                            record = next(results_iterator)
                    except mp.TimeoutError:
                        continue
                    except StopIteration:
                        break
                    last_candidate = int(record['n'])
                    if events_writer is not None:
                        events_writer.writerow(record)
                    new_ring = ring_update_results(record, stats, pair_first, ring_first, pair_counts, ring_counts, error_writer, mismatch_writer)
                    if not record['match'] and record['actual'] != 'ERROR':
                        ring_print_mismatch(record)
                    if new_ring:
                        print(
                            f"NEW RING R{int(record['ring']):_} "
                            f"available=R{ring_AVAILABLE_RING(last_candidate):_} "
                            f"n={last_candidate:_} "
                            f"index={int(record['index']):_} "
                            f"result={record['actual']} "
                            f"pair=({int(record['a']):_},{int(record['b']):_})",
                            flush=True,
                        )
                    if stats['tested'] % args.save_every == 0:
                        if events_file is not None:
                            events_file.flush()
                        errors_file.flush()
                        mismatches_file.flush()
                        ring_save_checkpoint(paths['state'], last_candidate, stats, pair_first, ring_first, pair_counts, ring_counts)
                    if args.progress_every and stats['tested'] % args.progress_every == 0:
                        ring_print_progress(last_candidate, stats, initial_tested, started, pair_first, ring_first, ring_counts)
                    if ring_STOP_REQUESTED:
                        interrupted = True
                        break
                    if record['actual'] == 'ERROR':
                        interrupted = True
                        print(f"Stopping at the first error: n={last_candidate:_} error={record['error']}", flush=True)
                        break
                    if record['mismatch_type'] in ('FALSE_POSITIVE', 'FALSE_NEGATIVE'):
                        interrupted = True
                        print('Stopping at the first mismatch.', flush=True)
                        break
            except KeyboardInterrupt:
                interrupted = True
                print('\nInterrupt received: stopping workers and saving...', flush=True)
                pool.terminate()
                pool.join()
            except BaseException:
                signal.signal(signal.SIGINT, signal.SIG_IGN)
                pool.terminate()
                pool.join()
                raise
            else:
                if interrupted:
                    pool.terminate()
                else:
                    pool.close()
                pool.join()
            if events_file is not None:
                events_file.flush()
            errors_file.flush()
            mismatches_file.flush()
            ring_save_checkpoint(paths['state'], last_candidate, stats, pair_first, ring_first, pair_counts, ring_counts)
    finally:
        if events_file is not None:
            events_file.close()
    ring_write_first_seen_files(paths['pairs'], paths['rings'], pair_first, ring_first, pair_counts, ring_counts)
    elapsed = time.perf_counter() - started
    tested_this_run = stats['tested'] - initial_tested
    rate = tested_this_run / elapsed if elapsed else 0.0
    summary = {'state_version': ring_STATE_VERSION, 'generator': ring_GENERATOR, 'generated_residues_mod_6': [1, 5], 'initial_start_exclusive': args.start, 'resumed': resumed, 'last_candidate': last_candidate, 'last_candidate_residue_mod_6': last_candidate % 6, 'requested_count_this_run': args.count, 'continuous_mode': args.count is None, 'save_events': args.save_events, 'interrupted': interrupted, 'brpt_module': module_path, 'tested': stats['tested'], 'accepted_by_brpt': stats['accepted'], 'rejected_by_brpt': stats['rejected'], 'reference': 'sympy.isprime', 'sympy_definitive_below': ring_SYMPY_DEFINITIVE_LIMIT, 'allow_probable_reference': args.allow_probable_reference, 'verified': stats['verified'], 'matches': stats['matches'], 'mismatches': stats['mismatches'], 'false_positives': stats['false_positives'], 'false_negatives': stats['false_negatives'], 'sympy_primes': stats['sympy_primes'], 'sympy_composites': stats['sympy_composites'], 'fully_verified': stats['errors'] == 0, 'errors': stats['errors'], 'records_without_pair': stats['records_without_pair'], 'distinct_pairs': len(pair_first), 'distinct_rings': len(ring_first), 'maximum_ring': max(ring_first, default=None), 'available_ring_at_last_candidate': ring_AVAILABLE_RING(last_candidate), 'elapsed_seconds': round(elapsed, 3), 'tested_this_run': tested_this_run, 'rate_per_second': round(rate, 3)}
    ring_atomic_write_json(paths['summary'], summary)
    available_ring = ring_AVAILABLE_RING(last_candidate)
    maximum_found_ring = max(ring_first, default=0)
    print(
        f"done: last_n={last_candidate:_} "
        f"total_tested={stats['tested']:_} "
        f"tested_this_run={tested_this_run:_} "
        f"accepted={stats['accepted']:_} "
        f"rejected={stats['rejected']:_} "
        f"verified={stats['verified']:_} "
        f"match={stats['matches']:_} "
        f"mismatch={stats['mismatches']:_} "
        f"FP={stats['false_positives']:_} "
        f"FN={stats['false_negatives']:_} "
        f"errors={stats['errors']:_} "
        f"pairs={len(pair_first):_} "
        f"available_ring=R{available_ring:_} "
        f"max_found_ring=R{maximum_found_ring:_} "
        f"distinct_rings={len(ring_first):_} "
        f"rate={rate:_.1f} candidates/s"
    )
    print(f'summary: {ring_format_ring_summary(ring_counts)}')
    print(f'\nresults: {output.resolve()}')
prime_STATE_VERSION = 2
prime_GENERATOR = 'segmented_sieve'
prime_STOP_REQUESTED = False
prime_WORKER_BRPT: Callable[[int], bool] | None = None
prime_WORKER_LAST_PAIR: tuple[int, int] | None = None


def prime_sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open('rb') as stream:
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def prime_atomic_write_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_suffix(path.suffix + '.tmp')
    with temporary.open('w', encoding='utf-8', newline='\n') as stream:
        json.dump(data, stream, ensure_ascii=False, indent=2, sort_keys=True)
        stream.write('\n')
        stream.flush()
        os.fsync(stream.fileno())
    os.replace(temporary, path)


def prime_ensure_csv(path: Path, header: tuple[str, ...]) -> None:
    if path.exists() and path.stat().st_size:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('w', encoding='utf-8', newline='') as stream:
        csv.writer(stream).writerow(header)


def prime_append_csv(path: Path, row: Iterable[Any]) -> None:
    with path.open('a', encoding='utf-8', newline='') as stream:
        csv.writer(stream).writerow(row)
        stream.flush()
        os.fsync(stream.fileno())


def prime_request_stop(signum: int, _frame: Any) -> None:
    """Set a flag only; the main loop polls worker results with a timeout."""
    global prime_STOP_REQUESTED
    if not prime_STOP_REQUESTED:
        prime_STOP_REQUESTED = True
        print(
            f'\nSignal {signum} received: stopping workers and saving the last completed prime...',
            flush=True,
        )


def prime_load_module(path: Path, module_name: str) -> ModuleType:
    spec = importlib.util.spec_from_file_location(module_name, str(path))
    if spec is None or spec.loader is None:
        raise RuntimeError(f'Unable to load BRPT module: {path}')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def prime_install_pair_tracker(module: ModuleType) -> None:
    """Track the coefficient pair returned by find_pair/find_couple."""
    global prime_WORKER_LAST_PAIR
    for name in ('find_pair', 'find_couple'):
        original = module.__dict__.get(name)
        if not callable(original):
            continue

        def tracked(n: int, _original: Callable[[int], Any] = original) -> Any:
            global prime_WORKER_LAST_PAIR
            prime_WORKER_LAST_PAIR = None
            result = _original(n)
            if result is not None:
                try:
                    pair = result[0]
                    prime_WORKER_LAST_PAIR = (int(pair[0]), int(pair[1]))
                except (IndexError, TypeError, ValueError):
                    prime_WORKER_LAST_PAIR = None
            return result

        module.__dict__[name] = tracked


def prime_worker_init(module_path: str) -> None:
    global prime_WORKER_BRPT, prime_WORKER_LAST_PAIR
    signal.signal(signal.SIGINT, signal.SIG_IGN)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, signal.SIG_IGN)
    path = Path(module_path)
    module = prime_load_module(path, f'brpt_prime_worker_{os.getpid()}')
    prime_install_pair_tracker(module)
    predicate = module.__dict__.get('brpt')
    if not callable(predicate):
        raise RuntimeError(f'{path} does not expose a callable brpt(n)')
    prime_WORKER_BRPT = cast(Callable[[int], bool], predicate)
    prime_WORKER_LAST_PAIR = None


def prime_worker_test(n: int) -> tuple[int, bool, tuple[int, int] | None, float, str]:
    global prime_WORKER_LAST_PAIR
    started = time.perf_counter()
    prime_WORKER_LAST_PAIR = None
    try:
        if prime_WORKER_BRPT is None:
            raise RuntimeError('BRPT worker was not initialized')
        passed = bool(prime_WORKER_BRPT(n))
        return n, passed, prime_WORKER_LAST_PAIR, (time.perf_counter() - started) * 1000.0, ''
    except BaseException:
        return n, False, prime_WORKER_LAST_PAIR, (time.perf_counter() - started) * 1000.0, traceback.format_exc()


def prime_worker_test_batch(numbers: list[int]) -> list[tuple[int, bool, tuple[int, int] | None, float, str]]:
    """Test one explicit batch while the parent polls batch completion."""
    return [prime_worker_test(n) for n in numbers]


def prime_batches(values: list[int], size: int) -> Iterable[list[int]]:
    for start in range(0, len(values), size):
        yield values[start:start + size]


class prime_BasePrimeCache:
    """Dynamically enlarged cache of base primes for the segmented sieve."""

    def __init__(self) -> None:
        self.limit = 1
        self.primes: list[int] = []

    def ensure(self, required: int) -> list[int]:
        if required <= self.limit:
            return self.primes
        target = max(required, 1024 if self.limit < 2 else self.limit * 2)
        sieve = bytearray(b'\x01') * (target + 1)
        sieve[0:2] = b'\x00\x00'
        for p in range(2, math.isqrt(target) + 1):
            if sieve[p]:
                start = p * p
                count = ((target - start) // p) + 1
                sieve[start:target + 1:p] = b'\x00' * count
        self.primes = [value for value, flag in enumerate(sieve) if flag]
        self.limit = target
        return self.primes


def prime_segmented_primes(lo: int, hi: int, cache: prime_BasePrimeCache) -> list[int]:
    """Return every prime in the inclusive interval [lo, hi], exactly."""
    if hi < 2 or hi < lo:
        return []
    lo = max(lo, 2)
    flags = bytearray(b'\x01') * (hi - lo + 1)
    for p in cache.ensure(math.isqrt(hi)):
        p_squared = p * p
        if p_squared > hi:
            break
        first = max(p_squared, ((lo + p - 1) // p) * p)
        count = ((hi - first) // p) + 1
        flags[first - lo:len(flags):p] = b'\x00' * count
    return [lo + offset for offset, flag in enumerate(flags) if flag]


def prime_encode_pair_counts(counter: Counter[tuple[int, int]]) -> dict[str, int]:
    return {f'{a},{b}': count for (a, b), count in sorted(counter.items())}


def prime_decode_pair_counts(data: dict[str, Any]) -> Counter[tuple[int, int]]:
    counter: Counter[tuple[int, int]] = Counter()
    for key, value in data.items():
        a_text, b_text = key.split(',', 1)
        counter[int(a_text), int(b_text)] = int(value)
    return counter


def prime_make_state(args: argparse.Namespace, module_path: Path, module_sha256: str) -> dict[str, Any]:
    now = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    return {
        'state_version': prime_STATE_VERSION,
        'generator': prime_GENERATOR,
        'status': 'RUNNING',
        'started_at': now,
        'updated_at': now,
        'completed_at': None,
        'requested_start': args.start,
        'requested_stop': args.stop,
        'next_n': args.start,
        'last_prime': None,
        'prime_index': 0,
        'tested': 0,
        'accepted': 0,
        'rejected': 0,
        'failures': 0,
        'false_negatives': 0,
        'errors': 0,
        'total_brpt_seconds': 0.0,
        'max_elapsed_ms': 0.0,
        'maximum_ring': 0,
        'ring_counts': {},
        'pair_counts': {},
        'ring_first': {},
        'pair_first': {},
        'first_seen_tracking': {
            'complete_from_requested_start': True,
            'started_prime_index': 0,
            'started_n': args.start,
            'historical_gap': False,
        },
        'halted_on': None,
        'brpt_module': {'path': str(module_path.resolve()), 'sha256': module_sha256},
        'runtime': {
            'python': sys.version,
            'platform': platform.platform(),
            'workers': args.workers,
            'segment_size': args.segment_size,
            'chunksize': args.chunksize,
        },
        'scope': 'Exact primes generated by a segmented sieve; composites are never submitted to BRPT.',
    }


def prime_migrate_state_v1(state: dict[str, Any]) -> dict[str, Any]:
    """Convert the standalone prime tester checkpoint without losing progress."""
    generator = str(state.get('generator', ''))
    if generator not in ('sieve', 'mr64'):
        raise RuntimeError(f'Unsupported legacy prime generator: {generator!r}')
    range_data = state.get('range', {})
    progress = state.get('progress', {})
    false_negatives = int(progress.get('false_negatives', 0))
    errors = int(progress.get('errors', 0))
    migrated = {
        'state_version': prime_STATE_VERSION,
        'generator': prime_GENERATOR,
        'status': state.get('status', 'INTERRUPTED'),
        'started_at': state.get('started_at'),
        'updated_at': state.get('updated_at'),
        'completed_at': state.get('completed_at'),
        'requested_start': int(range_data.get('requested_start', 2)),
        'requested_stop': int(range_data.get('requested_stop', 0)),
        'next_n': int(range_data.get('next_n', range_data.get('requested_start', 2))),
        'last_prime': range_data.get('last_prime'),
        'prime_index': int(progress.get('prime_index', progress.get('tested', 0))),
        'tested': int(progress.get('tested', 0)),
        'accepted': int(progress.get('accepted', 0)),
        'rejected': int(progress.get('rejected', 0)),
        'failures': false_negatives + errors,
        'false_negatives': false_negatives,
        'errors': errors,
        'total_brpt_seconds': float(progress.get('total_brpt_seconds', 0.0)),
        'max_elapsed_ms': 0.0,
        'maximum_ring': int(progress.get('max_observed_ring', 0)),
        'ring_counts': dict(state.get('ring_counts', {})),
        'pair_counts': dict(state.get('pair_counts', {})),
        'ring_first': dict(state.get('ring_first', {})),
        'pair_first': dict(state.get('pair_first', {})),
        'first_seen_tracking': dict(state.get('first_seen_tracking', {
            'complete_from_requested_start': bool(state.get('pair_first') and state.get('ring_first')),
            'started_prime_index': int(progress.get('tested', 0)),
            'started_n': int(range_data.get('next_n', range_data.get('requested_start', 2))),
            'historical_gap': not bool(state.get('pair_first') and state.get('ring_first')),
        })),
        'halted_on': state.get('halted_on'),
        'brpt_module': dict(state.get('brpt_module', {})),
        'runtime': dict(state.get('runtime', {})),
        'scope': 'Migrated from standalone prime tester; continuation uses the exact segmented sieve.',
        'migration': {
            'from_state_version': 1,
            'historical_generator': generator,
            'migrated_at': time.strftime('%Y-%m-%dT%H:%M:%S%z'),
        },
    }
    return migrated


def prime_ensure_first_seen_schema(state: dict[str, Any]) -> None:
    """Initialize chronology fields in checkpoints created before v18.2."""
    had_pair_first = 'pair_first' in state and bool(state.get('pair_first'))
    had_ring_first = 'ring_first' in state and bool(state.get('ring_first'))
    state.setdefault('pair_first', {})
    state.setdefault('ring_first', {})
    if 'first_seen_tracking' not in state:
        complete = bool(had_pair_first and had_ring_first)
        tested = int(state.get('tested', state.get('prime_index', 0)))
        state['first_seen_tracking'] = {
            'complete_from_requested_start': complete,
            'started_prime_index': 0 if complete else tested,
            'started_n': int(state.get('requested_start', 2)) if complete else int(state.get('next_n', state.get('requested_start', 2))),
            'historical_gap': not complete,
        }


def prime_write_first_seen_files(
    pair_path: Path,
    ring_path: Path,
    state: dict[str, Any],
    pair_counts: Counter[tuple[int, int]],
    ring_counts: Counter[int],
) -> None:
    """Write PRIME first-seen tables analogous to the RING reports."""
    pair_first = state.get('pair_first', {})
    ring_first = state.get('ring_first', {})
    with pair_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(('a', 'b', 'ring', 'first_prime_index', 'first_prime', 'first_result', 'elapsed_ms', 'occurrences'))
        rows: list[tuple[tuple[int, int], dict[str, Any]]] = []
        for key, record in pair_first.items():
            try:
                a_text, b_text = str(key).split(',', 1)
                rows.append(((int(a_text), int(b_text)), record))
            except (TypeError, ValueError):
                continue
        for (a, b), record in sorted(rows, key=lambda item: int(item[1].get('index', 0))):
            writer.writerow((
                a, b, int(record.get('ring', max(abs(a), abs(b)))),
                int(record.get('index', 0)), int(record.get('n', 0)),
                record.get('actual', 'PRIME'), record.get('elapsed_ms', ''),
                int(pair_counts.get((a, b), 0)),
            ))
    with ring_path.open('w', newline='', encoding='utf-8') as handle:
        writer = csv.writer(handle)
        writer.writerow(('ring', 'first_prime_index', 'first_prime', 'first_result', 'a', 'b', 'elapsed_ms', 'occurrences'))
        rows: list[tuple[int, dict[str, Any]]] = []
        for key, record in ring_first.items():
            try:
                rows.append((int(key), record))
            except (TypeError, ValueError):
                continue
        for ring, record in sorted(rows):
            writer.writerow((
                ring, int(record.get('index', 0)), int(record.get('n', 0)),
                record.get('actual', 'PRIME'), int(record.get('a', 0)),
                int(record.get('b', 0)), record.get('elapsed_ms', ''),
                int(ring_counts.get(ring, 0)),
            ))


def prime_validate_resume_state(
    state: dict[str, Any],
    args: argparse.Namespace,
    module_path: Path,
    module_sha256: str,
) -> None:
    if int(state.get('state_version', -1)) != prime_STATE_VERSION:
        raise RuntimeError(
            f"Unsupported prime checkpoint version {state.get('state_version')}; expected {prime_STATE_VERSION}. Use --restart."
        )
    if state.get('generator') != prime_GENERATOR:
        raise RuntimeError('Checkpoint was not generated by the exact segmented sieve')
    if int(state.get('requested_start', -1)) != args.start:
        raise RuntimeError('Checkpoint start differs from --start')
    previous_module = state.get('brpt_module', {})
    if str(previous_module.get('sha256', '')) != module_sha256 and not args.allow_module_change:
        raise RuntimeError('BRPT module SHA-256 differs from the checkpoint; use --allow-module-change only intentionally')
    if Path(str(previous_module.get('path', ''))) != module_path.resolve() and not args.allow_module_change:
        raise RuntimeError('BRPT module path differs from the checkpoint; use --allow-module-change only intentionally')


def prime_save_state(
    path: Path,
    state: dict[str, Any],
    ring_counts: Counter[int],
    pair_counts: Counter[tuple[int, int]],
) -> None:
    state['updated_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
    state['ring_counts'] = {str(ring): count for ring, count in sorted(ring_counts.items())}
    state['pair_counts'] = prime_encode_pair_counts(pair_counts)
    prime_atomic_write_json(path, state)


def prime_print_progress(
    state: dict[str, Any],
    session_start_tested: int,
    started_monotonic: float,
    last_pair: tuple[int, int] | None,
) -> None:
    elapsed = max(time.monotonic() - started_monotonic, 1e-12)
    rate = (int(state['tested']) - session_start_tested) / elapsed
    pair_text = '-' if last_pair is None else f'({last_pair[0]},{last_pair[1]})'
    last_prime = state.get('last_prime')
    last_prime_text = '-' if last_prime is None else f'{int(last_prime):_}'
    print(
        f"prime_index={int(state['prime_index']):_} "
        f"last_prime={last_prime_text} "
        f"tested={int(state['tested']):_} "
        f"accepted={int(state['accepted']):_} "
        f"FN={int(state['false_negatives']):_} "
        f"errors={int(state['errors']):_} "
        f"max_ring=R{int(state['maximum_ring']):_} "
        f"last_pair={pair_text} "
        f"rate={rate:_.1f} primes/s",
        flush=True,
    )


def prime_process_result(
    result: tuple[int, bool, tuple[int, int] | None, float, str],
    state: dict[str, Any],
    ring_counts: Counter[int],
    pair_counts: Counter[tuple[int, int]],
    false_negative_path: Path,
    error_path: Path,
    all_results_path: Path,
    save_events: bool,
) -> tuple[tuple[int, int] | None, str | None]:
    n, passed, pair, elapsed_ms, error_text = result
    state['prime_index'] = int(state['prime_index']) + 1
    state['tested'] = int(state['tested']) + 1
    state['total_brpt_seconds'] = float(state['total_brpt_seconds']) + elapsed_ms / 1000.0
    state['max_elapsed_ms'] = max(float(state.get('max_elapsed_ms', 0.0)), elapsed_ms)
    state['last_prime'] = n
    state['next_n'] = n + 1

    ring = 0
    a_value: int | str = ''
    b_value: int | str = ''
    if pair is not None:
        a_value, b_value = pair
        ring = max(abs(a_value), abs(b_value))
        pair_counts[pair] += 1
        ring_counts[ring] += 1
        state['maximum_ring'] = max(int(state['maximum_ring']), ring)
        if passed and not error_text:
            first_record = {
                'index': int(state['prime_index']),
                'n': str(n),
                'actual': 'PRIME',
                'passed': True,
                'a': a_value,
                'b': b_value,
                'ring': ring,
                'elapsed_ms': f'{elapsed_ms:.6f}',
                'error': '',
            }
            state.setdefault('pair_first', {}).setdefault(f'{a_value},{b_value}', dict(first_record))
            state.setdefault('ring_first', {}).setdefault(str(ring), dict(first_record))

    stop_reason: str | None = None
    if error_text:
        state['errors'] = int(state['errors']) + 1
        state['failures'] = int(state['failures']) + 1
        state['status'] = 'ERROR'
        state['next_n'] = n
        state['halted_on'] = {
            'type': 'ERROR',
            'prime_index': int(state['prime_index']),
            'n': n,
            'error': error_text,
        }
        prime_append_csv(error_path, (
            state['prime_index'], n, 'PRIME', 'ERROR', a_value, b_value,
            ring or '', f'{elapsed_ms:.6f}', error_text,
        ))
        stop_reason = 'ERROR'
    elif not passed:
        state['rejected'] = int(state['rejected']) + 1
        state['failures'] = int(state['failures']) + 1
        state['false_negatives'] = int(state['false_negatives']) + 1
        state['status'] = 'FALSE_NEGATIVE'
        state['next_n'] = n
        state['halted_on'] = {
            'type': 'FALSE_NEGATIVE',
            'prime_index': int(state['prime_index']),
            'n': n,
            'pair': list(pair) if pair is not None else None,
            'ring': ring or None,
        }
        prime_append_csv(false_negative_path, (
            state['prime_index'], n, 'PRIME', 'COMPOSITE', a_value, b_value,
            ring or '', f'{elapsed_ms:.6f}', '',
        ))
        stop_reason = 'FALSE_NEGATIVE'
    else:
        state['accepted'] = int(state['accepted']) + 1

    if save_events:
        prime_append_csv(all_results_path, (
            state['prime_index'], n, 'PRIME' if passed else 'COMPOSITE',
            a_value, b_value, ring or '', f'{elapsed_ms:.6f}', error_text,
        ))
    return pair, stop_reason


def prime_main() -> None:
    global prime_STOP_REQUESTED
    prime_STOP_REQUESTED = False
    # The unified launcher has already validated and forwarded every option.
    parser = argparse.ArgumentParser(description='Exact prime-only BRPT validation with checkpoint/resume.')
    parser.add_argument('--brpt-module', type=Path, required=True)
    parser.add_argument('--start', type=int, default=2)
    parser.add_argument('--stop', type=int, default=0)
    parser.add_argument('--output-dir', type=Path, default=Path('results_prime_scan'))
    parser.add_argument('--workers', type=int, default=max(1, os.cpu_count() or 1))
    parser.add_argument('--segment-size', type=int, default=2_000_000)
    parser.add_argument('--chunksize', type=int, default=32)
    parser.add_argument('--save-every', type=int, default=10_000)
    parser.add_argument('--progress-every', type=int, default=10_000)
    parser.add_argument('--restart', action='store_true')
    parser.add_argument('--allow-module-change', action='store_true')
    parser.add_argument('--save-events', action='store_true')
    args = parser.parse_args()

    module_path = args.brpt_module.expanduser().resolve()
    if not module_path.is_file():
        parser.error(f'BRPT module not found: {module_path}')
    if args.start < 0:
        parser.error('--start must be >= 0')
    if args.stop < 0 or (args.stop and args.stop < args.start):
        parser.error('--stop must be 0 or >= --start')
    if args.workers < 1 or args.segment_size < 1 or args.chunksize < 1:
        parser.error('--workers, --segment-size, and --chunksize must be >= 1')
    if args.save_every < 1 or args.progress_every < 0:
        parser.error('--save-every must be >= 1 and --progress-every must be >= 0')

    # Validate import before workers are started, so configuration errors are immediate.
    module = prime_load_module(module_path, 'brpt_prime_validation')
    if not callable(module.__dict__.get('brpt')):
        parser.error(f'{module_path} does not expose a callable brpt(n)')
    module_sha256 = prime_sha256_file(module_path)

    output_dir = args.output_dir.expanduser().resolve()
    output_dir.mkdir(parents=True, exist_ok=True)
    state_path = output_dir / 'state_prime_scan.json'
    summary_path = output_dir / 'summary_prime_scan.json'
    false_negative_path = output_dir / 'false_negatives.csv'
    error_path = output_dir / 'errors.csv'
    all_results_path = output_dir / 'prime_results.csv'
    pair_first_path = output_dir / 'pair_first_seen_prime.csv'
    ring_first_path = output_dir / 'ring_first_seen_prime.csv'
    event_header = ('prime_index', 'n', 'expected', 'brpt_result', 'a', 'b', 'ring', 'elapsed_ms', 'error')
    new_campaign = args.restart or not state_path.exists()
    if new_campaign:
        for stale_path in (state_path, summary_path, false_negative_path, error_path, all_results_path, pair_first_path, ring_first_path):
            if stale_path.is_file():
                stale_path.unlink()
    prime_ensure_csv(false_negative_path, event_header)
    prime_ensure_csv(error_path, event_header)
    if args.save_events:
        prime_ensure_csv(all_results_path, ('prime_index', 'n', 'brpt_result', 'a', 'b', 'ring', 'elapsed_ms', 'error'))

    if state_path.exists() and not args.restart:
        state = json.loads(state_path.read_text(encoding='utf-8'))
        if int(state.get('state_version', -1)) == 1:
            try:
                state = prime_migrate_state_v1(state)
            except RuntimeError as exc:
                parser.error(str(exc))
            print('Migrated legacy prime checkpoint from state version 1.', flush=True)
        try:
            prime_validate_resume_state(state, args, module_path, module_sha256)
        except RuntimeError as exc:
            parser.error(str(exc))
        state['status'] = 'RUNNING'
        state['completed_at'] = None
        state['halted_on'] = None
        state['requested_stop'] = args.stop
        state['brpt_module'] = {'path': str(module_path), 'sha256': module_sha256}
        state['runtime'].update({
            'python': sys.version,
            'platform': platform.platform(),
            'workers': args.workers,
            'segment_size': args.segment_size,
            'chunksize': args.chunksize,
        })
        # Older PRIME checkpoints contain frequency totals but may not contain chronology.
        # They remain resumable; exact first-seen tracking starts at the next prime.
        prime_ensure_first_seen_schema(state)
        ring_counts = Counter({int(k): int(v) for k, v in state.get('ring_counts', {}).items()})
        pair_counts = prime_decode_pair_counts(state.get('pair_counts', {}))
        current = max(args.start, int(state['next_n']))
        print(f'Resuming exact prime scan from n={current:_}', flush=True)
    else:
        state = prime_make_state(args, module_path, module_sha256)
        prime_ensure_first_seen_schema(state)
        ring_counts: Counter[int] = Counter()
        pair_counts: Counter[tuple[int, int]] = Counter()
        current = args.start
        prime_save_state(state_path, state, ring_counts, pair_counts)
        print(f'Starting exact prime scan from n={current:_}', flush=True)

    session_start_tested = int(state['tested'])
    started_monotonic = time.monotonic()
    cache = prime_BasePrimeCache()
    signal.signal(signal.SIGINT, prime_request_stop)
    if hasattr(signal, 'SIGBREAK'):
        signal.signal(signal.SIGBREAK, prime_request_stop)

    context = mp.get_context('spawn' if os.name == 'nt' else 'fork')
    pool = context.Pool(args.workers, initializer=prime_worker_init, initargs=(str(module_path),))
    pool_finished = False
    last_pair: tuple[int, int] | None = None
    stop_reason: str | None = None

    try:
        while not prime_STOP_REQUESTED and (args.stop == 0 or current <= args.stop):
            hi = current + args.segment_size - 1
            if args.stop:
                hi = min(hi, args.stop)
            primes = prime_segmented_primes(current, hi, cache)
            if prime_STOP_REQUESTED:
                break
            if not primes:
                current = hi + 1
                state['next_n'] = current
                prime_save_state(state_path, state, ring_counts, pair_counts)
                continue

            # Submit explicit batches as single pool jobs.  This preserves
            # throughput while keeping a real IMapIterator whose next(timeout)
            # lets Ctrl+C terminate the pool even when a worker is busy.
            iterator = pool.imap(
                prime_worker_test_batch,
                prime_batches(primes, args.chunksize),
                chunksize=1,
            )
            while True:
                if prime_STOP_REQUESTED:
                    break
                try:
                    batch_results = iterator.next(timeout=0.5)
                except mp.TimeoutError:
                    continue
                except StopIteration:
                    break
                for result in batch_results:
                    last_pair, stop_reason = prime_process_result(
                        result, state, ring_counts, pair_counts,
                        false_negative_path, error_path, all_results_path,
                        args.save_events,
                    )
                    tested = int(state['tested'])
                    if args.progress_every and tested % args.progress_every == 0:
                        prime_print_progress(state, session_start_tested, started_monotonic, last_pair)
                    if tested % args.save_every == 0 or stop_reason:
                        prime_save_state(state_path, state, ring_counts, pair_counts)
                    if stop_reason or prime_STOP_REQUESTED:
                        break
                if stop_reason or prime_STOP_REQUESTED:
                    break

            if stop_reason or prime_STOP_REQUESTED:
                break
            current = hi + 1
            state['next_n'] = current
            prime_save_state(state_path, state, ring_counts, pair_counts)

        if stop_reason:
            pool.terminate()
        elif prime_STOP_REQUESTED:
            state['status'] = 'INTERRUPTED'
            state['halted_on'] = {'type': 'INTERRUPTED', 'next_n': int(state['next_n'])}
            pool.terminate()
        else:
            state['status'] = 'COMPLETED'
            state['completed_at'] = time.strftime('%Y-%m-%dT%H:%M:%S%z')
            state['halted_on'] = None
            pool.close()
        pool.join()
        pool_finished = True
    except KeyboardInterrupt:
        # A second Ctrl+C bypasses the normal handler on some terminals.
        prime_STOP_REQUESTED = True
        state['status'] = 'INTERRUPTED'
        state['halted_on'] = {'type': 'INTERRUPTED', 'next_n': int(state['next_n'])}
        pool.terminate()
        pool.join()
        pool_finished = True
    except BaseException:
        state['status'] = 'MAIN_PROCESS_ERROR'
        state['halted_on'] = {
            'type': 'MAIN_PROCESS_ERROR',
            'next_n': int(state['next_n']),
            'error': traceback.format_exc(),
        }
        prime_append_csv(error_path, (
            state['prime_index'], state['next_n'], 'PRIME', 'MAIN_PROCESS_ERROR',
            '', '', '', '', state['halted_on']['error'],
        ))
        pool.terminate()
        pool.join()
        pool_finished = True
        raise
    finally:
        if not pool_finished:
            pool.terminate()
            pool.join()
        prime_save_state(state_path, state, ring_counts, pair_counts)
        prime_write_first_seen_files(pair_first_path, ring_first_path, state, pair_counts, ring_counts)

    elapsed = max(time.monotonic() - started_monotonic, 0.0)
    tested_this_run = int(state['tested']) - session_start_tested
    summary = {
        'state_version': prime_STATE_VERSION,
        'generator': prime_GENERATOR,
        'status': state['status'],
        'interrupted': state['status'] == 'INTERRUPTED',
        'brpt_module': str(module_path),
        'requested_start': args.start,
        'requested_stop': args.stop,
        'last_prime': state['last_prime'],
        'next_n': state['next_n'],
        'prime_index': state['prime_index'],
        'tested': state['tested'],
        'accepted': state['accepted'],
        'rejected': state['rejected'],
        'false_negatives': state['false_negatives'],
        'errors': state['errors'],
        'maximum_ring': state['maximum_ring'],
        'distinct_pairs': len(pair_counts),
        'distinct_rings': len(ring_counts),
        'recorded_first_pairs': len(state.get('pair_first', {})),
        'recorded_first_rings': len(state.get('ring_first', {})),
        'first_seen_tracking': dict(state.get('first_seen_tracking', {})),
        'tested_this_run': tested_this_run,
        'elapsed_seconds': round(elapsed, 3),
        'rate_per_second': round(tested_this_run / elapsed, 3) if elapsed else 0.0,
        'max_elapsed_ms': round(float(state.get('max_elapsed_ms', 0.0)), 6),
        'halted_on': state.get('halted_on'),
    }
    prime_atomic_write_json(summary_path, summary)
    prime_print_progress(state, session_start_tested, started_monotonic, last_pair)
    print(f'results: {output_dir}', flush=True)
    if stop_reason:
        raise SystemExit(1)


plot_STATE_LABEL_6K1 = '6k+/-1 candidates'
plot_STATE_LABEL_PRIME = 'Primes'
plot_SCRIPT_VERSION = '2026-08-05-v18.4.1-flat-index-fix'
# Plotly remains optional: PLOT imports it only when an interactive backend is
# requested.  Declaring the lazy module reference keeps static analyzers aware
# of the symbol without making FILE or RING depend on Plotly.
_go: Any = None

def plot_load_json(path: Path) -> dict[str, Any]:
    try:
        data = json.loads(path.read_text(encoding='utf-8'))
    except (OSError, json.JSONDecodeError) as exc:
        raise SystemExit(f'Error reading {path}: {exc}') from exc
    if not isinstance(data, dict):
        raise SystemExit(f'Invalid format in {path}: expected a JSON object')
    return data

def plot_load_ring_json(path: Path) -> tuple[dict[str, Any], Path]:
    """Load a detailed ring state, resolving it from a summary when possible."""
    supplied = plot_load_json(path)
    if 'ring_counts' in supplied and 'pair_counts' in supplied:
        return supplied, path
    candidates: list[Path] = []
    if path.name.startswith('summary_'):
        candidates.append(path.with_name(f"state_{path.name[len('summary_'):]}"))
    candidates.extend((path.with_name('state_ring.json'), path.with_name('state.json')))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or candidate == path:
            continue
        seen.add(candidate)
        if not candidate.is_file():
            continue
        detailed = plot_load_json(candidate)
        if 'ring_counts' not in detailed or 'pair_counts' not in detailed:
            continue
        merged = dict(detailed)
        merged.update(supplied)
        print(f'Detailed ring state: {candidate}')
        return merged, candidate
    expected = '\n'.join((f'  - {candidate}' for candidate in candidates))
    raise SystemExit(f'{path} is a summary without the data required for ring plots.\nPass the complete state JSON file to --ring, or place it next to the summary using one of these names:\n{expected}')

def plot_load_prime_json(path: Path) -> tuple[dict[str, Any], Path]:
    """Load a detailed PRIME state, resolving it from a summary if needed."""
    supplied = plot_load_json(path)
    if 'ring_counts' in supplied and 'pair_counts' in supplied:
        supplied.setdefault('ring_first', {})
        supplied.setdefault('pair_first', {})
        return supplied, path
    candidates: list[Path] = []
    if path.name.startswith('summary_'):
        candidates.append(path.with_name('state_prime_scan.json'))
        candidates.append(path.with_name(f"state_{path.name[len('summary_'):]}"))
    candidates.extend((path.with_name('state_prime_scan.json'), path.with_name('state_prime.json')))
    seen: set[Path] = set()
    for candidate in candidates:
        if candidate in seen or candidate == path:
            continue
        seen.add(candidate)
        if not candidate.is_file():
            continue
        detailed = plot_load_json(candidate)
        if 'ring_counts' not in detailed or 'pair_counts' not in detailed:
            continue
        merged = dict(detailed)
        merged.update(supplied)
        merged.setdefault('ring_first', detailed.get('ring_first', {}))
        merged.setdefault('pair_first', detailed.get('pair_first', {}))
        print(f'Detailed PRIME state: {candidate}')
        return merged, candidate
    expected = '\n'.join(f'  - {candidate}' for candidate in candidates)
    raise SystemExit(
        f'{path} is a PRIME summary without ring_counts and pair_counts.\n'
        f'Pass state_prime_scan.json to --prime, or place it next to the summary using one of these names:\n{expected}'
    )


def plot_has_pair_discovery(state: dict[str, Any]) -> bool:
    return bool(plot_pair_first(state))


def plot_has_pair_data(state: dict[str, Any]) -> bool:
    """Return True when a state contains pair frequencies for a pyramid."""
    return bool(plot_pair_counts(state))


def plot_has_ring_discovery(state: dict[str, Any]) -> bool:
    return bool(plot_ring_first(state))


def plot_dataset_label(data: dict[str, Any], fallback: str) -> str:
    source = Path(str(data.get('input_file', fallback))).name
    for suffix in ('.bz2', '.gz'):
        if source.lower().endswith(suffix):
            source = source[:-len(suffix)]
    return source.upper() or fallback.upper()

def plot_as_int(value: Any, default: int=0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default

def plot_as_float(value: Any, default: float=0.0) -> float:
    try:
        return float(value)
    except (TypeError, ValueError):
        return default

def plot_ring_counts(state: dict[str, Any]) -> dict[int, int]:
    return {int(key): plot_as_int(value) for key, value in state.get('ring_counts', {}).items()}

def plot_pair_counts(state: dict[str, Any]) -> dict[tuple[int, int], int]:
    result: dict[tuple[int, int], int] = {}
    for key, value in state.get('pair_counts', {}).items():
        try:
            a_text, b_text = str(key).split(',', maxsplit=1)
            result[int(a_text), int(b_text)] = plot_as_int(value)
        except (TypeError, ValueError):
            continue
    return result

def plot_ring_first(state: dict[str, Any]) -> dict[int, dict[str, Any]]:
    result: dict[int, dict[str, Any]] = {}
    for key, value in state.get('ring_first', {}).items():
        if isinstance(value, dict):
            result[int(key)] = value
    return result

def plot_pair_first(state: dict[str, Any]) -> dict[tuple[int, int], dict[str, Any]]:
    result: dict[tuple[int, int], dict[str, Any]] = {}
    for key, value in state.get('pair_first', {}).items():
        try:
            a_text, b_text = str(key).split(',', maxsplit=1)
            if isinstance(value, dict):
                result[int(a_text), int(b_text)] = value
        except (TypeError, ValueError):
            continue
    return result

def plot_safe_percentage(value: float, total: float) -> float:
    return 100.0 * value / total if total else 0.0

def plot_safe_ratio(value: float, base: float) -> float:
    return value / base if base else 0.0

def plot_format_count(value: float | int) -> str:
    return f'{value:,.0f}'

def plot_ensure_positive_for_log(values: Iterable[float | int]) -> bool:
    values = list(values)
    return bool(values) and all((value > 0 for value in values))

def plot_save_static(fig: Any, output_dir: Path, stem: str, formats: tuple[str, ...]) -> None:
    fig.tight_layout()
    for extension in formats:
        path = output_dir / f'{stem}.{extension}'
        fig.savefig(path, dpi=180, bbox_inches='tight')
        print(f'written: {path}')
    fig.clear()

def plot_ordered_union(*collections: Iterable[int]) -> list[int]:
    values: set[int] = set()
    for collection in collections:
        values.update(collection)
    return sorted(values)

def plot_state_summary(state: dict[str, Any], kind: str) -> dict[str, Any]:
    counts = plot_ring_counts(state)
    pairs = plot_pair_counts(state)
    with_pair = sum(counts.values())
    tested = plot_as_int(state.get('tested'))
    result: dict[str, Any] = {'tested': tested, 'with_pair': with_pair, 'without_pair': max(tested - with_pair, 0), 'distinct_pairs': len(pairs), 'max_ring': max(counts, default=0), 'errors': plot_as_int(state.get('errors'))}
    if kind == '6k1':
        result.update(accepted=plot_as_int(state.get('accepted')), rejected=plot_as_int(state.get('rejected')), last_value=plot_as_int(state.get('last_candidate')))
    else:
        result.update(failures=plot_as_int(state.get('failures')), last_value=plot_as_int(state.get('last_prime')))
    return result

def plot_static_validation(plt: Any, summaries: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    labels = [label for label, _ in summaries]
    metrics = (('tested', 'Numbers tested', 'items', True), ('rate_per_second', 'Speed', 'numbers/s', False), ('elapsed_seconds', 'Total time', 'seconds', False), ('max_elapsed_ms', 'Maximum single time', 'ms', False))
    fig, axes = plt.subplots(2, 2, figsize=(12, 8))
    for axis, (key, title, ylabel, use_log) in zip(axes.flat, metrics):
        values = [plot_as_float(data.get(key)) for _, data in summaries]
        bars = axis.bar(labels, values)
        axis.set_title(title)
        axis.set_ylabel(ylabel)
        axis.grid(axis='y', alpha=0.25)
        if use_log and plot_ensure_positive_for_log(values):
            axis.set_yscale('log')
        axis.bar_label(bars, labels=[f'{value:,.6g}' for value in values], padding=3)
    fig.suptitle(f"BRPT - {' and '.join(labels)} validation")
    plot_save_static(fig, output_dir, '01_validation', formats)
    plt.close(fig)

def plot_static_ring_counts(plt: Any, states: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    counts_by_state = [(label, plot_ring_counts(state)) for label, state in states]
    rings = plot_ordered_union(*(counts.keys() for _, counts in counts_by_state))
    width = 0.38
    fig, axis = plt.subplots(figsize=(11, 6))
    for index, (label, counts) in enumerate(counts_by_state):
        offsets = [ring + (index - 0.5) * width for ring in rings]
        values = [counts.get(ring, 0) for ring in rings]
        axis.bar(offsets, values, width=width, label=label)
    axis.set_yscale('log')
    axis.set_xticks(rings)
    axis.set_xlabel('Ring = max(|A|, |B|)')
    axis.set_ylabel('Occurrences (log scale)')
    axis.set_title('Absolute ring distribution')
    axis.grid(axis='y', which='both', alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '02_ring_counts', formats)
    plt.close(fig)

def plot_static_ring_shares(plt: Any, states: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    counts_by_state = [(label, plot_ring_counts(state)) for label, state in states]
    rings = plot_ordered_union(*(counts.keys() for _, counts in counts_by_state))
    fig, axis = plt.subplots(figsize=(11, 6))
    for label, counts in counts_by_state:
        total = sum(counts.values())
        shares = [plot_safe_percentage(counts.get(ring, 0), total) for ring in rings]
        axis.plot(rings, shares, marker='o', label=label)
    axis.set_yscale('log')
    axis.set_xticks(rings)
    axis.set_xlabel('Ring')
    axis.set_ylabel('Share of cases with a pair (%) - log scale')
    axis.set_title('Normalized ring distribution')
    axis.grid(True, which='both', alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '03_ring_share', formats)
    plt.close(fig)

def plot_static_ring_first_seen(plt: Any, states: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    first_by_state = [(label, plot_ring_first(state)) for label, state in states]
    rings = plot_ordered_union(*(first.keys() for _, first in first_by_state))
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
    for label, first in first_by_state:
        available = [ring for ring in rings if ring in first]
        indices = [plot_as_int(first[ring].get('index')) for ring in available]
        values = [plot_as_int(first[ring].get('n')) for ring in available]
        axes[0].plot(available, indices, marker='o', label=label)
        axes[1].plot(available, values, marker='o', label=label)
    for axis, title, ylabel in ((axes[0], 'First occurrence index', 'Index (log scale)'), (axes[1], 'n value at first occurrence', 'n (log scale)')):
        axis.set_yscale('log')
        axis.set_xticks(rings)
        axis.set_xlabel('Ring')
        axis.set_ylabel(ylabel)
        axis.set_title(title)
        axis.grid(True, which='both', alpha=0.25)
        axis.legend()
    fig.suptitle('Ring discovery')
    plot_save_static(fig, output_dir, '04_ring_first_seen', formats)
    plt.close(fig)

def plot_static_pair_heatmap(plt: Any, state: dict[str, Any], label: str, stem: str, output_dir: Path, formats: tuple[str, ...]) -> None:
    pairs = plot_pair_counts(state)
    if not pairs:
        return
    a_values = list(range(min((a for a, _ in pairs)), max((a for a, _ in pairs)) + 1))
    b_values = list(range(min((b for _, b in pairs)), max((b for _, b in pairs)) + 1))
    matrix = [[math.log10(pairs.get((a, b), 0) + 1) for a in a_values] for b in b_values]
    fig, axis = plt.subplots(figsize=(11, 7))
    image = axis.imshow(matrix, origin='lower', aspect='auto')
    axis.set_xticks(range(len(a_values)), a_values)
    axis.set_yticks(range(len(b_values)), b_values)
    axis.set_xlabel('A')
    axis.set_ylabel('B')
    axis.set_title(f'Winning-pair frequency - {label}')
    colorbar = fig.colorbar(image, ax=axis)
    colorbar.set_label('log10(occurrences + 1)')
    for row, b in enumerate(b_values):
        for col, a in enumerate(a_values):
            count = pairs.get((a, b), 0)
            if count:
                axis.text(col, row, f'{count:,}', ha='center', va='center', fontsize=6)
    plot_save_static(fig, output_dir, stem, formats)
    plt.close(fig)

def plot_static_top_pairs(plt: Any, state: dict[str, Any], label: str, stem: str, top_n: int, output_dir: Path, formats: tuple[str, ...]) -> None:
    ranked = sorted(plot_pair_counts(state).items(), key=lambda item: item[1], reverse=True)[:top_n]
    if not ranked:
        return
    labels = [f'({a},{b})' for (a, b), _ in reversed(ranked)]
    values = [value for _, value in reversed(ranked)]
    fig, axis = plt.subplots(figsize=(11, max(6, len(ranked) * 0.38)))
    bars = axis.barh(labels, values)
    axis.set_xscale('log')
    axis.set_xlabel('Occurrences (log scale)')
    axis.set_title(f'Top {len(ranked)} pairs - {label}')
    axis.grid(axis='x', which='both', alpha=0.25)
    axis.bar_label(bars, labels=[f'{value:,}' for value in values], padding=3, fontsize=8)
    plot_save_static(fig, output_dir, stem, formats)
    plt.close(fig)

def plot_static_pair_share_comparison(plt: Any, states: list[tuple[str, dict[str, Any]]], top_n: int, output_dir: Path, formats: tuple[str, ...]) -> None:
    pairs_by_state = [(label, plot_pair_counts(state)) for label, state in states]
    union_totals: dict[tuple[int, int], int] = {}
    for _, pairs in pairs_by_state:
        for pair, value in pairs.items():
            union_totals[pair] = union_totals.get(pair, 0) + value
    selected = [pair for pair, _ in sorted(union_totals.items(), key=lambda item: item[1], reverse=True)[:top_n]]
    labels = [f'({a},{b})' for a, b in selected]
    x = list(range(len(selected)))
    width = 0.38
    fig, axis = plt.subplots(figsize=(max(12, top_n * 0.65), 6.5))
    for index, (state_label, pairs) in enumerate(pairs_by_state):
        total = sum(pairs.values())
        shares = [plot_safe_percentage(pairs.get(pair, 0), total) for pair in selected]
        offsets = [value + (index - 0.5) * width for value in x]
        axis.bar(offsets, shares, width=width, label=state_label)
    axis.set_yscale('log')
    axis.set_xticks(x, labels, rotation=45, ha='right')
    axis.set_ylabel('Quota sui casi con coppia (%) - scala log')
    axis.set_title(f'Confronto normalizzato delle prime {len(selected)} coppie')
    axis.grid(axis='y', which='both', alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '10_pair_share_comparison', formats)
    plt.close(fig)

def plot_static_state_outcomes(plt: Any, state_6k1: dict[str, Any], state_prime: dict[str, Any], output_dir: Path, formats: tuple[str, ...]) -> None:
    summary_6k1 = plot_state_summary(state_6k1, '6k1')
    summary_prime = plot_state_summary(state_prime, 'prime')
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
    categories_6k1 = ['Accettati', 'Rifiutati', 'Errori']
    values_6k1 = [summary_6k1['accepted'], summary_6k1['rejected'], summary_6k1['errors']]
    bars = axes[0].bar(categories_6k1, values_6k1)
    axes[0].set_yscale('symlog', linthresh=1)
    axes[0].bar_label(bars, labels=[plot_format_count(value) for value in values_6k1], padding=3)
    axes[0].set_title('Esiti scansione candidati 6k+/-1')
    axes[0].set_ylabel('Casi')
    axes[0].grid(axis='y', alpha=0.25)
    categories_prime = ['Con coppia', 'Senza coppia', 'Failure', 'Errori']
    values_prime = [summary_prime['with_pair'], summary_prime['without_pair'], summary_prime['failures'], summary_prime['errors']]
    bars = axes[1].bar(categories_prime, values_prime)
    axes[1].set_yscale('symlog', linthresh=1)
    axes[1].bar_label(bars, labels=[plot_format_count(value) for value in values_prime], padding=3)
    axes[1].set_title('Copertura scansione primi')
    axes[1].set_ylabel('Casi')
    axes[1].grid(axis='y', alpha=0.25)
    fig.suptitle('BRPT - ring state summary')
    plot_save_static(fig, output_dir, '11_state_outcomes', formats)
    plt.close(fig)

def plot_static_coefficient_marginals(plt: Any, states: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.5))
    for label, state in states:
        pairs = plot_pair_counts(state)
        total = sum(pairs.values())
        a_counts: dict[int, int] = {}
        b_counts: dict[int, int] = {}
        for (a, b), value in pairs.items():
            a_counts[a] = a_counts.get(a, 0) + value
            b_counts[b] = b_counts.get(b, 0) + value
        axes[0].plot(sorted(a_counts), [plot_safe_percentage(a_counts[a], total) for a in sorted(a_counts)], marker='o', label=label)
        axes[1].plot(sorted(b_counts), [plot_safe_percentage(b_counts[b], total) for b in sorted(b_counts)], marker='o', label=label)
    for axis, coefficient in zip(axes, ('A', 'B')):
        axis.set_yscale('log')
        axis.set_xlabel(coefficient)
        axis.set_ylabel('Share (%) - log scale')
        axis.set_title(f'Marginal distribution of {coefficient}')
        axis.grid(True, which='both', alpha=0.25)
        axis.legend()
    fig.suptitle('Normalized coefficient marginal distributions')
    plot_save_static(fig, output_dir, '08_coefficient_marginals', formats)
    plt.close(fig)

def plot_static_ring_cumulative(plt: Any, states: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    """Plot cumulative coverage as the maximum permitted ring increases."""
    counts_by_state = [(label, plot_ring_counts(state)) for label, state in states]
    rings = plot_ordered_union(*(counts.keys() for _, counts in counts_by_state))
    if not rings:
        return
    fig, axis = plt.subplots(figsize=(11, 6))
    for label, counts in counts_by_state:
        total = sum(counts.values())
        running = 0
        cumulative: list[float] = []
        for ring in rings:
            running += counts.get(ring, 0)
            cumulative.append(plot_safe_percentage(running, total))
        axis.plot(rings, cumulative, marker='o', label=label)
    axis.set_xticks(rings)
    axis.set_ylim(0, 101)
    axis.set_xlabel('Maximum ring')
    axis.set_ylabel('Cumulative coverage (%)')
    axis.set_title('Cumulative ring coverage')
    axis.grid(True, alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '05_ring_cumulative', formats)
    plt.close(fig)

def plot_static_pair_share_correlation(plt: Any, state_6k1: dict[str, Any], state_prime: dict[str, Any], output_dir: Path, formats: tuple[str, ...]) -> None:
    """Compare the relative frequencies of pairs shared by both scans."""
    pairs_6k1 = plot_pair_counts(state_6k1)
    pairs_prime = plot_pair_counts(state_prime)
    shared = sorted(set(pairs_6k1) & set(pairs_prime))
    total_6k1 = sum(pairs_6k1.values())
    total_prime = sum(pairs_prime.values())
    rows = [(pair, plot_safe_percentage(pairs_6k1[pair], total_6k1), plot_safe_percentage(pairs_prime[pair], total_prime)) for pair in shared if pairs_6k1[pair] > 0 and pairs_prime[pair] > 0]
    if not rows:
        return
    fig, axis = plt.subplots(figsize=(8, 7))
    x_values = [row[1] for row in rows]
    y_values = [row[2] for row in rows]
    axis.scatter(x_values, y_values)
    lower = min(min(x_values), min(y_values))
    upper = max(max(x_values), max(y_values))
    axis.plot([lower, upper], [lower, upper], linestyle='--', label='y = x')
    for (a, b), x_value, y_value in rows:
        axis.annotate(f'({a},{b})', (x_value, y_value), fontsize=7, xytext=(3, 3), textcoords='offset points')
    axis.set_xscale('log')
    axis.set_yscale('log')
    axis.set_xlabel('Quota nei candidati 6k+/-1 (%)')
    axis.set_ylabel('Quota nei primi (%)')
    axis.set_title('Stabilita delle frequenze relative delle coppie')
    axis.grid(True, which='both', alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '13_pair_share_correlation', formats)
    plt.close(fig)

def plot_static_3d_frequency(plt: Any, state: dict[str, Any], label: str, stem: str, output_dir: Path, formats: tuple[str, ...]) -> None:
    """Render the frequency landscape of coefficient pairs as a 3D scatter."""
    pairs = plot_pair_counts(state)
    first = plot_pair_first(state)
    if not pairs:
        return
    rows = []
    for (a, b), frequency in pairs.items():
        record = first.get((a, b), {})
        rows.append((a, b, max(abs(a), abs(b)), frequency, plot_as_int(record.get('index')), plot_as_int(record.get('n'))))
    fig = plt.figure(figsize=(11, 8))
    axis = fig.add_subplot(111, projection='3d')
    z_values = [math.log10(max(1, row[3])) for row in rows]
    sizes = [25 + 16 * value for value in z_values]
    scatter = axis.scatter([row[0] for row in rows], [row[1] for row in rows], z_values, s=sizes, c=[row[2] for row in rows], alpha=0.82)
    for row, z_value in zip(rows, z_values):
        axis.text(row[0], row[1], z_value, f'({row[0]},{row[1]})', fontsize=6)
    axis.set_xlabel('A')
    axis.set_ylabel('B')
    axis.set_zlabel('log10(frequency)')
    axis.set_title(f'3D frequency landscape - {label}')
    colorbar = fig.colorbar(scatter, ax=axis, pad=0.1, shrink=0.75)
    colorbar.set_label('Ring')
    plot_save_static(fig, output_dir, stem, formats)
    plt.close(fig)

def plot_static_3d_birth(plt: Any, state: dict[str, Any], label: str, stem: str, output_dir: Path, formats: tuple[str, ...]) -> None:
    """Render the chronological discovery path of coefficient pairs in 3D."""
    pairs = plot_pair_counts(state)
    first = plot_pair_first(state)
    rows = []
    for (a, b), record in first.items():
        n_value = plot_as_int(record.get('n'))
        index = plot_as_int(record.get('index'))
        if n_value > 0:
            rows.append((a, b, max(abs(a), abs(b)), index, n_value, pairs.get((a, b), 0)))
    rows.sort(key=lambda row: row[3])
    if not rows:
        return
    fig = plt.figure(figsize=(11, 8))
    axis = fig.add_subplot(111, projection='3d')
    z_values = [math.log10(row[4]) for row in rows]
    sizes = [25 + 14 * math.log10(row[5] + 1) for row in rows]
    axis.plot([row[0] for row in rows], [row[1] for row in rows], z_values, linewidth=1.5)
    scatter = axis.scatter([row[0] for row in rows], [row[1] for row in rows], z_values, s=sizes, c=[row[2] for row in rows])
    axis.set_xlabel('A')
    axis.set_ylabel('B')
    axis.set_zlabel('log10(n at discovery)')
    axis.set_title(f'3D pair discovery timeline - {label}')
    colorbar = fig.colorbar(scatter, ax=axis, pad=0.1, shrink=0.75)
    colorbar.set_label('Ring')
    plot_save_static(fig, output_dir, stem, formats)
    plt.close(fig)

def plot_static_3d_pyramid(plt: Any, state: dict[str, Any], label: str, output_dir: Path, formats: tuple[str, ...]) -> None:
    """Render the 3D ring pyramid, using a frequency-only view when needed."""
    from mpl_toolkits.mplot3d.art3d import Poly3DCollection

    rows = plot_build_rows(state, mirror=True)
    if not rows:
        return
    rings = sorted({int(row['ring']) for row in rows})
    birth_available = all(int(row.get('n_first', 0)) > 0 for row in rows)
    birth_values = [math.log10(max(int(row.get('n_first', 0)), 1)) for row in rows]
    count_values = [math.log10(max(int(row['count']), 1)) for row in rows]
    birth_minima, birth_maxima, birth_spans = plot_ring_statistics(rows, birth_values)
    count_minima, count_maxima, count_spans = plot_ring_statistics(rows, count_values)
    birth_floors = plot_compute_mode_floors(rings, birth_minima, birth_maxima, birth_spans, 'auto', 0.1, reverse=False)
    count_floors = plot_compute_mode_floors(rings, count_minima, count_maxima, count_spans, 'auto', 0.1, reverse=True)
    columns = 2 if birth_available else 1
    fig = plt.figure(figsize=(17, 8.5) if birth_available else (10.5, 8.5))

    def draw_view(position: int, raw_values: list[float], floors: dict[int, float], title: str, color_map: str, color_label: str) -> None:
        axis = fig.add_subplot(1, columns, position, projection='3d')
        displayed = [floors[int(row['ring'])] + value for row, value in zip(rows, raw_values)]
        points_by_ring: dict[int, list[tuple[dict[str, Any], float]]] = {}
        for row, height in zip(rows, displayed):
            points_by_ring.setdefault(int(row['ring']), []).append((row, height))
        cmap = plt.get_cmap(color_map)
        for ring_index, ring in enumerate(rings):
            floor = floors[ring]
            color = cmap(ring_index / max(len(rings) - 1, 1))
            vertices = [[(-ring, -ring, floor), (-ring, ring, floor), (ring, ring, floor), (ring, -ring, floor)]]
            plane = Poly3DCollection(vertices, facecolors=[color], edgecolors=[color], linewidths=0.8, alpha=0.09)
            axis.add_collection3d(plane)
            perimeter_x = [-ring, -ring, ring, ring, -ring]
            perimeter_y = [-ring, ring, ring, -ring, -ring]
            axis.plot(perimeter_x, perimeter_y, [floor] * 5, color=color, linewidth=1.2, alpha=0.65)
            ordered = sorted(points_by_ring.get(ring, []), key=lambda item: math.atan2(item[0]['b'], item[0]['a']))
            if ordered:
                closed = [*ordered, ordered[0]]
                axis.plot([item[0]['a'] for item in closed], [item[0]['b'] for item in closed], [item[1] for item in closed], color=color, linewidth=1.0, alpha=0.55)
                for row, height in ordered:
                    axis.plot([row['a'], row['a']], [row['b'], row['b']], [floor, height], color=color, linewidth=0.55, alpha=0.28)
            axis.text(-ring, -ring, floor, f' R{ring}', color=color, fontsize=7)
        scatter = axis.scatter([row['a'] for row in rows], [row['b'] for row in rows], displayed, c=raw_values, cmap=color_map, s=[18 + 4 * int(row['ring']) for row in rows], alpha=0.88, depthshade=True)
        axis.set_xlabel('A')
        axis.set_ylabel('B')
        axis.set_zlabel('Ring planes + data height')
        axis.set_title(title)
        axis.view_init(elev=26, azim=-52)
        axis.set_box_aspect((1, 1, 0.85))
        colorbar = fig.colorbar(scatter, ax=axis, pad=0.08, shrink=0.68)
        colorbar.set_label(color_label)

    if birth_available:
        draw_view(1, birth_values, birth_floors, 'First appearances', 'viridis', 'log10(first n)')
        draw_view(2, count_values, count_floors, 'Pair frequencies', 'plasma', 'log10(count)')
    else:
        draw_view(1, count_values, count_floors, 'Pair frequencies', 'plasma', 'log10(count)')
    subtitle = '' if birth_available else ' — frequency view; historical pair_first unavailable'
    fig.suptitle(f'BRPT - Static 3D ring pyramid - {label}{subtitle}', fontsize=15)
    plot_save_static(fig, output_dir, '16_3d_pyramid', formats)
    plt.close(fig)

def plot_static_pair_cumulative_coverage(plt: Any, states: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    """Plot coverage obtained by retaining the most frequent pairs."""
    fig, axis = plt.subplots(figsize=(11, 6))
    plotted = False
    for label, state in states:
        ranked = sorted(plot_pair_counts(state).values(), reverse=True)
        total = sum(ranked)
        if not ranked or not total:
            continue
        running = 0
        cumulative = []
        for value in ranked:
            running += value
            cumulative.append(plot_safe_percentage(running, total))
        axis.plot(range(1, len(cumulative) + 1), cumulative, marker='o', label=label)
        plotted = True
    if not plotted:
        plt.close(fig)
        return
    axis.set_xlabel('Number of most frequent pairs')
    axis.set_ylabel('Cumulative coverage (%)')
    axis.set_title('Cumulative pair coverage')
    axis.set_ylim(0, 101)
    axis.grid(True, alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '11_pair_cumulative_coverage', formats)
    plt.close(fig)

def plot_static_pair_relative_enrichment(plt: Any, state_6k1: dict[str, Any], state_prime: dict[str, Any], top_n: int, output_dir: Path, formats: tuple[str, ...]) -> None:
    """Rank shared pairs by relative enrichment in the prime scan."""
    pairs_6k1 = plot_pair_counts(state_6k1)
    pairs_prime = plot_pair_counts(state_prime)
    total_6k1 = sum(pairs_6k1.values())
    total_prime = sum(pairs_prime.values())
    rows = []
    for pair in sorted(set(pairs_6k1) & set(pairs_prime)):
        share_6k1 = plot_safe_ratio(pairs_6k1[pair], total_6k1)
        share_prime = plot_safe_ratio(pairs_prime[pair], total_prime)
        if share_6k1 > 0 and share_prime > 0:
            rows.append((pair, math.log2(share_prime / share_6k1)))
    rows.sort(key=lambda row: abs(row[1]), reverse=True)
    rows = rows[:max(top_n, 20)]
    rows.reverse()
    if not rows:
        return
    labels = [f'({pair[0]},{pair[1]})' for pair, _ in rows]
    values = [value for _, value in rows]
    fig, axis = plt.subplots(figsize=(11, max(6.5, len(rows) * 0.34)))
    axis.barh(labels, values)
    axis.axvline(0, linestyle='--')
    axis.set_xlabel('log2(quota primi / quota candidati 6k+/-1)')
    axis.set_ylabel('Coppia (A,B)')
    axis.set_title('Arricchimento relativo delle coppie')
    axis.grid(axis='x', alpha=0.25)
    plot_save_static(fig, output_dir, '19_pair_relative_enrichment', formats)
    plt.close(fig)

def plot_static_ring_survival(plt: Any, states: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    """Plot the ring survival function P(R >= r)."""
    counts_by_state = [(label, plot_ring_counts(state)) for label, state in states]
    rings = plot_ordered_union(*(counts.keys() for _, counts in counts_by_state))
    if not rings:
        return
    fig, axis = plt.subplots(figsize=(11, 6))
    for label, counts in counts_by_state:
        total = sum(counts.values())
        survival = [plot_safe_percentage(sum((value for ring_value, value in counts.items() if ring_value >= ring)), total) for ring in rings]
        axis.plot(rings, survival, marker='o', label=label)
    axis.set_yscale('log')
    axis.set_xticks(rings)
    axis.set_xlabel('Minimum required ring')
    axis.set_ylabel('P(R >= r) (%) - log scale')
    axis.set_title('Ring survival function')
    axis.grid(True, which='both', alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '12_ring_survival', formats)
    plt.close(fig)

def plot_static_pair_rank_stability(plt: Any, state_6k1: dict[str, Any], state_prime: dict[str, Any], output_dir: Path, formats: tuple[str, ...]) -> None:
    """Compare frequency ranks assigned to shared pairs."""
    pairs_6k1 = plot_pair_counts(state_6k1)
    pairs_prime = plot_pair_counts(state_prime)
    shared = sorted(set(pairs_6k1) & set(pairs_prime))
    if not shared:
        return
    rank_6k1 = {pair: rank for rank, (pair, _) in enumerate(sorted(pairs_6k1.items(), key=lambda item: item[1], reverse=True), 1)}
    rank_prime = {pair: rank for rank, (pair, _) in enumerate(sorted(pairs_prime.items(), key=lambda item: item[1], reverse=True), 1)}
    rows = [(pair, rank_6k1[pair], rank_prime[pair]) for pair in shared]
    fig, axis = plt.subplots(figsize=(8, 7))
    axis.scatter([row[1] for row in rows], [row[2] for row in rows])
    max_rank = max((max(row[1], row[2]) for row in rows))
    axis.plot([1, max_rank], [1, max_rank], linestyle='--', label='stesso rango')
    for (a, b), x_value, y_value in rows:
        axis.annotate(f'({a},{b})', (x_value, y_value), fontsize=7, xytext=(3, 3), textcoords='offset points')
    axis.set_xscale('log')
    axis.set_yscale('log')
    axis.set_xlabel('Rango nei candidati 6k+/-1')
    axis.set_ylabel('Rango nei primi')
    axis.set_title('Stabilita del rango delle coppie')
    axis.grid(True, which='both', alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '21_pair_rank_stability', formats)
    plt.close(fig)

def plot_static_pair_birth_index_comparison(plt: Any, state_6k1: dict[str, Any], state_prime: dict[str, Any], output_dir: Path, formats: tuple[str, ...]) -> None:
    """Compare first-seen indices for coefficient pairs shared by both scans."""
    first_6k1 = plot_pair_first(state_6k1)
    first_prime = plot_pair_first(state_prime)
    rows = []
    for pair in sorted(set(first_6k1) & set(first_prime)):
        index_6k1 = plot_as_int(first_6k1[pair].get('index'))
        index_prime = plot_as_int(first_prime[pair].get('index'))
        if index_6k1 > 0 and index_prime > 0:
            rows.append((pair, index_6k1, index_prime))
    if not rows:
        return
    fig, axis = plt.subplots(figsize=(8, 7))
    x_values = [row[1] for row in rows]
    y_values = [row[2] for row in rows]
    axis.scatter(x_values, y_values)
    lower = min(min(x_values), min(y_values))
    upper = max(max(x_values), max(y_values))
    axis.plot([lower, upper], [lower, upper], linestyle='--', label='y = x')
    for (a, b), x_value, y_value in rows:
        axis.annotate(f'({a},{b})', (x_value, y_value), fontsize=7, xytext=(3, 3), textcoords='offset points')
    axis.set_xscale('log')
    axis.set_yscale('log')
    axis.set_xlabel('Indice prima comparsa nei candidati 6k+/-1')
    axis.set_ylabel('Indice prima comparsa nei primi')
    axis.set_title('Confronto degli indici di nascita delle coppie')
    axis.grid(True, which='both', alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '22_pair_birth_index_comparison', formats)
    plt.close(fig)

def plot_static_pair_first_elapsed_comparison(plt: Any, state_6k1: dict[str, Any], state_prime: dict[str, Any], output_dir: Path, formats: tuple[str, ...]) -> None:
    """Compare elapsed times recorded at the first appearance of shared pairs."""
    first_6k1 = plot_pair_first(state_6k1)
    first_prime = plot_pair_first(state_prime)
    rows = []
    for pair in sorted(set(first_6k1) & set(first_prime)):
        elapsed_6k1 = plot_as_float(first_6k1[pair].get('elapsed_ms'))
        elapsed_prime = plot_as_float(first_prime[pair].get('elapsed_ms'))
        if elapsed_6k1 > 0 and elapsed_prime > 0:
            rows.append((pair, elapsed_6k1, elapsed_prime))
    if not rows:
        return
    fig, axis = plt.subplots(figsize=(8, 7))
    x_values = [row[1] for row in rows]
    y_values = [row[2] for row in rows]
    axis.scatter(x_values, y_values)
    lower = min(min(x_values), min(y_values))
    upper = max(max(x_values), max(y_values))
    axis.plot([lower, upper], [lower, upper], linestyle='--', label='y = x')
    for (a, b), x_value, y_value in rows:
        axis.annotate(f'({a},{b})', (x_value, y_value), fontsize=7, xytext=(3, 3), textcoords='offset points')
    axis.set_xscale('log')
    axis.set_yscale('log')
    axis.set_xlabel('Elapsed nei candidati 6k+/-1 (ms)')
    axis.set_ylabel('Elapsed nei primi (ms)')
    axis.set_title('Confronto dei tempi alla prima comparsa')
    axis.grid(True, which='both', alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '23_pair_first_elapsed_comparison', formats)
    plt.close(fig)

def plot_static_discriminant_vs_relative_frequency(plt: Any, states: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    """Relate absolute cubic discriminants to pair frequencies."""
    fig, axis = plt.subplots(figsize=(10, 7))
    plotted = False
    for label, state in states:
        pairs = plot_pair_counts(state)
        total = sum(pairs.values())
        rows = []
        for (a, b), value in pairs.items():
            discriminant = abs(-4 * a ** 3 - 27 * b ** 2)
            share = plot_safe_percentage(value, total)
            if discriminant > 0 and share > 0:
                rows.append((a, b, discriminant, share))
        if not rows:
            continue
        axis.scatter([row[2] for row in rows], [row[3] for row in rows], label=label)
        for a, b, x_value, y_value in rows:
            axis.annotate(f'({a},{b})', (x_value, y_value), fontsize=6, xytext=(3, 3), textcoords='offset points')
        plotted = True
    if not plotted:
        plt.close(fig)
        return
    axis.set_xscale('log')
    axis.set_yscale('log')
    axis.set_xlabel('|-4A^3-27B^2|')
    axis.set_ylabel('Share (%)')
    axis.set_title('Discriminant and relative pair frequency')
    axis.grid(True, which='both', alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '13_discriminant_vs_relative_frequency', formats)
    plt.close(fig)

def plot_static_ring_pair_diversity_entropy(plt: Any, states: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    """Plot distinct-pair counts and effective entropy diversity by ring."""
    pairs_by_state = [(label, plot_pair_counts(state)) for label, state in states]
    rings = plot_ordered_union(*((max(abs(a), abs(b)) for a, b in pairs) for _, pairs in pairs_by_state))
    if not rings:
        return
    fig, axes = plt.subplots(1, 2, figsize=(14, 5.8))
    width = 0.38
    for index, (label, pairs) in enumerate(pairs_by_state):
        distinct_values = []
        effective_values = []
        for ring in rings:
            values = [value for (a, b), value in pairs.items() if max(abs(a), abs(b)) == ring and value > 0]
            distinct_values.append(len(values))
            ring_total = sum(values)
            entropy = -sum((value / ring_total * math.log(value / ring_total) for value in values)) if ring_total else 0.0
            effective_values.append(math.exp(entropy) if values else 0.0)
        offsets = [ring + (index - 0.5) * width for ring in rings]
        axes[0].bar(offsets, distinct_values, width=width, label=label)
        axes[1].plot(rings, effective_values, marker='o', label=label)
    axes[0].set_xticks(rings)
    axes[0].set_xlabel('Ring')
    axes[0].set_ylabel('Number of pairs')
    axes[0].set_title('Distinct pairs by ring')
    axes[0].grid(axis='y', alpha=0.25)
    axes[0].legend()
    axes[1].set_xticks(rings)
    axes[1].set_xlabel('Ring')
    axes[1].set_ylabel('Effective diversity exp(H)')
    axes[1].set_title('Effective number of pairs')
    axes[1].grid(True, alpha=0.25)
    axes[1].legend()
    fig.suptitle('Pair diversity and entropy by ring')
    plot_save_static(fig, output_dir, '14_ring_pair_diversity_entropy', formats)
    plt.close(fig)

def plot_static_pair_discovery_curve(plt: Any, states: list[tuple[str, dict[str, Any]]], output_dir: Path, formats: tuple[str, ...]) -> None:
    """Plot cumulative discovery of distinct coefficient pairs."""
    fig, axis = plt.subplots(figsize=(11, 6))
    plotted = False
    for label, state in states:
        discoveries = sorted((plot_as_int(record.get('n')) for record in plot_pair_first(state).values() if plot_as_int(record.get('n')) > 0))
        if not discoveries:
            continue
        axis.plot(discoveries, range(1, len(discoveries) + 1), marker='o', label=label)
        plotted = True
    if not plotted:
        plt.close(fig)
        return
    axis.set_xscale('log')
    axis.set_xlabel('n value at first occurrence')
    axis.set_ylabel('Cumulative distinct pairs')
    axis.set_title('Distinct-pair discovery curve')
    axis.grid(True, which='both', alpha=0.25)
    axis.legend()
    plot_save_static(fig, output_dir, '15_pair_discovery_curve', formats)
    plt.close(fig)

def plot_static_pair_distribution_difference(plt: Any, state_6k1: dict[str, Any], state_prime: dict[str, Any], top_n: int, output_dir: Path, formats: tuple[str, ...]) -> None:
    """Rank pairs by their contribution to the difference of distributions."""
    pairs_6k1 = plot_pair_counts(state_6k1)
    pairs_prime = plot_pair_counts(state_prime)
    total_6k1 = sum(pairs_6k1.values())
    total_prime = sum(pairs_prime.values())
    rows = []
    for pair in sorted(set(pairs_6k1) | set(pairs_prime)):
        share_6k1 = plot_safe_percentage(pairs_6k1.get(pair, 0), total_6k1)
        share_prime = plot_safe_percentage(pairs_prime.get(pair, 0), total_prime)
        rows.append((pair, share_prime - share_6k1))
    rows.sort(key=lambda row: abs(row[1]), reverse=True)
    rows = rows[:max(top_n, 20)]
    rows.reverse()
    if not rows:
        return
    labels = [f'({pair[0]},{pair[1]})' for pair, _ in rows]
    values = [value for _, value in rows]
    fig, axis = plt.subplots(figsize=(11, max(6.5, len(rows) * 0.34)))
    axis.barh(labels, values)
    axis.axvline(0, linestyle='--')
    axis.set_xlabel('Quota primi - quota candidati 6k+/-1 (punti percentuali)')
    axis.set_ylabel('Coppia (A,B)')
    axis.set_title('Coppie responsabili delle differenze fra le distribuzioni')
    axis.grid(axis='x', alpha=0.25)
    plot_save_static(fig, output_dir, '27_pair_distribution_difference', formats)
    plt.close(fig)

def plot_build_rows(state: dict[str, Any], mirror: bool) -> list[dict[str, Any]]:
    """Build pyramid rows from every measured pair frequency.

    ``pair_first`` enriches rows with chronological discovery information, but
    it is not required.  This lets legacy PRIME checkpoints still produce a
    correct frequency pyramid without inventing historical first appearances.
    """
    measured_rows: list[dict[str, Any]] = []
    first_records = state.get('pair_first', {})
    count_records = state.get('pair_counts', {})
    keys = list(count_records)
    for key in first_records:
        if key not in count_records:
            keys.append(key)
    for key in keys:
        record = first_records.get(key, {})
        try:
            key_a, key_b = str(key).split(',', maxsplit=1)
            a = int(record.get('a', key_a))
            b = int(record.get('b', key_b))
        except (TypeError, ValueError):
            continue
        n_first = plot_as_int(record.get('n'))
        ring = plot_as_int(record.get('ring'), max(abs(a), abs(b)))
        count = plot_as_int(count_records.get(key))
        if count <= 0:
            continue
        measured_rows.append({
            'a': a,
            'b': b,
            'n_first': n_first,
            'birth_known': n_first > 0,
            'count': count,
            'ring': ring,
            'source': 'Measured',
        })
    if not mirror:
        return measured_rows
    rows = list(measured_rows)
    occupied = {(row['a'], row['b']) for row in measured_rows}
    for row in measured_rows:
        a = row['a']
        b = row['b']
        for mirrored_a, mirrored_b in ((-a, b), (a, -b), (-a, -b)):
            coordinate = (mirrored_a, mirrored_b)
            if coordinate in occupied:
                continue
            mirrored_row = dict(row)
            mirrored_row.update({'a': mirrored_a, 'b': mirrored_b, 'source': 'Mirrored'})
            rows.append(mirrored_row)
            occupied.add(coordinate)
    return rows

def plot_make_hover_text(row: dict[str, Any], mode: str) -> str:
    a = row['a']
    b = row['b']
    ring = row['ring']
    n_first = row['n_first']
    count = row['count']
    source = row['source']
    if mode == 'birth':
        if n_first > 0:
            return f'A={a}<br>B={b}<br>Ring={ring}<br>First appearance n={n_first:,}<br>log10(n)={math.log10(n_first):.6f}<br>pair_count={count:,}<br>Source={source}'
        return f'A={a}<br>B={b}<br>Ring={ring}<br>First appearance unavailable<br>pair_count={count:,}<br>Source={source}'
    first_text = f'{n_first:,}' if n_first > 0 else 'unavailable'
    return f'A={a}<br>B={b}<br>Ring={ring}<br>pair_count={count:,}<br>log10(pair_count)={math.log10(max(count, 1)):.6f}<br>First appearance n={first_text}<br>Source={source}'

def plot_vertical_lines(rows: list[dict[str, Any]], z_values: list[float], plane_offsets: dict[int, float], visible: bool) -> _go.Scatter3d:
    x: list[float | None] = []
    y: list[float | None] = []
    z: list[float | None] = []
    for row, height in zip(rows, z_values):
        floor = plane_offsets.get(int(row['ring']), 0.0)
        x.extend((row['a'], row['a'], None))
        y.extend((row['b'], row['b'], None))
        z.extend((floor, height, None))
    return _go.Scatter3d(x=x, y=y, z=z, mode='lines', line={'width': 2}, opacity=0.35, hoverinfo='skip', meta={'kind': 'vertical_lines'}, showlegend=False, visible=visible)

def plot_symmetric_ring_coordinates(points: list[tuple[dict[str, Any], float]]) -> dict[tuple[int, int], tuple[float, float, float]]:
    """Complete missing ring coordinates by A/B reflection.

    Measured points are inserted first and are never overwritten; symmetry is
    used only to create coordinates that are absent from the input data.
    """
    coordinates: dict[tuple[int, int], tuple[float, float, float]] = {
        (int(row['a']), int(row['b'])): (float(row['a']), float(row['b']), float(height))
        for row, height in points
    }
    for (a, b), (_, _, height) in list(coordinates.items()):
        for mirrored_a, mirrored_b in ((a, b), (-a, b), (a, -b), (-a, -b)):
            coordinates.setdefault((mirrored_a, mirrored_b), (float(mirrored_a), float(mirrored_b), height))
    return coordinates

def plot_ring_connection_traces(rows: list[dict[str, Any]], z_values: list[float], visible: bool) -> list[_go.Scatter3d]:
    """Connect all points belonging to the same ring in perimeter order."""
    points_by_ring: dict[int, list[tuple[dict[str, Any], float]]] = {}
    for row, height in zip(rows, z_values):
        points_by_ring.setdefault(row['ring'], []).append((row, height))
    traces: list[_go.Scatter3d] = []
    for ring in sorted(points_by_ring):
        points = sorted(points_by_ring[ring], key=lambda item: math.atan2(item[0]['b'], item[0]['a']))
        if len(points) < 2:
            continue
        symmetric_points = sorted(plot_symmetric_ring_coordinates(points).values(), key=lambda point: math.atan2(point[1], point[0]))
        closed_points = [*symmetric_points, symmetric_points[0]]
        traces.append(_go.Scatter3d(x=[point[0] for point in closed_points], y=[point[1] for point in closed_points], z=[point[2] for point in closed_points], mode='lines', line={'width': 4, 'color': 'rgba(55, 70, 90, 0.7)'}, hovertemplate=f'Ring {ring}<extra></extra>', name=f'Ring {ring} connections', meta={'kind': 'ring_connection', 'ring': ring}, showlegend=False, visible=visible))
    return traces

def plot_evaluate_open_polyline(points: list[tuple[float, float, float]], parameter: float) -> tuple[float, float, float]:
    """Evaluate a piecewise-linear border at parameter 0..1."""
    if len(points) == 1:
        return points[0]
    if parameter >= 1.0:
        return points[-1]
    scaled = max(0.0, parameter) * (len(points) - 1)
    index = min(int(scaled), len(points) - 2)
    t = scaled - index
    start = points[index]
    end = points[index + 1]
    return (
        start[0] + t * (end[0] - start[0]),
        start[1] + t * (end[1] - start[1]),
        start[2] + t * (end[2] - start[2]),
    )

def plot_coons_patch_grid(points: list[tuple[dict[str, Any], float]], ring: int, grid_size: int=33) -> list[list[tuple[float, float, float]]] | None:
    """Build a Coons grid following the four interpolated edges of a ring."""
    interior_relief = 0.35
    unique_points = plot_symmetric_ring_coordinates(points)
    corners = ((-ring, -ring), (ring, -ring), (-ring, ring), (ring, ring))
    if not all((corner in unique_points for corner in corners)):
        return None
    bottom = sorted((point for (a, b), point in unique_points.items() if b == -ring), key=lambda point: point[0])
    top = sorted((point for (a, b), point in unique_points.items() if b == ring), key=lambda point: point[0])
    left = sorted((point for (a, b), point in unique_points.items() if a == -ring), key=lambda point: point[1])
    right = sorted((point for (a, b), point in unique_points.items() if a == ring), key=lambda point: point[1])
    if min(map(len, (bottom, top, left, right))) < 2:
        return None
    p00 = unique_points[-ring, -ring]
    p10 = unique_points[ring, -ring]
    p01 = unique_points[-ring, ring]
    p11 = unique_points[ring, ring]
    grid: list[list[tuple[float, float, float]]] = []
    for v_index in range(grid_size):
        v = v_index / (grid_size - 1)
        left_point = plot_evaluate_open_polyline(left, v)
        right_point = plot_evaluate_open_polyline(right, v)
        row_points: list[tuple[float, float, float]] = []
        for u_index in range(grid_size):
            u = u_index / (grid_size - 1)
            bottom_point = plot_evaluate_open_polyline(bottom, u)
            top_point = plot_evaluate_open_polyline(top, u)
            coordinates: list[float] = []
            for axis in range(3):
                blended_edges = (1.0 - v) * bottom_point[axis] + v * top_point[axis] + (1.0 - u) * left_point[axis] + u * right_point[axis]
                blended_corners = (1.0 - u) * (1.0 - v) * p00[axis] + u * (1.0 - v) * p10[axis] + (1.0 - u) * v * p01[axis] + u * v * p11[axis]
                standard_coons = blended_edges - blended_corners
                coons_deviation = standard_coons - blended_corners
                center_weight = 16.0 * u * (1.0 - u) * v * (1.0 - v)
                relief_factor = 1.0 - (1.0 - interior_relief) * center_weight
                coordinates.append(blended_corners + relief_factor * coons_deviation)
            row_points.append((coordinates[0], coordinates[1], coordinates[2]))
        grid.append(row_points)
    return grid

def plot_point_is_inside_previous_ring_projection(x: float, y: float, ring: int, tolerance: float=1e-09) -> bool:
    """Return True when a point lies inside the projected footprint of ring-1."""
    if ring <= 1:
        return False
    inner_limit = float(ring - 1)
    return abs(float(x)) < inner_limit - tolerance and abs(float(y)) < inner_limit - tolerance

def plot_cell_is_inside_previous_ring_projection(vertices: list[tuple[float, float, float]], ring: int) -> bool:
    """Return True when a quadrilateral cell belongs to the inner hole."""
    if ring <= 1:
        return False
    center_x = sum((vertex[0] for vertex in vertices)) / len(vertices)
    center_y = sum((vertex[1] for vertex in vertices)) / len(vertices)
    return plot_point_is_inside_previous_ring_projection(center_x, center_y, ring)

def plot_append_masked_polyline(container_x: list[float | None], container_y: list[float | None], container_z: list[float | None], points: list[tuple[float, float, float]], ring: int) -> None:
    """Append a polyline, breaking it wherever it enters the inner hole."""
    wrote_segment = False
    for point in points:
        x_value, y_value, z_value = point
        if plot_point_is_inside_previous_ring_projection(x_value, y_value, ring):
            if wrote_segment and container_x and (container_x[-1] is not None):
                container_x.append(None)
                container_y.append(None)
                container_z.append(None)
            wrote_segment = False
            continue
        container_x.append(x_value)
        container_y.append(y_value)
        container_z.append(z_value)
        wrote_segment = True
    if wrote_segment:
        container_x.append(None)
        container_y.append(None)
        container_z.append(None)

def plot_ring_surface_traces(rows: list[dict[str, Any]], z_values: list[float], visible: bool, color: str, view_mode: str, carve_hole: bool, hole_mode: str) -> list[_go.Mesh3d]:
    """Fill each ring with a Coons surface, optionally carving the inner hole."""
    points_by_ring: dict[int, list[tuple[dict[str, Any], float]]] = {}
    for row, height in zip(rows, z_values):
        points_by_ring.setdefault(row['ring'], []).append((row, height))
    surfaces: list[_go.Mesh3d] = []
    for ring in sorted(points_by_ring):
        points = sorted(points_by_ring[ring], key=lambda item: math.atan2(item[0]['b'], item[0]['a']))
        if len(points) < 3:
            continue
        interpolation_points = [(float(row['a']), float(row['b']), float(height)) for row, height in points]
        patch_grid = plot_coons_patch_grid(points, ring)
        i: list[int] = []
        j: list[int] = []
        k: list[int] = []
        if patch_grid is not None:
            grid_size = len(patch_grid)
            x = [point[0] for grid_row in patch_grid for point in grid_row]
            y = [point[1] for grid_row in patch_grid for point in grid_row]
            z = [point[2] for grid_row in patch_grid for point in grid_row]
            for v_index in range(grid_size - 1):
                for u_index in range(grid_size - 1):
                    lower_left = v_index * grid_size + u_index
                    lower_right = lower_left + 1
                    upper_left = lower_left + grid_size
                    upper_right = upper_left + 1
                    cell_vertices = [patch_grid[v_index][u_index], patch_grid[v_index][u_index + 1], patch_grid[v_index + 1][u_index], patch_grid[v_index + 1][u_index + 1]]
                    if carve_hole and plot_cell_is_inside_previous_ring_projection(cell_vertices, ring):
                        continue
                    i.extend((lower_left, lower_left))
                    j.extend((lower_right, upper_right))
                    k.extend((upper_right, upper_left))
        else:
            boundary = interpolation_points
            boundary_count = len(boundary)
            radial_steps = 10
            center = tuple((sum((point[axis] for point in interpolation_points)) / len(interpolation_points) for axis in range(3)))
            x = [center[0]]
            y = [center[1]]
            z = [center[2]]
            for radial_index in range(1, radial_steps + 1):
                factor = radial_index / radial_steps
                for boundary_point in boundary:
                    x.append(center[0] + factor * (boundary_point[0] - center[0]))
                    y.append(center[1] + factor * (boundary_point[1] - center[1]))
                    z.append(center[2] + factor * (boundary_point[2] - center[2]))
            for boundary_index in range(boundary_count):
                next_boundary = (boundary_index + 1) % boundary_count
                i.append(0)
                j.append(1 + boundary_index)
                k.append(1 + next_boundary)
            for radial_index in range(1, radial_steps):
                inner_start = 1 + (radial_index - 1) * boundary_count
                outer_start = 1 + radial_index * boundary_count
                for boundary_index in range(boundary_count):
                    next_boundary = (boundary_index + 1) % boundary_count
                    inner = inner_start + boundary_index
                    inner_next = inner_start + next_boundary
                    outer = outer_start + boundary_index
                    outer_next = outer_start + next_boundary
                    i.extend((inner, inner))
                    j.extend((outer, outer_next))
                    k.extend((outer_next, inner_next))
        surfaces.append(_go.Mesh3d(x=x, y=y, z=z, i=i, j=j, k=k, color=color, opacity=0.2, flatshading=False, hovertemplate=f'Ring {ring} surface<extra></extra>', name=f'Ring {ring} surface', meta={'kind': 'ring_surface', 'mode': view_mode, 'ring': ring, 'hole_mode': hole_mode}, showlegend=False, visible=visible))
    return surfaces

def plot_ring_grid_traces(rows: list[dict[str, Any]], z_values: list[float], visible: bool, color: str, view_mode: str, carve_hole: bool, hole_mode: str) -> list[_go.Scatter3d]:
    """Draw the isoparametric grid of every ring, with optional inner hole."""
    points_by_ring: dict[int, list[tuple[dict[str, Any], float]]] = {}
    for row, height in zip(rows, z_values):
        points_by_ring.setdefault(row['ring'], []).append((row, height))
    traces: list[_go.Scatter3d] = []
    for ring in sorted(points_by_ring):
        points = points_by_ring[ring]
        patch_grid = plot_coons_patch_grid(points, ring)
        x: list[float | None] = []
        y: list[float | None] = []
        z: list[float | None] = []
        if patch_grid is not None:
            grid_size = len(patch_grid)
            for grid_row in patch_grid:
                if carve_hole:
                    plot_append_masked_polyline(x, y, z, list(grid_row), ring)
                else:
                    x.extend([point[0] for point in grid_row] + [None])
                    y.extend([point[1] for point in grid_row] + [None])
                    z.extend([point[2] for point in grid_row] + [None])
            for u_index in range(grid_size):
                column = [grid_row[u_index] for grid_row in patch_grid]
                if carve_hole:
                    plot_append_masked_polyline(x, y, z, column, ring)
                else:
                    x.extend([point[0] for point in column] + [None])
                    y.extend([point[1] for point in column] + [None])
                    z.extend([point[2] for point in column] + [None])
        else:
            ordered = sorted(points, key=lambda item: math.atan2(item[0]['b'], item[0]['a']))
            interpolation_points = [(float(row['a']), float(row['b']), float(height)) for row, height in ordered]
            boundary = interpolation_points
            center = tuple((sum((point[axis] for point in interpolation_points)) / len(interpolation_points) for axis in range(3)))
            for radial_index in range(1, 9):
                factor = radial_index / 8.0
                curve = [tuple((center[axis] + factor * (point[axis] - center[axis]) for axis in range(3))) for point in boundary]
                closed_curve = [*curve, curve[0]]
                x.extend([point[0] for point in closed_curve] + [None])
                y.extend([point[1] for point in closed_curve] + [None])
                z.extend([point[2] for point in closed_curve] + [None])
            spoke_count = min(16, len(boundary))
            for spoke_index in range(spoke_count):
                boundary_index = spoke_index * len(boundary) // spoke_count
                boundary_point = boundary[boundary_index]
                spoke = [tuple((center[axis] + step / 8.0 * (boundary_point[axis] - center[axis]) for axis in range(3))) for step in range(9)]
                x.extend([point[0] for point in spoke] + [None])
                y.extend([point[1] for point in spoke] + [None])
                z.extend([point[2] for point in spoke] + [None])
        traces.append(_go.Scatter3d(x=x, y=y, z=z, mode='lines', line={'width': 2, 'color': color}, opacity=0.55, hoverinfo='skip', name=f'Ring {ring} grid', meta={'kind': 'ring_grid', 'mode': view_mode, 'ring': ring, 'hole_mode': hole_mode}, showlegend=False, visible=visible))
    return traces

def plot_inner_hole_border_traces(rows: list[dict[str, Any]], z_values: list[float], visible: bool, view_mode: str) -> list[_go.Scatter3d]:
    """Draw the real inner-hole edge directly on the Coons surface.

    The previous implementation drew the border on the ring floor, so the
    mesh could cover it.  Here the border is extracted from the exact boundary
    between removed and retained mesh cells and therefore follows the relief.
    """
    points_by_ring: dict[int, list[tuple[dict[str, Any], float]]] = {}
    for row, height in zip(rows, z_values):
        points_by_ring.setdefault(int(row['ring']), []).append((row, height))
    traces: list[_go.Scatter3d] = []
    for ring in sorted(points_by_ring):
        if ring <= 1:
            continue
        points = points_by_ring[ring]
        patch_grid = plot_coons_patch_grid(points, ring)
        x: list[float | None] = []
        y: list[float | None] = []
        z: list[float | None] = []
        if patch_grid is not None:
            grid_size = len(patch_grid)
            removed: list[list[bool]] = []
            for v_index in range(grid_size - 1):
                row_mask: list[bool] = []
                for u_index in range(grid_size - 1):
                    vertices = [patch_grid[v_index][u_index], patch_grid[v_index][u_index + 1], patch_grid[v_index + 1][u_index], patch_grid[v_index + 1][u_index + 1]]
                    row_mask.append(plot_cell_is_inside_previous_ring_projection(vertices, ring))
                removed.append(row_mask)

            def append_edge(first: tuple[float, float, float], second: tuple[float, float, float]) -> None:
                local_lift = 0.001
                x.extend((first[0], second[0], None))
                y.extend((first[1], second[1], None))
                z.extend((first[2] + local_lift, second[2] + local_lift, None))
            for v_index in range(grid_size - 1):
                for u_index in range(grid_size - 1):
                    if not removed[v_index][u_index]:
                        continue
                    top_is_hole = v_index > 0 and removed[v_index - 1][u_index]
                    bottom_is_hole = v_index < grid_size - 2 and removed[v_index + 1][u_index]
                    left_is_hole = u_index > 0 and removed[v_index][u_index - 1]
                    right_is_hole = u_index < grid_size - 2 and removed[v_index][u_index + 1]
                    if not top_is_hole:
                        append_edge(patch_grid[v_index][u_index], patch_grid[v_index][u_index + 1])
                    if not bottom_is_hole:
                        append_edge(patch_grid[v_index + 1][u_index], patch_grid[v_index + 1][u_index + 1])
                    if not left_is_hole:
                        append_edge(patch_grid[v_index][u_index], patch_grid[v_index + 1][u_index])
                    if not right_is_hole:
                        append_edge(patch_grid[v_index][u_index + 1], patch_grid[v_index + 1][u_index + 1])
        else:
            inner_ring = ring - 1
            fallback_height = min((float(height) for _, height in points)) + 0.001
            x = [-inner_ring, -inner_ring, inner_ring, inner_ring, -inner_ring]
            y = [-inner_ring, inner_ring, inner_ring, -inner_ring, -inner_ring]
            z = [fallback_height] * 5
        if not x:
            continue
        traces.append(_go.Scatter3d(x=x, y=y, z=z, mode='lines', line={'width': 4, 'color': 'rgba(55, 70, 90, 0.7)'}, opacity=1.0, hovertemplate=f'Ring {ring} inner boundary<extra></extra>', name=f'Ring {ring} inner boundary', meta={'kind': 'hole_border', 'mode': view_mode, 'ring': ring, 'hole_mode': 'hollow'}, showlegend=False, visible=visible))
    return traces

def plot_ring_traces(max_ring: int, plane_offsets: dict[int, float]) -> list[_go.Scatter3d]:
    traces: list[_go.Scatter3d] = []
    for ring in range(1, max_ring + 1):
        x = [-ring, -ring, ring, ring, -ring]
        y = [-ring, ring, ring, -ring, -ring]
        floor = plane_offsets.get(ring, 0.0)
        z = [floor, floor, floor, floor, floor]
        traces.append(_go.Scatter3d(x=x, y=y, z=z, mode='lines', line={'width': 3}, opacity=0.35, hovertemplate=f'Ring {ring}<extra></extra>', name=f'Ring {ring}', meta={'kind': 'ring_floor', 'ring': ring}, showlegend=False, visible=True))
    return traces

def plot_ring_statistics(rows: list[dict[str, Any]], values: list[float]) -> tuple[dict[int, float], dict[int, float], dict[int, float]]:
    """Return the minimum, maximum, and span of each ring."""
    minima: dict[int, float] = {}
    maxima: dict[int, float] = {}
    for row, value in zip(rows, values):
        ring = int(row['ring'])
        value = float(value)
        minima[ring] = min(minima.get(ring, value), value)
        maxima[ring] = max(maxima.get(ring, value), value)
    spans = {ring: maxima[ring] - minima[ring] for ring in minima}
    return minima, maxima, spans

def plot_compute_mode_floors(rings: list[int], minima: dict[int, float], maxima: dict[int, float], spans: dict[int, float], plane_gap: str | float, plane_clearance: float, reverse: bool) -> dict[int, float]:
    """Compute rigid vertical translations for one display mode."""
    order = list(reversed(rings)) if reverse else rings
    floors: dict[int, float] = {}
    previous_top: float | None = None
    for index, ring in enumerate(order):
        if index == 0:
            floor = 0.0
        else:
            previous_ring = order[index - 1]
            gap = plane_clearance * max(spans[previous_ring], 1.0) if plane_gap == 'auto' else float(plane_gap)
            floor = max(0.0, (previous_top or 0.0) + gap - minima[ring])
        floors[ring] = floor
        shifted_top = maxima[ring] + floor
        previous_top = shifted_top if previous_top is None else max(previous_top, shifted_top)
    return floors

def plot_build_mode_traces(rows: list[dict[str, Any]], mode: str, raw_values: list[float], displayed_values: list[float], floors: dict[int, float], visible: bool, label: str, colorscale: str, colorbar_title: str, surface_color: str, grid_color: str) -> list[_go.BaseTraceType]:
    """Build every Plotly trace associated with one selectable measure."""
    ring_values = [int(row['ring']) for row in rows]
    marker_trace = _go.Scatter3d(x=[row['a'] for row in rows], y=[row['b'] for row in rows], z=displayed_values, mode='markers', marker={'size': [5 + 1.2 * ring for ring in ring_values], 'color': raw_values, 'colorscale': colorscale, 'showscale': True, 'colorbar': {'title': colorbar_title}}, text=[plot_make_hover_text(row, mode) for row in rows], hovertemplate='%{text}<extra></extra>', name=label, meta={'kind': 'markers', 'mode': mode}, showlegend=False, visible=visible)
    line_trace = plot_vertical_lines(rows, displayed_values, floors, visible)
    line_trace.meta = {'kind': 'vertical_lines', 'mode': mode}
    connections = plot_ring_connection_traces(rows, displayed_values, visible)
    for trace in connections:
        trace.meta = {**(trace.meta or {}), 'mode': mode}
    hollow_surfaces = plot_ring_surface_traces(rows, displayed_values, visible, surface_color, mode, True, 'hollow')
    solid_surfaces = plot_ring_surface_traces(rows, displayed_values, False, surface_color, mode, False, 'solid')
    hollow_grids = plot_ring_grid_traces(rows, displayed_values, visible, grid_color, mode, True, 'hollow')
    solid_grids = plot_ring_grid_traces(rows, displayed_values, False, grid_color, mode, False, 'solid')
    hole_borders = plot_inner_hole_border_traces(rows, displayed_values, visible, mode)
    return [marker_trace, line_trace, *connections, *hollow_surfaces, *solid_surfaces, *hollow_grids, *solid_grids, *hole_borders]

def plot_build_figure(rows: list[dict[str, Any]], title: str, plane_gap: str | float, plane_clearance: float) -> _go.Figure:
    """Construct the complete interactive Plotly figure.

    Frequency data is sufficient to build the pyramid.  The chronological
    ``First appearances`` view is enabled only when every measured pair has a
    valid historical ``n_first`` value.
    """
    if not rows:
        raise ValueError('No pair data found')
    ring_values = [int(row['ring']) for row in rows]
    rings = sorted(set(ring_values))
    birth_available = all(int(row.get('n_first', 0)) > 0 for row in rows)
    raw_birth = [math.log10(max(int(row.get('n_first', 0)), 1)) for row in rows]
    raw_count = [math.log10(max(int(row['count']), 1)) for row in rows]
    birth_minima, birth_maxima, birth_spans = plot_ring_statistics(rows, raw_birth)
    count_minima, count_maxima, count_spans = plot_ring_statistics(rows, raw_count)
    birth_floors = plot_compute_mode_floors(rings, birth_minima, birth_maxima, birth_spans, plane_gap, plane_clearance, reverse=False)
    count_floors = plot_compute_mode_floors(rings, count_minima, count_maxima, count_spans, plane_gap, plane_clearance, reverse=True)
    displayed_birth = [birth_floors[int(row['ring'])] + value for row, value in zip(rows, raw_birth)]
    displayed_count = [count_floors[int(row['ring'])] + value for row, value in zip(rows, raw_count)]
    initial_mode = 'birth' if birth_available else 'count'
    initial_floors = birth_floors if birth_available else count_floors
    traces = [
        *plot_build_mode_traces(
            rows, 'birth', raw_birth, displayed_birth, birth_floors,
            birth_available, 'First appearances', 'Viridis', 'log10(n)',
            'royalblue', 'rgba(25, 55, 110, 0.75)',
        ),
        *plot_build_mode_traces(
            rows, 'count', raw_count, displayed_count, count_floors,
            not birth_available, 'Frequencies', 'Plasma', 'log10(count)',
            'darkorange', 'rgba(130, 65, 10, 0.75)',
        ),
        *plot_ring_traces(max(ring_values), initial_floors),
    ]
    gap_mode = 'auto' if plane_gap == 'auto' else 'fixed'
    gap_value = plane_clearance if plane_gap == 'auto' else float(plane_gap)
    figure = _go.Figure(data=traces)
    figure.update_layout(
        title=None,
        scene={
            'xaxis_title': 'A',
            'yaxis_title': 'B',
            'zaxis_title': 'Ring — visual offset; data height unchanged',
            'aspectmode': 'data',
            'camera': {'eye': {'x': 1.55, 'y': -1.65, 'z': 1.15}},
        },
        meta={
            'sidebar_title': title,
            'birth_available': birth_available,
            'initial_mode': initial_mode,
            'row_rings': ring_values,
            'row_raw_birth': raw_birth,
            'row_raw_count': raw_count,
            'ring_order': rings,
            'birth_minima': {str(ring): birth_minima[ring] for ring in rings},
            'birth_maxima': {str(ring): birth_maxima[ring] for ring in rings},
            'birth_spans': {str(ring): birth_spans[ring] for ring in rings},
            'count_minima': {str(ring): count_minima[ring] for ring in rings},
            'count_maxima': {str(ring): count_maxima[ring] for ring in rings},
            'count_spans': {str(ring): count_spans[ring] for ring in rings},
            'initial_birth_floors': {str(ring): birth_floors[ring] for ring in rings},
            'initial_count_floors': {str(ring): count_floors[ring] for ring in rings},
            'initial_gap_mode': gap_mode,
            'initial_gap_value': gap_value,
            'initial_scale_mode': 'absolute',
            'initial_grid_density': 1.0,
            'initial_mesh_opacity': 0.2,
        },
        margin={'l': 10, 'r': 10, 'b': 10, 't': 20},
        updatemenus=[],
        annotations=[],
    )
    return figure

def plot_ring_toggle_post_script() -> str:
    """Return JavaScript for ring visibility, mirror controls and sliders."""
    return "\n(function () {\n    const graph = document.getElementById('{plot_id}');\n    const layoutMeta = graph.layout.meta || {};\n    const birthAvailable = Boolean(layoutMeta.birth_available);\n    const rowRings = (layoutMeta.row_rings || []).map(Number);\n    const rowRaw = {\n        birth: (layoutMeta.row_raw_birth || []).map(Number),\n        count: (layoutMeta.row_raw_count || []).map(Number)\n    };\n    const ringOrder = (layoutMeta.ring_order || []).map(Number).sort((a, b) => a - b);\n    const birthMinima = Object.fromEntries(\n        Object.entries(layoutMeta.birth_minima || {}).map(([key, value]) => [Number(key), Number(value)])\n    );\n    const birthMaxima = Object.fromEntries(\n        Object.entries(layoutMeta.birth_maxima || {}).map(([key, value]) => [Number(key), Number(value)])\n    );\n    const birthSpans = Object.fromEntries(\n        Object.entries(layoutMeta.birth_spans || {}).map(([key, value]) => [Number(key), Number(value)])\n    );\n    const countMinima = Object.fromEntries(\n        Object.entries(layoutMeta.count_minima || {}).map(([key, value]) => [Number(key), Number(value)])\n    );\n    const countMaxima = Object.fromEntries(\n        Object.entries(layoutMeta.count_maxima || {}).map(([key, value]) => [Number(key), Number(value)])\n    );\n    const countSpans = Object.fromEntries(\n        Object.entries(layoutMeta.count_spans || {}).map(([key, value]) => [Number(key), Number(value)])\n    );\n    const initialBirthFloors = Object.fromEntries(\n        Object.entries(layoutMeta.initial_birth_floors || {}).map(([key, value]) => [Number(key), Number(value)])\n    );\n    const initialCountFloors = Object.fromEntries(\n        Object.entries(layoutMeta.initial_count_floors || {}).map(([key, value]) => [Number(key), Number(value)])\n    );\n\n    const components = graph.data\n        .map((trace, index) => ({ trace, index }))\n        .filter(({ trace }) => trace.meta && trace.meta.kind);\n\n    if (!components.length) {\n        return;\n    }\n\n    const rings = [...new Set(\n        components\n            .map(({ trace }) => trace.meta && trace.meta.ring)\n            .filter(value => value !== undefined)\n            .map(Number)\n    )].sort((first, second) => first - second);\n\n    const modeMinima = { birth: birthMinima, count: countMinima };\n    const modeMaxima = { birth: birthMaxima, count: countMaxima };\n    const modeSpans = { birth: birthSpans, count: countSpans };\n    const enabled = new Map(rings.map(ring => [ring, true]));\n    let currentMode = layoutMeta.initial_mode || (birthAvailable ? 'birth' : 'count');\n    let fullMirror = true;\n    let gapMode = layoutMeta.initial_gap_mode || 'auto';\n    let gapValue = Number(layoutMeta.initial_gap_value ?? 0.1);\n    let scaleMode = layoutMeta.initial_scale_mode || 'absolute';\n    let gridDensity = Number(layoutMeta.initial_grid_density ?? 1.0);\n    let meshOpacity = Number(layoutMeta.initial_mesh_opacity ?? 0.20);\n    let holeMode = 'hollow';\n\n    const originalGeometry = graph.data.map(trace => ({\n        x: trace.x ? Array.from(trace.x) : null,\n        y: trace.y ? Array.from(trace.y) : null,\n        i: trace.i ? Array.from(trace.i) : null,\n        j: trace.j ? Array.from(trace.j) : null,\n        k: trace.k ? Array.from(trace.k) : null\n    }));\n\n    const baseRawZByTrace = graph.data.map(trace => {\n        if (!trace.z || !trace.meta || trace.meta.ring === undefined) {\n            return null;\n        }\n\n        const ring = Number(trace.meta.ring);\n        const floorMap = trace.meta.mode === 'count' ? initialCountFloors : initialBirthFloors;\n        const floor = Number(floorMap[ring] ?? 0);\n\n        if (trace.meta.kind === 'ring_floor') {\n            return Array.from(trace.z).map(() => 0);\n        }\n\n        return Array.from(trace.z).map(value =>\n            value == null ? null : Number(value) - floor\n        );\n    });\n\n    function styleButton(button, isEnabled) {\n        button.style.background = isEnabled ? '#e8f0fb' : '#f2f2f2';\n        button.style.color = isEnabled ? '#24476f' : '#888';\n        button.style.borderColor = isEnabled ? '#8caad0' : '#c8c8c8';\n        button.style.textDecoration = isEnabled ? 'none' : 'line-through';\n        button.setAttribute('aria-pressed', String(isEnabled));\n    }\n\n    function pointHeight(mode, ring, rawValue) {\n        // The numerical height is determined only by the selected scale.\n        // Plane spacing is a separate visual translation and must never\n        // rescale or modify this value.\n        const raw = Number(rawValue || 0);\n        if (scaleMode === 'relative') {\n            return raw - Number((modeMinima[mode] || {})[ring] || 0);\n        }\n        return raw;\n    }\n\n    function computeFloors() {\n        const floors = {};\n        let previousTop = null;\n        const minimaByRing = modeMinima[currentMode] || {};\n        const maximaByRing = modeMaxima[currentMode] || {};\n        const spansByRing = modeSpans[currentMode] || {};\n        const stackOrder = currentMode === 'count'\n            ? Array.from(ringOrder).reverse()\n            : Array.from(ringOrder);\n\n        stackOrder.forEach((ring, index) => {\n            let rawMinimum;\n            let rawMaximum;\n\n            // These extrema describe only the data relief.  The gap is\n            // applied later as a rigid translation of the complete ring.\n            rawMinimum = pointHeight(\n                currentMode,\n                ring,\n                Number(minimaByRing[ring] || 0),\n            );\n            rawMaximum = pointHeight(\n                currentMode,\n                ring,\n                Number(maximaByRing[ring] || 0),\n            );\n\n            if (index === 0) {\n                floors[ring] = 0;\n            } else {\n                const previousRing = stackOrder[index - 1];\n                const previousSpan = Number(spansByRing[previousRing] || 0);\n                const gap = gapMode === 'auto'\n                    ? gapValue * Math.max(previousSpan, 1.0)\n                    : gapValue;\n\n                floors[ring] = Math.max(\n                    0,\n                    Number(previousTop || 0) + gap - rawMinimum,\n                );\n            }\n\n            const shiftedTop = rawMaximum + floors[ring];\n            previousTop = previousTop == null ? shiftedTop : Math.max(previousTop, shiftedTop);\n        });\n\n        return floors;\n    }\n\n    function makeDisplayedZ(trace, floors) {\n        if (!trace.meta || !trace.meta.kind) {\n            return null;\n        }\n\n        const kind = trace.meta.kind;\n        const ring = trace.meta.ring !== undefined ? Number(trace.meta.ring) : null;\n\n        if (kind === 'markers' || kind === 'vertical_lines') {\n            const mode = trace.meta.mode;\n            const rawValues = rowRaw[mode] || [];\n\n            if (kind === 'markers') {\n                return rowRings.map((rowRing, pointIndex) =>\n                    Number(floors[rowRing] || 0)\n                    + pointHeight(mode, rowRing, rawValues[pointIndex])\n                );\n            }\n\n            const newZ = [];\n            rowRings.forEach((rowRing, pointIndex) => {\n                const floor = Number(floors[rowRing] || 0);\n                newZ.push(\n                    floor,\n                    floor + pointHeight(mode, rowRing, rawValues[pointIndex]),\n                    null,\n                );\n            });\n            return newZ;\n        }\n\n        if (kind === 'ring_floor' && ring !== null) {\n            return Array.from(baseRawZByTrace[graph.data.indexOf(trace)] || []).map(() => Number(floors[ring] || 0));\n        }\n\n        if (ring === null || !trace.meta.mode) {\n            return null;\n        }\n\n        const mode = trace.meta.mode;\n        const baseRawZ = baseRawZByTrace[graph.data.indexOf(trace)];\n        if (!baseRawZ) {\n            return null;\n        }\n\n        return baseRawZ.map(value =>\n            value == null\n                ? null\n                : Number(floors[ring] || 0) + pointHeight(mode, ring, value)\n        );\n    }\n\n    function splitGroups(x, y, z) {\n        const groups = [];\n        let currentX = [];\n        let currentY = [];\n        let currentZ = [];\n\n        for (let index = 0; index < x.length; index += 1) {\n            if (x[index] == null || y[index] == null || z[index] == null) {\n                if (currentX.length) {\n                    groups.push({ x: currentX, y: currentY, z: currentZ });\n                    currentX = [];\n                    currentY = [];\n                    currentZ = [];\n                }\n                continue;\n            }\n            currentX.push(x[index]);\n            currentY.push(y[index]);\n            currentZ.push(z[index]);\n        }\n\n        if (currentX.length) {\n            groups.push({ x: currentX, y: currentY, z: currentZ });\n        }\n        return groups;\n    }\n\n    function flattenGroups(groups, measuredHalfOnly) {\n        const x = [];\n        const y = [];\n        const z = [];\n        groups.forEach(group => {\n            if (measuredHalfOnly && !group.y.every(value => Number(value) <= 0)) {\n                return;\n            }\n            x.push(...group.x, null);\n            y.push(...group.y, null);\n            z.push(...group.z, null);\n        });\n        return { x, y, z };\n    }\n\n    function densityStride() {\n        return Math.max(1, Math.round(1 / Math.max(0.05, gridDensity)));\n    }\n\n    function applyPlaneVisibility() {\n        const indexes = [];\n        const visibility = [];\n\n        components.forEach(({ trace, index }) => {\n            if (!trace.meta) {\n                return;\n            }\n\n            indexes.push(index);\n            const modeMatches = !trace.meta.mode || trace.meta.mode === currentMode;\n            const holeMatches = !trace.meta.hole_mode || trace.meta.hole_mode === holeMode;\n            const ringMatches = trace.meta.ring === undefined\n                || enabled.get(Number(trace.meta.ring)) !== false;\n\n            visibility.push(modeMatches && holeMatches && ringMatches);\n        });\n\n        if (indexes.length) {\n            Plotly.restyle(graph, { visible: visibility }, indexes);\n        }\n    }\n\n    function updateSurfaceOpacity() {\n        const indexes = [];\n        const opacities = [];\n\n        graph.data.forEach((trace, index) => {\n            if (trace.meta && trace.meta.kind === 'ring_surface') {\n                indexes.push(index);\n                opacities.push(meshOpacity);\n            }\n        });\n\n        if (indexes.length) {\n            Plotly.restyle(graph, { opacity: opacities }, indexes);\n        }\n    }\n\n    function updateAllGeometry() {\n        const floors = computeFloors();\n        const updates = [];\n\n        graph.data.forEach((trace, index) => {\n            if (!trace.meta || !trace.meta.kind) {\n                return;\n            }\n\n            const kind = trace.meta.kind;\n            const displayedZ = makeDisplayedZ(trace, floors);\n            const original = originalGeometry[index];\n            const ring = trace.meta.ring !== undefined ? Number(trace.meta.ring) : null;\n\n            // Markers and vertical lines are aggregate traces containing\n            // points from every ring.  They therefore cannot be hidden by the\n            // trace-level ring metadata used for surfaces and grids.  Mask\n            // each point/segment according to its own row ring.\n            if (kind === 'markers') {\n                const maskedX = [];\n                const maskedY = [];\n                const maskedZ = [];\n\n                rowRings.forEach((rowRing, pointIndex) => {\n                    const xValue = original.x ? original.x[pointIndex] : null;\n                    const yValue = original.y ? original.y[pointIndex] : null;\n                    const zValue = displayedZ ? displayedZ[pointIndex] : null;\n                    const ringEnabled = enabled.get(Number(rowRing)) !== false;\n                    const mirrorEnabled = fullMirror\n                        || yValue == null\n                        || Number(yValue) <= 0;\n                    const showPoint = ringEnabled && mirrorEnabled;\n\n                    maskedX.push(showPoint ? xValue : null);\n                    maskedY.push(showPoint ? yValue : null);\n                    maskedZ.push(showPoint ? zValue : null);\n                });\n\n                Plotly.restyle(graph, {\n                    x: [maskedX],\n                    y: [maskedY],\n                    z: [maskedZ],\n                }, [index]);\n                return;\n            }\n\n            if (kind === 'vertical_lines') {\n                const maskedX = [];\n                const maskedY = [];\n                const maskedZ = [];\n\n                rowRings.forEach((rowRing, pointIndex) => {\n                    const baseIndex = pointIndex * 3;\n                    const yValue = original.y ? original.y[baseIndex] : null;\n                    const ringEnabled = enabled.get(Number(rowRing)) !== false;\n                    const mirrorEnabled = fullMirror\n                        || yValue == null\n                        || Number(yValue) <= 0;\n                    const showSegment = ringEnabled && mirrorEnabled;\n\n                    for (let offset = 0; offset < 3; offset += 1) {\n                        const sourceIndex = baseIndex + offset;\n                        maskedX.push(\n                            showSegment && original.x\n                                ? original.x[sourceIndex]\n                                : null\n                        );\n                        maskedY.push(\n                            showSegment && original.y\n                                ? original.y[sourceIndex]\n                                : null\n                        );\n                        maskedZ.push(\n                            showSegment && displayedZ\n                                ? displayedZ[sourceIndex]\n                                : null\n                        );\n                    }\n                });\n\n                Plotly.restyle(graph, {\n                    x: [maskedX],\n                    y: [maskedY],\n                    z: [maskedZ],\n                }, [index]);\n                return;\n            }\n\n            if (kind === 'ring_grid') {\n                const groups = splitGroups(original.x || [], original.y || [], displayedZ || []);\n                const stride = densityStride();\n                const keptGroups = groups.filter((_, groupIndex) => groupIndex % stride === 0);\n                const flattened = flattenGroups(keptGroups, !fullMirror);\n                Plotly.restyle(graph, {\n                    x: [flattened.x],\n                    y: [flattened.y],\n                    z: [flattened.z],\n                }, [index]);\n                return;\n            }\n\n            if (kind === 'ring_surface' && original.i && original.j && original.k && original.x && original.y) {\n                if (fullMirror) {\n                    Plotly.restyle(graph, {\n                        x: [original.x],\n                        y: [original.y],\n                        z: [displayedZ],\n                        i: [original.i],\n                        j: [original.j],\n                        k: [original.k],\n                    }, [index]);\n                } else {\n                    const halfI = [];\n                    const halfJ = [];\n                    const halfK = [];\n                    for (let face = 0; face < original.i.length; face += 1) {\n                        const vertices = [original.i[face], original.j[face], original.k[face]];\n                        const isMeasuredHalf = vertices.every(vertex => Number(original.y[vertex]) <= 0);\n                        if (isMeasuredHalf) {\n                            halfI.push(original.i[face]);\n                            halfJ.push(original.j[face]);\n                            halfK.push(original.k[face]);\n                        }\n                    }\n                    Plotly.restyle(graph, {\n                        x: [original.x],\n                        y: [original.y],\n                        z: [displayedZ],\n                        i: [halfI],\n                        j: [halfJ],\n                        k: [halfK],\n                    }, [index]);\n                }\n                return;\n            }\n\n            if (original.x && original.y) {\n                if (fullMirror) {\n                    Plotly.restyle(graph, {\n                        x: [original.x],\n                        y: [original.y],\n                        z: [displayedZ],\n                    }, [index]);\n                } else {\n                    const halfX = original.x.map((value, point) =>\n                        original.y[point] == null || Number(original.y[point]) <= 0 ? value : null\n                    );\n                    const halfY = original.y.map(value =>\n                        value == null || Number(value) <= 0 ? value : null\n                    );\n                    const halfZ = (displayedZ || []).map((value, point) =>\n                        original.y[point] == null || Number(original.y[point]) <= 0 ? value : null\n                    );\n                    Plotly.restyle(graph, {\n                        x: [halfX],\n                        y: [halfY],\n                        z: [halfZ],\n                    }, [index]);\n                }\n            }\n        });\n\n        updateSurfaceOpacity();\n        applyPlaneVisibility();\n\n        // Put each ring label on the Z axis at the height of the highest\n        // displayed pair belonging to that ring.\n        const rawValuesForMode = rowRaw[currentMode] || [];\n        const highestByRing = {};\n\n        rowRings.forEach((ring, pointIndex) => {\n            const displayedHeight = Number(floors[ring] || 0)\n                + pointHeight(currentMode, ring, rawValuesForMode[pointIndex]);\n            highestByRing[ring] = ring in highestByRing\n                ? Math.max(highestByRing[ring], displayedHeight)\n                : displayedHeight;\n        });\n\n        const ringTicks = ringOrder\n            .filter(ring => enabled.get(ring) !== false && ring in highestByRing)\n            .map(ring => ({ ring, value: Number(highestByRing[ring]) }))\n            .sort((first, second) => first.value - second.value);\n\n        Plotly.relayout(graph, {\n            // ``data`` prevents the changing plane offset from altering the\n            // visual scale of the point heights, unlike ``cube``.\n            'scene.aspectmode': 'data',\n            'scene.zaxis.title.text': 'Ring',\n            'scene.zaxis.tickmode': 'array',\n            'scene.zaxis.tickvals': ringTicks.map(item => item.value),\n            'scene.zaxis.ticktext': ringTicks.map(item => 'Ring ' + item.ring),\n            'scene.zaxis.showexponent': 'none',\n            'scene.zaxis.exponentformat': 'none',\n        });\n    }\n\n    document.documentElement.style.height = '100%';\n    document.body.style.height = '100%';\n    document.body.style.margin = '0';\n    document.body.style.overflow = 'hidden';\n\n    const sidebarWidth = 320;\n    const bottomPanelHeight = 54;\n    // Chart 16 receives the common Previous / Index / Next banner after the\n    // Plotly HTML is written.  Reserve its real height so the fixed 3D canvas\n    // and the sidebar start below it instead of covering the sidebar title.\n    const navigationBanner = document.querySelector('.brpt-nav');\n    const navigationHeight = navigationBanner\n        ? Math.ceil(navigationBanner.getBoundingClientRect().height)\n        : 0;\n\n    graph.style.position = 'fixed';\n    graph.style.left = sidebarWidth + 'px';\n    graph.style.top = navigationHeight + 'px';\n    graph.style.width = 'calc(100vw - ' + sidebarWidth + 'px)';\n    graph.style.height = 'calc(100vh - ' + navigationHeight + 'px - ' + bottomPanelHeight + 'px)';\n\n    const controls = document.createElement('aside');\n    controls.id = graph.id + '-ring-controls';\n    controls.style.cssText = [\n        'position:fixed','left:0','top:' + navigationHeight + 'px','bottom:0','width:' + sidebarWidth + 'px',\n        'box-sizing:border-box','display:flex','flex-direction:column','align-items:stretch',\n        'gap:12px','overflow-y:auto','padding:18px 16px 22px 16px',\n        'background:rgba(255,255,255,0.98)','border-right:1px solid #b8c7dc',\n        'box-shadow:3px 0 12px rgba(20,45,80,0.16)',\n        'z-index:1000','font-family:Arial,sans-serif','font-size:13px'\n    ].join(';');\n\n    const panelTitle = document.createElement('div');\n    panelTitle.textContent = String(layoutMeta.sidebar_title || 'BRPT — Interactive 3D Pyramid');\n    panelTitle.style.cssText = [\n        'font-size:19px','line-height:1.25','font-weight:700','color:#183f6b',\n        'padding-bottom:10px','border-bottom:1px solid #d5dfec'\n    ].join(';');\n    controls.appendChild(panelTitle);\n\n    const modeSectionTitle = document.createElement('div');\n    modeSectionTitle.textContent = 'View';\n    modeSectionTitle.style.cssText = 'font-weight:700;color:#2a4365';\n    controls.appendChild(modeSectionTitle);\n\n    const modeWrap = document.createElement('div');\n    modeWrap.style.cssText = 'display:grid;grid-template-columns:1fr;gap:7px';\n    const birthButton = document.createElement('button');\n    const countButton = document.createElement('button');\n    [birthButton, countButton].forEach(button => {\n        button.type = 'button';\n        button.style.cssText = [\n            'width:100%','padding:8px 10px','border:1px solid #8caad0',\n            'border-radius:5px','cursor:pointer','font-size:13px',\n            'background:#f6fbff','color:#24476f','text-align:left'\n        ].join(';');\n    });\n    birthButton.textContent = 'First appearances';\n    countButton.textContent = 'Frequencies';\n\n    function updateModeButtons() {\n        birthButton.style.fontWeight = currentMode === 'birth' ? '700' : '400';\n        countButton.style.fontWeight = currentMode === 'count' ? '700' : '400';\n        birthButton.style.background = currentMode === 'birth' ? '#dceafb' : '#f6fbff';\n        countButton.style.background = currentMode === 'count' ? '#dceafb' : '#f6fbff';\n    }\n\n    birthButton.addEventListener('click', () => {\n        if (!birthAvailable) {\n            return;\n        }\n        currentMode = 'birth';\n        updateModeButtons();\n        updateAllGeometry();\n    });\n    countButton.addEventListener('click', () => {\n        currentMode = 'count';\n        updateModeButtons();\n        updateAllGeometry();\n    });\n    if (!birthAvailable) {\n        birthButton.disabled = true;\n        birthButton.textContent = 'First appearances: unavailable';\n        birthButton.style.cursor = 'not-allowed';\n        birthButton.style.opacity = '0.55';\n        birthButton.title = 'This legacy checkpoint has pair frequencies but no historical pair_first data.';\n    }\n    updateModeButtons();\n    modeWrap.appendChild(birthButton);\n    modeWrap.appendChild(countButton);\n    controls.appendChild(modeWrap);\n\n    const controlsTitle = document.createElement('div');\n    controlsTitle.textContent = 'Controls';\n    controlsTitle.style.cssText = 'font-weight:700;color:#2a4365;margin-top:2px';\n    controls.appendChild(controlsTitle);\n\n    const mirrorButton = document.createElement('button');\n    mirrorButton.type = 'button';\n    mirrorButton.style.cssText = [\n        'padding:5px 10px','border:1px solid #6489b5','border-radius:5px',\n        'cursor:pointer','font-size:13px','font-weight:600',\n        'background:#dceafb','color:#183f6b'\n    ].join(';');\n    function updateMirrorButton() {\n        mirrorButton.textContent = fullMirror ? 'Mirror: full' : 'Mirror: measured half';\n        mirrorButton.setAttribute('aria-pressed', String(fullMirror));\n    }\n    updateMirrorButton();\n    mirrorButton.addEventListener('click', () => {\n        fullMirror = !fullMirror;\n        updateMirrorButton();\n        updateAllGeometry();\n    });\n    controls.appendChild(mirrorButton);\n\n    const holeWrap = document.createElement('div');\n    holeWrap.style.cssText = 'display:grid;grid-template-columns:1fr 1fr 1fr;align-items:center;gap:6px;color:#24476f';\n    const holeLabel = document.createElement('span');\n    holeLabel.textContent = 'Projection';\n    const holeOnButton = document.createElement('button');\n    const holeOffButton = document.createElement('button');\n    [holeOnButton, holeOffButton].forEach(button => {\n        button.type = 'button';\n        button.style.cssText = [\n            'padding:5px 10px','border:1px solid #8caad0','border-radius:5px',\n            'cursor:pointer','font-size:13px','background:#f6fbff','color:#24476f'\n        ].join(';');\n    });\n    holeOnButton.textContent = 'Yes';\n    holeOffButton.textContent = 'No';\n    function updateHoleButtons() {\n        holeOnButton.style.fontWeight = holeMode === 'hollow' ? '700' : '400';\n        holeOffButton.style.fontWeight = holeMode === 'solid' ? '700' : '400';\n        holeOnButton.style.background = holeMode === 'hollow' ? '#dceafb' : '#f6fbff';\n        holeOffButton.style.background = holeMode === 'solid' ? '#dceafb' : '#f6fbff';\n    }\n    holeOnButton.addEventListener('click', () => {\n        holeMode = 'hollow';\n        updateHoleButtons();\n        updateAllGeometry();\n    });\n    holeOffButton.addEventListener('click', () => {\n        holeMode = 'solid';\n        updateHoleButtons();\n        updateAllGeometry();\n    });\n    updateHoleButtons();\n    holeWrap.appendChild(holeLabel);\n    holeWrap.appendChild(holeOnButton);\n    holeWrap.appendChild(holeOffButton);\n    controls.appendChild(holeWrap);\n\n    const scaleWrap = document.createElement('div');\n    scaleWrap.style.cssText = 'display:grid;grid-template-columns:1fr 1fr 1fr;align-items:center;gap:6px;color:#24476f';\n    const scaleLabel = document.createElement('span');\n    scaleLabel.textContent = 'Scale';\n    const absoluteButton = document.createElement('button');\n    const relativeButton = document.createElement('button');\n    [absoluteButton, relativeButton].forEach(button => {\n        button.type = 'button';\n        button.style.cssText = [\n            'padding:5px 10px','border:1px solid #8caad0','border-radius:5px',\n            'cursor:pointer','font-size:13px','background:#f6fbff','color:#24476f'\n        ].join(';');\n    });\n    absoluteButton.textContent = 'Absolute';\n    relativeButton.textContent = 'Relative';\n    function updateScaleButtons() {\n        absoluteButton.style.fontWeight = scaleMode === 'absolute' ? '700' : '400';\n        relativeButton.style.fontWeight = scaleMode === 'relative' ? '700' : '400';\n        absoluteButton.style.background = scaleMode === 'absolute' ? '#dceafb' : '#f6fbff';\n        relativeButton.style.background = scaleMode === 'relative' ? '#dceafb' : '#f6fbff';\n    }\n    absoluteButton.addEventListener('click', () => {\n        scaleMode = 'absolute';\n        updateScaleButtons();\n        updateHoleButtons();\n        updateAllGeometry();\n    });\n    relativeButton.addEventListener('click', () => {\n        scaleMode = 'relative';\n        updateScaleButtons();\n        updateHoleButtons();\n        updateAllGeometry();\n    });\n    updateScaleButtons();\n    scaleWrap.appendChild(scaleLabel);\n    scaleWrap.appendChild(absoluteButton);\n    scaleWrap.appendChild(relativeButton);\n    controls.appendChild(scaleWrap);\n\n    function makeSlider(labelText, min, max, step, value, onInput) {\n        const wrap = document.createElement('label');\n        wrap.style.cssText = 'display:grid;grid-template-columns:1fr auto;gap:5px 8px;color:#24476f';\n        const label = document.createElement('span');\n        label.textContent = labelText;\n        const slider = document.createElement('input');\n        slider.type = 'range';\n        slider.min = String(min);\n        slider.max = String(max);\n        slider.step = String(step);\n        slider.value = String(value);\n        slider.style.cssText = 'grid-column:1 / -1;width:100%;box-sizing:border-box';\n        const out = document.createElement('span');\n        out.style.cssText = 'min-width:44px;text-align:right;font-variant-numeric:tabular-nums;font-weight:600';\n        out.textContent = Number(value).toFixed(2);\n        slider.addEventListener('input', () => {\n            out.textContent = Number(slider.value).toFixed(2);\n            onInput(Number(slider.value), slider, out);\n        });\n        wrap.appendChild(label);\n        wrap.appendChild(slider);\n        wrap.appendChild(out);\n        return { wrap, slider, out, label };\n    }\n\n    const gapCtrl = makeSlider(\n        gapMode === 'auto' ? 'Ring spacing' : 'Fixed visual gap',\n        0,\n        gapMode === 'auto' ? 3 : 10,\n        0.05,\n        gapValue,\n        value => {\n            gapValue = value;\n            updateAllGeometry();\n        }\n    );\n    controls.appendChild(gapCtrl.wrap);\n\n    const gapNote = document.createElement('div');\n    gapNote.textContent = 'Moves only the ring planes; scale and point relief remain unchanged.';\n    gapNote.style.cssText = [\n        'margin-top:-7px','font-size:11px','line-height:1.3',\n        'color:#5f7188','font-style:italic'\n    ].join(';');\n    controls.appendChild(gapNote);\n\n    const densityCtrl = makeSlider('Grid density', 0.10, 1.00, 0.05, gridDensity, value => {\n        gridDensity = value;\n        updateAllGeometry();\n    });\n    controls.appendChild(densityCtrl.wrap);\n\n    const opacityCtrl = makeSlider('Mesh opacity', 0.00, 1.00, 0.05, meshOpacity, value => {\n        meshOpacity = value;\n        updateSurfaceOpacity();\n    });\n    controls.appendChild(opacityCtrl.wrap);\n\n    const resetButton = document.createElement('button');\n    resetButton.type = 'button';\n    resetButton.textContent = 'Reset';\n    resetButton.style.cssText = [\n        'padding:5px 10px','border:1px solid #8caad0','border-radius:5px',\n        'cursor:pointer','font-size:13px','background:#f6fbff','color:#24476f'\n    ].join(';');\n    resetButton.addEventListener('click', () => {\n        gapValue = Number(layoutMeta.initial_gap_value ?? 0.1);\n        scaleMode = layoutMeta.initial_scale_mode || 'absolute';\n        gridDensity = Number(layoutMeta.initial_grid_density ?? 1.0);\n        meshOpacity = Number(layoutMeta.initial_mesh_opacity ?? 0.20);\n        holeMode = 'hollow';\n        gapCtrl.slider.value = String(gapValue);\n        gapCtrl.out.textContent = gapValue.toFixed(2);\n        densityCtrl.slider.value = String(gridDensity);\n        densityCtrl.out.textContent = gridDensity.toFixed(2);\n        opacityCtrl.slider.value = String(meshOpacity);\n        opacityCtrl.out.textContent = meshOpacity.toFixed(2);\n        updateScaleButtons();\n        updateAllGeometry();\n    });\n    controls.appendChild(resetButton);\n\n    const ringTitle = document.createElement('div');\n    ringTitle.textContent = 'Ring planes';\n    ringTitle.style.cssText = 'font-weight:700;color:#2a4365;margin-top:2px';\n    controls.appendChild(ringTitle);\n\n    const ringGrid = document.createElement('div');\n    ringGrid.style.cssText = 'display:grid;grid-template-columns:1fr 1fr;gap:6px';\n\n    rings.forEach(ring => {\n        const button = document.createElement('button');\n        button.type = 'button';\n        button.textContent = 'Ring ' + ring;\n        button.style.cssText = [\n            'width:100%','padding:6px 8px','border:1px solid','border-radius:5px',\n            'cursor:pointer','font-size:13px'\n        ].join(';');\n        styleButton(button, true);\n        button.addEventListener('click', () => {\n            const nextState = !enabled.get(ring);\n            enabled.set(ring, nextState);\n            styleButton(button, nextState);\n            updateAllGeometry();\n        });\n        ringGrid.appendChild(button);\n    });\n\n    controls.appendChild(ringGrid);\n    document.body.appendChild(controls);\n\n    const instructionsPanel = document.createElement('footer');\n    instructionsPanel.id = graph.id + '-instructions-panel';\n    instructionsPanel.style.cssText = [\n        'position:fixed','left:' + sidebarWidth + 'px','right:0','bottom:0',\n        'height:' + bottomPanelHeight + 'px','box-sizing:border-box',\n        'display:flex','align-items:center','justify-content:center',\n        'padding:8px 18px','background:rgba(255,255,255,0.98)',\n        'border-top:1px solid #b8c7dc','box-shadow:0 -2px 8px rgba(20,45,80,0.12)',\n        'z-index:999','font-family:Arial,sans-serif','font-size:13px',\n        'line-height:1.35','color:#24476f','text-align:center'\n    ].join(';');\n\n    const instructionsText = document.createElement('span');\n    instructionsText.innerHTML = [\n        '<strong>Instructions:</strong>',\n        'drag to rotate',\n        'use the mouse wheel to zoom',\n        'use the left panel to change the view, scale, projection, and ring planes'\n    ].join(' &nbsp;·&nbsp; ');\n    instructionsPanel.appendChild(instructionsText);\n    document.body.appendChild(instructionsPanel);\n\n    window.addEventListener('resize', () => {\n        Plotly.Plots.resize(graph);\n    });\n\n    // Apply the custom Z-axis ring labels immediately after the first Plotly\n    // render. Previously updateAllGeometry() ran only after using a control,\n    // so the initial view retained Plotly's default numeric Z ticks.\n    requestAnimationFrame(() => {\n        updateAllGeometry();\n        Plotly.Plots.resize(graph);\n    });\n})();\n"

def plot_write_plotly_pyramid_3d(go_module: Any, state: dict[str, Any], label: str, output_dir: Path) -> str:
    """Write the complete interactive ring-pyramid view as chart 16."""
    global _go
    _go = go_module
    rows = plot_build_rows(state, mirror=True)
    figure = plot_build_figure(rows, f'BRPT — Interactive 3D Pyramid — {label}', 'auto', 0.1)
    name = '16_3d_pyramid.html'
    path = output_dir / name
    figure.write_html(path, include_plotlyjs=True, full_html=True, post_script=plot_ring_toggle_post_script(), config={'responsive': True, 'displaylogo': False, 'scrollZoom': True, 'toImageButtonOptions': {'format': 'png', 'filename': 'brpt_3d_pyramid', 'scale': 2}})
    if not path.is_file() or path.stat().st_size == 0:
        raise RuntimeError(f'Chart 16 was not created correctly: {path}')
    print(f'written: {path} ({path.stat().st_size:,} bytes, self-contained)')
    return name

def plot_write_plotly(fig: Any, output_dir: Path, name: str) -> str:
    path = output_dir / name
    fig.write_html(path, include_plotlyjs='cdn', full_html=True)
    print(f'written: {path}')
    return name

def plot_add_html_navigation(output_dir: Path, names: list[str]) -> None:
    """Add one global Previous/Next chain to every interactive chart.

    All charts are stored in the same directory. Dataset charts use a second
    index button that points to ``index_ring.html`` or ``index_prime.html``.
    """
    style = '\n<style>\n.brpt-nav{position:sticky;top:0;z-index:9999;display:flex;justify-content:flex-end;\ngap:8px;padding:10px 16px;background:#fff;border-bottom:1px solid #d1d5db;\nfont:14px system-ui,sans-serif}.brpt-nav a,.brpt-nav span{padding:7px 12px;\nborder-radius:7px;text-decoration:none}.brpt-nav a{color:#fff;background:#2563eb}\n.brpt-nav a:hover{background:#1d4ed8}.brpt-nav span{color:#64748b;background:#e2e8f0}\n</style>'
    start_marker = '<!-- BRPT_NAV_START -->'
    end_marker = '<!-- BRPT_NAV_END -->'

    for index, name in enumerate(names):
        previous = (
            f'<a href="{html.escape(names[index - 1], quote=True)}"><- Previous</a>'
            if index > 0 else '<span><- Previous</span>'
        )
        following = (
            f'<a href="{html.escape(names[index + 1], quote=True)}">Next -></a>'
            if index + 1 < len(names) else '<span>Next -></span>'
        )
        if name.startswith('ring_'):
            indexes = '<a href="index_ring.html">RING index</a><a href="index.html">General index</a>'
        elif name.startswith('prime_'):
            indexes = '<a href="index_prime.html">PRIME index</a><a href="index.html">General index</a>'
        else:
            indexes = '<a href="index.html">Index</a>'
        navigation = (
            f'{start_marker}{style}\n'
            f'<nav class="brpt-nav" aria-label="Plot navigation">'
            f'{previous}{indexes}{following}</nav>\n{end_marker}\n'
        )
        path = output_dir / name
        document = path.read_text(encoding='utf-8')

        # Remove navigation inserted earlier in the same generation pass.
        while True:
            old_start = document.find(start_marker)
            if old_start < 0:
                break
            old_end = document.find(end_marker, old_start)
            if old_end < 0:
                break
            document = document[:old_start] + document[old_end + len(end_marker):]

        body_start = document.lower().find('<body')
        body_end = document.find('>', body_start)
        if body_start < 0 or body_end < 0:
            print(f'warning: body tag not found in {path}')
            continue
        document = document[:body_end + 1] + '\n' + navigation + document[body_end + 1:]
        path.write_text(document, encoding='utf-8')

def plot_plotly_heatmap(go: Any, state: dict[str, Any], label: str) -> Any:
    pairs = plot_pair_counts(state)
    a_values = list(range(min((a for a, _ in pairs), default=0), max((a for a, _ in pairs), default=0) + 1))
    b_values = list(range(min((b for _, b in pairs), default=0), max((b for _, b in pairs), default=0) + 1))
    z = [[math.log10(pairs.get((a, b), 0) + 1) for a in a_values] for b in b_values]
    raw = [[pairs.get((a, b), 0) for a in a_values] for b in b_values]
    fig = go.Figure(go.Heatmap(x=a_values, y=b_values, z=z, customdata=raw, colorscale='Viridis', colorbar_title='log10(n+1)', hovertemplate='A=%{x}, B=%{y}<br>occurrences=%{customdata:,}<extra></extra>'))
    fig.update_layout(title=f'Pair heatmap - {label}', xaxis_title='A', yaxis_title='B', template='plotly_white')
    return fig

def plot_plotly_top_pairs(go: Any, state: dict[str, Any], label: str, top_n: int) -> Any:
    ranked = sorted(plot_pair_counts(state).items(), key=lambda item: item[1], reverse=True)[:top_n]
    ranked.reverse()
    fig = go.Figure(go.Bar(x=[value for _, value in ranked], y=[f'({a},{b})' for (a, b), _ in ranked], orientation='h', text=[f'{value:,}' for _, value in ranked], textposition='outside'))
    fig.update_layout(title=f'Top {len(ranked)} pairs - {label}', xaxis_title='Occurrences (log)', xaxis_type='log', template='plotly_white', height=max(600, 30 * len(ranked)))
    return fig

def plot_plotly_3d_frequency(go: Any, state: dict[str, Any], label: str) -> Any:
    pairs = plot_pair_counts(state)
    first = plot_pair_first(state)
    rows = []
    for (a, b), frequency in pairs.items():
        record = first.get((a, b), {})
        rows.append((a, b, max(abs(a), abs(b)), frequency, plot_as_int(record.get('index')), plot_as_int(record.get('n')), plot_as_float(record.get('elapsed_ms'))))
    log_frequency = [math.log10(max(1, row[3])) for row in rows]
    customdata = [[row[2], row[3], row[4], row[5], row[6]] for row in rows]
    fig = go.Figure(go.Scatter3d(x=[row[0] for row in rows], y=[row[1] for row in rows], z=log_frequency, mode='markers+text', text=[f'({row[0]},{row[1]})' for row in rows], textposition='top center', marker=dict(size=[6 + 2 * value for value in log_frequency], color=[row[2] for row in rows], colorscale='Turbo', colorbar_title='Ring', opacity=0.82), customdata=customdata, hovertemplate='A=%{x}, B=%{y}<br>ring=%{customdata[0]}<br>frequency=%{customdata[1]:,}<br>discovery index=%{customdata[2]:,}<br>discovery n=%{customdata[3]:,}<br>time=%{customdata[4]:.6g} ms<extra></extra>'))
    fig.update_layout(title=f'3D frequency landscape - {label}', scene=dict(xaxis_title='A', yaxis_title='B', zaxis_title='log10(frequency)'), template='plotly_white')
    return fig

def plot_plotly_3d_birth(go: Any, state: dict[str, Any], label: str) -> Any:
    pairs = plot_pair_counts(state)
    first = plot_pair_first(state)
    rows = []
    for (a, b), record in first.items():
        rows.append((a, b, max(abs(a), abs(b)), plot_as_int(record.get('index')), plot_as_int(record.get('n')), pairs.get((a, b), 0), plot_as_float(record.get('elapsed_ms'))))
    rows.sort(key=lambda row: row[3])
    customdata = [[row[2], row[3], row[4], row[5], row[6]] for row in rows]
    fig = go.Figure(go.Scatter3d(x=[row[0] for row in rows], y=[row[1] for row in rows], z=[math.log10(max(1, row[4])) for row in rows], mode='lines+markers', line=dict(width=3), marker=dict(size=[6 + 2 * math.log10(row[5] + 1) for row in rows], color=[row[2] for row in rows], colorscale='Turbo', colorbar_title='Ring'), customdata=customdata, hovertemplate='A=%{x}, B=%{y}<br>ring=%{customdata[0]}<br>index=%{customdata[1]:,}<br>n=%{customdata[2]:,}<br>frequency=%{customdata[3]:,}<br>time=%{customdata[4]:.6g} ms<extra></extra>'))
    fig.update_layout(title=f'3D pair discovery timeline - {label}', scene=dict(xaxis_title='A', yaxis_title='B', zaxis_title='log10(n at discovery)'), template='plotly_white')
    return fig

def plot_plotly_suite(go: Any, make_subplots: Any, summaries: list[tuple[str, dict[str, Any]]], states: list[tuple[str, dict[str, Any]]], state_6k1: dict[str, Any], state_prime: dict[str, Any], output_dir: Path, top_n: int) -> list[str]:
    written: list[str] = []
    metrics = (('tested', 'Numbers tested'), ('rate_per_second', 'Speed (n/s)'), ('elapsed_seconds', 'Total time (s)'), ('max_elapsed_ms', 'Maximum single time (ms)'))
    fig = make_subplots(rows=2, cols=2, subplot_titles=[title for _, title in metrics])
    labels = [label for label, _ in summaries]
    for index, (key, _title) in enumerate(metrics):
        row, col = divmod(index, 2)
        values = [plot_as_float(data.get(key)) for _, data in summaries]
        fig.add_trace(go.Bar(x=labels, y=values, text=[f'{value:,.6g}' for value in values], textposition='outside', showlegend=False), row=row + 1, col=col + 1)
    fig.update_layout(title='BRPT - C21 and PSPS validation', template='plotly_white', height=760)
    written.append(plot_write_plotly(fig, output_dir, '01_validation_dashboard.html'))
    counts_by_state = [(label, plot_ring_counts(state)) for label, state in states]
    rings = plot_ordered_union(*(counts.keys() for _, counts in counts_by_state))
    fig = go.Figure()
    for label, counts in counts_by_state:
        fig.add_trace(go.Bar(name=label, x=rings, y=[counts.get(ring, 0) for ring in rings], text=[f'{counts.get(ring, 0):,}' for ring in rings], textposition='outside'))
    fig.update_layout(title='Distribuzione assoluta dei ring', xaxis_title='Ring', yaxis_title='Occorrenze (log)', yaxis_type='log', barmode='group', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '02_ring_counts_comparison.html'))
    fig = go.Figure()
    for label, counts in counts_by_state:
        total = sum(counts.values())
        shares = [plot_safe_percentage(counts.get(ring, 0), total) for ring in rings]
        fig.add_trace(go.Scatter(name=label, x=rings, y=shares, mode='lines+markers+text', text=[f'{share:.6g}%' for share in shares], textposition='top center'))
    fig.update_layout(title='Distribuzione normalizzata dei ring', xaxis_title='Ring', yaxis_title='Quota (%) - log', yaxis_type='log', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '03_ring_share_comparison.html'))
    fig = make_subplots(rows=1, cols=2, subplot_titles=('Indice della prima comparsa', 'Valore n della prima comparsa'))
    for label, state in states:
        first = plot_ring_first(state)
        state_rings = sorted(first)
        hover = [f"A={plot_as_int(first[ring].get('a'))}, B={plot_as_int(first[ring].get('b'))}" for ring in state_rings]
        fig.add_trace(go.Scatter(x=state_rings, y=[plot_as_int(first[ring].get('index')) for ring in state_rings], mode='lines+markers', name=label, customdata=hover, hovertemplate='Ring %{x}<br>indice=%{y:,}<br>%{customdata}<extra></extra>'), row=1, col=1)
        fig.add_trace(go.Scatter(x=state_rings, y=[plot_as_int(first[ring].get('n')) for ring in state_rings], mode='lines+markers', name=label, showlegend=False, customdata=hover, hovertemplate='Ring %{x}<br>n=%{y:,}<br>%{customdata}<extra></extra>'), row=1, col=2)
    fig.update_yaxes(type='log', row=1, col=1)
    fig.update_yaxes(type='log', row=1, col=2)
    fig.update_layout(title='Nascita dei ring: candidati 6k+/-1 vs primi', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '04_ring_first_seen_comparison.html'))
    fig = go.Figure()
    for label, counts in counts_by_state:
        total = sum(counts.values())
        running = 0
        cumulative = []
        for ring in rings:
            running += counts.get(ring, 0)
            cumulative.append(plot_safe_percentage(running, total))
        fig.add_trace(go.Scatter(x=rings, y=cumulative, mode='lines+markers', name=label, hovertemplate='Ring <= %{x}<br>copertura=%{y:.10g}%<extra></extra>'))
    fig.update_layout(title='Copertura cumulativa dei ring', xaxis_title='Ring massimo', yaxis_title='Copertura (%)', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '05_ring_cumulative_comparison.html'))
    for index, (label, state) in enumerate(states, start=6):
        slug = '6k1' if label == plot_STATE_LABEL_6K1 else 'prime'
        written.append(plot_write_plotly(plot_plotly_heatmap(go, state, label), output_dir, f'{index:02d}_pair_heatmap_{slug}.html'))
    written.append(plot_write_plotly(plot_plotly_top_pairs(go, state_6k1, plot_STATE_LABEL_6K1, top_n), output_dir, '08_top_pairs_6k1.html'))
    written.append(plot_write_plotly(plot_plotly_top_pairs(go, state_prime, plot_STATE_LABEL_PRIME, top_n), output_dir, '09_top_pairs_prime.html'))
    pairs_by_state = [(label, plot_pair_counts(state)) for label, state in states]
    union_totals: dict[tuple[int, int], int] = {}
    for _, pairs in pairs_by_state:
        for pair, value in pairs.items():
            union_totals[pair] = union_totals.get(pair, 0) + value
    selected = [pair for pair, _ in sorted(union_totals.items(), key=lambda item: item[1], reverse=True)[:top_n]]
    fig = go.Figure()
    for label, pairs in pairs_by_state:
        total = sum(pairs.values())
        fig.add_trace(go.Bar(name=label, x=[f'({a},{b})' for a, b in selected], y=[plot_safe_percentage(pairs.get(pair, 0), total) for pair in selected], text=[f'{plot_safe_percentage(pairs.get(pair, 0), total):.7g}%' for pair in selected], textposition='outside'))
    fig.update_layout(title=f'Confronto normalizzato delle prime {len(selected)} coppie', xaxis_title='Coppia (A,B)', yaxis_title='Quota (%) - log', yaxis_type='log', barmode='group', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '10_pair_share_comparison.html'))
    summary_6k1 = plot_state_summary(state_6k1, '6k1')
    summary_prime = plot_state_summary(state_prime, 'prime')
    fig = make_subplots(rows=1, cols=2, subplot_titles=('Esiti candidati 6k+/-1', 'Copertura primi'))
    fig.add_trace(go.Bar(x=['Accettati', 'Rifiutati', 'Errori'], y=[summary_6k1['accepted'], summary_6k1['rejected'], summary_6k1['errors']], text=[f'{summary_6k1[key]:,}' for key in ('accepted', 'rejected', 'errors')], textposition='outside', showlegend=False), row=1, col=1)
    fig.add_trace(go.Bar(x=['Con coppia', 'Senza coppia', 'Failure', 'Errori'], y=[summary_prime['with_pair'], summary_prime['without_pair'], summary_prime['failures'], summary_prime['errors']], text=[f'{summary_prime[key]:,}' for key in ('with_pair', 'without_pair', 'failures', 'errors')], textposition='outside', showlegend=False), row=1, col=2)
    fig.update_yaxes(type='log', row=1, col=1)
    fig.update_yaxes(type='log', row=1, col=2)
    fig.update_layout(title='Riepilogo degli stati ring', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '11_state_outcomes.html'))
    fig = make_subplots(rows=1, cols=2, subplot_titles=('Distribuzione di A', 'Distribuzione di B'))
    for label, state in states:
        pairs = plot_pair_counts(state)
        total = sum(pairs.values())
        a_counts: dict[int, int] = {}
        b_counts: dict[int, int] = {}
        for (a, b), value in pairs.items():
            a_counts[a] = a_counts.get(a, 0) + value
            b_counts[b] = b_counts.get(b, 0) + value
        fig.add_trace(go.Scatter(x=sorted(a_counts), y=[plot_safe_percentage(a_counts[a], total) for a in sorted(a_counts)], mode='lines+markers', name=label), row=1, col=1)
        fig.add_trace(go.Scatter(x=sorted(b_counts), y=[plot_safe_percentage(b_counts[b], total) for b in sorted(b_counts)], mode='lines+markers', name=label, showlegend=False), row=1, col=2)
    fig.update_yaxes(type='log', title_text='Quota (%) - log')
    fig.update_layout(title='Distribuzioni marginali normalizzate', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '12_coefficient_marginals.html'))
    pairs_6k1 = plot_pair_counts(state_6k1)
    pairs_prime = plot_pair_counts(state_prime)
    shared_pairs = sorted(set(pairs_6k1) & set(pairs_prime))
    total_6k1 = sum(pairs_6k1.values())
    total_prime = sum(pairs_prime.values())
    fig = go.Figure(go.Scatter(x=[plot_safe_percentage(pairs_6k1[pair], total_6k1) for pair in shared_pairs], y=[plot_safe_percentage(pairs_prime[pair], total_prime) for pair in shared_pairs], mode='markers+text', text=[f'({a},{b})' for a, b in shared_pairs], textposition='top center', customdata=[[pairs_6k1[pair], pairs_prime[pair]] for pair in shared_pairs], hovertemplate='%{text}<br>6k+/-1=%{x:.8g}% (%{customdata[0]:,})<br>primi=%{y:.8g}% (%{customdata[1]:,})<extra></extra>'))
    positive_x = [plot_safe_percentage(pairs_6k1[pair], total_6k1) for pair in shared_pairs if pairs_6k1[pair] and pairs_prime[pair]]
    positive_y = [plot_safe_percentage(pairs_prime[pair], total_prime) for pair in shared_pairs if pairs_6k1[pair] and pairs_prime[pair]]
    if positive_x and positive_y:
        lower = min(min(positive_x), min(positive_y))
        upper = max(max(positive_x), max(positive_y))
        fig.add_trace(go.Scatter(x=[lower, upper], y=[lower, upper], mode='lines', name='y=x', line=dict(dash='dash')))
    fig.update_layout(title='Stabilita delle frequenze relative delle coppie', xaxis_title='Quota nei candidati 6k+/-1 (%)', yaxis_title='Quota nei primi (%)', xaxis_type='log', yaxis_type='log', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '13_pair_share_correlation.html'))
    written.append(plot_write_plotly(plot_plotly_3d_frequency(go, state_6k1, plot_STATE_LABEL_6K1), output_dir, '14_3d_frequency_6k1.html'))
    written.append(plot_write_plotly(plot_plotly_3d_frequency(go, state_prime, plot_STATE_LABEL_PRIME), output_dir, '15_3d_frequency_prime.html'))
    if plot_has_pair_discovery(state_6k1):
        written.append(plot_write_plotly(plot_plotly_3d_birth(go, state_6k1, plot_STATE_LABEL_6K1), output_dir, '16_3d_birth_6k1.html'))
    if plot_has_pair_discovery(state_prime):
        written.append(plot_write_plotly(plot_plotly_3d_birth(go, state_prime, plot_STATE_LABEL_PRIME), output_dir, '17_3d_birth_prime.html'))
    fig = go.Figure()
    for label, pair_frequencies in ((plot_STATE_LABEL_6K1, pairs_6k1), (plot_STATE_LABEL_PRIME, pairs_prime)):
        ranked_values: list[int] = sorted((int(value) for value in pair_frequencies.values()), reverse=True)
        total: int = sum(ranked_values, start=0)
        running: int = 0
        cumulative: list[float] = []
        for value in ranked_values:
            running += value
            cumulative.append(plot_safe_percentage(running, total))
        fig.add_trace(go.Scatter(x=list(range(1, len(cumulative) + 1)), y=cumulative, mode='lines+markers', name=label, hovertemplate='prime %{x} coppie<br>copertura=%{y:.10g}%<extra></extra>'))
    fig.update_layout(title='Copertura cumulativa delle coppie', xaxis_title='Numero di coppie piu frequenti', yaxis_title='Copertura cumulativa (%)', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '18_pair_cumulative_coverage.html'))
    enrichment_rows = []
    for pair in shared_pairs:
        share_6k1 = pairs_6k1[pair] / total_6k1 if total_6k1 else 0.0
        share_prime = pairs_prime[pair] / total_prime if total_prime else 0.0
        if share_6k1 > 0.0 and share_prime > 0.0:
            enrichment_rows.append((pair, math.log2(share_prime / share_6k1), share_6k1, share_prime))
    enrichment_rows.sort(key=lambda item: abs(item[1]), reverse=True)
    enrichment_rows = enrichment_rows[:max(top_n, 20)]
    enrichment_rows.reverse()
    fig = go.Figure(go.Bar(x=[row[1] for row in enrichment_rows], y=[f'({row[0][0]},{row[0][1]})' for row in enrichment_rows], orientation='h', customdata=[[100.0 * row[2], 100.0 * row[3]] for row in enrichment_rows], hovertemplate='%{y}<br>log2(primi/6k+/-1)=%{x:.6g}<br>quota 6k+/-1=%{customdata[0]:.8g}%<br>quota primi=%{customdata[1]:.8g}%<extra></extra>'))
    fig.add_vline(x=0.0, line_dash='dash')
    fig.update_layout(title='Arricchimento relativo delle coppie', xaxis_title='log2(quota primi / quota candidati 6k+/-1)', yaxis_title='Coppia (A,B)', template='plotly_white', height=max(650, 30 * len(enrichment_rows)))
    written.append(plot_write_plotly(fig, output_dir, '19_pair_relative_enrichment.html'))
    fig = go.Figure()
    for label, counts in counts_by_state:
        total = sum(counts.values())
        survival = [plot_safe_percentage(sum((value for ring_value, value in counts.items() if ring_value >= ring)), total) for ring in rings]
        fig.add_trace(go.Scatter(x=rings, y=survival, mode='lines+markers+text', text=[f'{value:.8g}%' for value in survival], textposition='top center', name=label, hovertemplate='R >= %{x}<br>quota=%{y:.10g}%<extra></extra>'))
    fig.update_layout(title='Coda di sopravvivenza dei ring', xaxis_title='Ring minimo richiesto', yaxis_title='P(R >= r) (%) - scala log', yaxis_type='log', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '20_ring_survival_comparison.html'))
    rank_6k1 = {pair: rank for rank, (pair, _value) in enumerate(sorted(pairs_6k1.items(), key=lambda item: item[1], reverse=True), 1)}
    rank_prime = {pair: rank for rank, (pair, _value) in enumerate(sorted(pairs_prime.items(), key=lambda item: item[1], reverse=True), 1)}
    rank_rows = [(pair, rank_6k1[pair], rank_prime[pair]) for pair in shared_pairs]
    fig = go.Figure(go.Scatter(x=[row[1] for row in rank_rows], y=[row[2] for row in rank_rows], mode='markers+text', text=[f'({row[0][0]},{row[0][1]})' for row in rank_rows], textposition='top center', hovertemplate='%{text}<br>rango 6k+/-1=%{x}<br>rango primi=%{y}<extra></extra>'))
    max_rank = max([max(row[1], row[2]) for row in rank_rows], default=1)
    fig.add_trace(go.Scatter(x=[1, max_rank], y=[1, max_rank], mode='lines', name='stesso rango', line=dict(dash='dash')))
    fig.update_layout(title='Stabilita del rango delle coppie', xaxis_title='Rango nei candidati 6k+/-1', yaxis_title='Rango nei primi', xaxis_type='log', yaxis_type='log', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '21_pair_rank_stability.html'))
    first_6k1 = plot_pair_first(state_6k1)
    first_prime = plot_pair_first(state_prime)
    birth_shared = sorted(set(first_6k1) & set(first_prime))
    birth_rows = []
    for pair in birth_shared:
        index_6k1 = plot_as_int(first_6k1[pair].get('index'))
        index_prime = plot_as_int(first_prime[pair].get('index'))
        if index_6k1 > 0 and index_prime > 0:
            birth_rows.append((pair, index_6k1, index_prime, plot_as_int(first_6k1[pair].get('n')), plot_as_int(first_prime[pair].get('n'))))
    fig = go.Figure(go.Scatter(x=[row[1] for row in birth_rows], y=[row[2] for row in birth_rows], mode='markers+text', text=[f'({row[0][0]},{row[0][1]})' for row in birth_rows], textposition='top center', customdata=[[row[3], row[4]] for row in birth_rows], hovertemplate='%{text}<br>indice 6k+/-1=%{x:,}<br>indice primi=%{y:,}<br>n 6k+/-1=%{customdata[0]:,}<br>n primi=%{customdata[1]:,}<extra></extra>'))
    if birth_rows:
        lower = min((min(row[1], row[2]) for row in birth_rows))
        upper = max((max(row[1], row[2]) for row in birth_rows))
        fig.add_trace(go.Scatter(x=[lower, upper], y=[lower, upper], mode='lines', name='y=x', line=dict(dash='dash')))
    fig.update_layout(title='Confronto degli indici di nascita delle coppie', xaxis_title='Indice prima comparsa nei candidati 6k+/-1', yaxis_title='Indice prima comparsa nei primi', xaxis_type='log', yaxis_type='log', template='plotly_white')
    if birth_rows:
        written.append(plot_write_plotly(fig, output_dir, '22_pair_birth_index_comparison.html'))
    elapsed_rows = []
    for pair in birth_shared:
        elapsed_6k1 = plot_as_float(first_6k1[pair].get('elapsed_ms'))
        elapsed_prime = plot_as_float(first_prime[pair].get('elapsed_ms'))
        if elapsed_6k1 > 0.0 and elapsed_prime > 0.0:
            elapsed_rows.append((pair, elapsed_6k1, elapsed_prime))
    fig = go.Figure(go.Scatter(x=[row[1] for row in elapsed_rows], y=[row[2] for row in elapsed_rows], mode='markers+text', text=[f'({row[0][0]},{row[0][1]})' for row in elapsed_rows], textposition='top center', hovertemplate='%{text}<br>tempo 6k+/-1=%{x:.6g} ms<br>tempo primi=%{y:.6g} ms<extra></extra>'))
    if elapsed_rows:
        lower = min((min(row[1], row[2]) for row in elapsed_rows))
        upper = max((max(row[1], row[2]) for row in elapsed_rows))
        fig.add_trace(go.Scatter(x=[lower, upper], y=[lower, upper], mode='lines', name='y=x', line=dict(dash='dash')))
    fig.update_layout(title='Confronto dei tempi alla prima comparsa', xaxis_title='Elapsed nei candidati 6k+/-1 (ms)', yaxis_title='Elapsed nei primi (ms)', xaxis_type='log', yaxis_type='log', template='plotly_white')
    if elapsed_rows:
        written.append(plot_write_plotly(fig, output_dir, '23_pair_first_elapsed_comparison.html'))
    fig = go.Figure()
    for label, pairs, total in ((plot_STATE_LABEL_6K1, pairs_6k1, total_6k1), (plot_STATE_LABEL_PRIME, pairs_prime, total_prime)):
        rows = []
        for (a, b), value in pairs.items():
            discriminant = abs(-4 * a ** 3 - 27 * b ** 2)
            if discriminant > 0 and value > 0:
                rows.append((a, b, discriminant, plot_safe_percentage(value, total), value))
        fig.add_trace(go.Scatter(x=[row[2] for row in rows], y=[row[3] for row in rows], mode='markers+text', text=[f'({row[0]},{row[1]})' for row in rows], textposition='top center', name=label, customdata=[row[4] for row in rows], hovertemplate='%{text}<br>|Delta|=%{x:,}<br>quota=%{y:.9g}%<br>occorrenze=%{customdata:,}<extra></extra>'))
    fig.update_layout(title='Discriminante e frequenza relativa delle coppie', xaxis_title='|-4A^3-27B^2| (scala log)', yaxis_title='Quota (%) - scala log', xaxis_type='log', yaxis_type='log', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '24_discriminant_vs_relative_frequency.html'))
    fig = make_subplots(rows=1, cols=2, subplot_titles=('Coppie distinte per ring', 'Numero effettivo exp(H)'))
    diversity_datasets: tuple[tuple[str, dict[tuple[int, int], int]], ...] = (
        (plot_STATE_LABEL_6K1, pairs_6k1),
        (plot_STATE_LABEL_PRIME, pairs_prime),
    )
    for label, pair_frequencies in diversity_datasets:
        distinct_values: list[int] = []
        effective_values: list[float] = []
        for ring in rings:
            values: list[int] = [
                int(value)
                for (a, b), value in pair_frequencies.items()
                if max(abs(int(a)), abs(int(b))) == int(ring) and int(value) > 0
            ]
            distinct_values.append(len(values))
            ring_total = sum(values)
            entropy = -sum((value / ring_total * math.log(value / ring_total) for value in values)) if ring_total else 0.0
            effective_values.append(math.exp(entropy) if values else 0.0)
        fig.add_trace(go.Bar(x=rings, y=distinct_values, name=label, text=distinct_values, textposition='outside'), row=1, col=1)
        fig.add_trace(go.Scatter(x=rings, y=effective_values, mode='lines+markers+text', name=label, showlegend=False, text=[f'{value:.4g}' for value in effective_values], textposition='top center'), row=1, col=2)
    fig.update_xaxes(title_text='Ring')
    fig.update_yaxes(title_text='Numero di coppie', row=1, col=1)
    fig.update_yaxes(title_text='Diversita effettiva', row=1, col=2)
    fig.update_layout(title='Diversita ed entropia delle coppie per ring', barmode='group', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '25_ring_pair_diversity_entropy.html'))
    fig = go.Figure()
    for label, first in ((plot_STATE_LABEL_6K1, first_6k1), (plot_STATE_LABEL_PRIME, first_prime)):
        discoveries = sorted(((plot_as_int(record.get('n')), pair) for pair, record in first.items() if plot_as_int(record.get('n')) > 0))
        fig.add_trace(go.Scatter(x=[row[0] for row in discoveries], y=list(range(1, len(discoveries) + 1)), mode='lines+markers', name=label, customdata=[f'({row[1][0]},{row[1][1]})' for row in discoveries], hovertemplate='n=%{x:,}<br>coppie scoperte=%{y}<br>nuova coppia=%{customdata}<extra></extra>'))
    fig.update_layout(title='Curva di scoperta delle coppie distinte', xaxis_title='Valore n della prima comparsa (scala log)', yaxis_title='Coppie distinte cumulative', xaxis_type='log', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '26_pair_discovery_curve.html'))
    union_pairs = sorted(set(pairs_6k1) | set(pairs_prime))
    difference_rows = []
    for pair in union_pairs:
        share_6k1 = plot_safe_percentage(pairs_6k1.get(pair, 0), total_6k1)
        share_prime = plot_safe_percentage(pairs_prime.get(pair, 0), total_prime)
        difference_rows.append((pair, share_prime - share_6k1, share_6k1, share_prime))
    difference_rows.sort(key=lambda item: abs(item[1]), reverse=True)
    difference_rows = difference_rows[:max(top_n, 20)]
    difference_rows.reverse()
    fig = go.Figure(go.Bar(x=[row[1] for row in difference_rows], y=[f'({row[0][0]},{row[0][1]})' for row in difference_rows], orientation='h', customdata=[[row[2], row[3]] for row in difference_rows], hovertemplate='%{y}<br>delta quota=%{x:.9g} punti percentuali<br>quota 6k+/-1=%{customdata[0]:.9g}%<br>quota primi=%{customdata[1]:.9g}%<extra></extra>'))
    fig.add_vline(x=0.0, line_dash='dash')
    fig.update_layout(title='Coppie responsabili delle differenze fra le distribuzioni', xaxis_title='Quota primi - quota candidati 6k+/-1 (punti percentuali)', yaxis_title='Coppia (A,B)', template='plotly_white', height=max(650, 30 * len(difference_rows)))
    written.append(plot_write_plotly(fig, output_dir, '27_pair_distribution_difference.html'))
    plot_add_html_navigation(output_dir, written)
    return written

def plot_plotly_suite_single(go: Any, make_subplots: Any, summaries: list[tuple[str, dict[str, Any]]], state: dict[str, Any] | None, label: str, output_dir: Path, top_n: int) -> list[str]:
    """Generate the Plotly suite for one ring state."""
    written: list[str] = []
    metrics = (('tested', 'Numbers tested'), ('rate_per_second', 'Speed (n/s)'), ('elapsed_seconds', 'Total time (s)'), ('max_elapsed_ms', 'Maximum single time (ms)'))
    fig = make_subplots(rows=2, cols=2, subplot_titles=[title for _, title in metrics])
    validation_labels = [item_label for item_label, _ in summaries]
    for index, (key, _title) in enumerate(metrics):
        row, col = divmod(index, 2)
        values = [plot_as_float(data.get(key)) for _, data in summaries]
        fig.add_trace(go.Bar(x=validation_labels, y=values, text=[f'{value:,.6g}' for value in values], textposition='outside', showlegend=False), row=row + 1, col=col + 1)
    fig.update_layout(title=f"BRPT - {' and '.join(validation_labels)} validation", template='plotly_white', height=760)
    if summaries:
        written.append(plot_write_plotly(fig, output_dir, '01_validation.html'))
    if state is None:
        plot_add_html_navigation(output_dir, written)
        return written
    counts = plot_ring_counts(state)
    rings = sorted(counts)
    fig = go.Figure(go.Bar(name=label, x=rings, y=[counts[ring] for ring in rings], text=[f'{counts[ring]:,}' for ring in rings], textposition='outside'))
    fig.update_layout(title='Absolute ring distribution', xaxis_title='Ring', yaxis_title='Occurrences (log)', yaxis_type='log', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '02_ring_counts.html'))
    total_rings = sum(counts.values())
    ring_shares = [plot_safe_percentage(counts[ring], total_rings) for ring in rings]
    fig = go.Figure(go.Scatter(name=label, x=rings, y=ring_shares, mode='lines+markers+text', text=[f'{share:.6g}%' for share in ring_shares], textposition='top center'))
    fig.update_layout(title='Normalized ring distribution', xaxis_title='Ring', yaxis_title='Share (%) - log', yaxis_type='log', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '03_ring_share.html'))
    first = plot_ring_first(state)
    if first:
        first_rings = sorted(first)
        hover = [f"A={plot_as_int(first[ring].get('a'))}, B={plot_as_int(first[ring].get('b'))}" for ring in first_rings]
        fig = make_subplots(rows=1, cols=2, subplot_titles=('First occurrence index', 'n value at first occurrence'))
        fig.add_trace(go.Scatter(x=first_rings, y=[plot_as_int(first[ring].get('index')) for ring in first_rings], mode='lines+markers', name=label, customdata=hover, hovertemplate='Ring %{x}<br>index=%{y:,}<br>%{customdata}<extra></extra>'), row=1, col=1)
        fig.add_trace(go.Scatter(x=first_rings, y=[plot_as_int(first[ring].get('n')) for ring in first_rings], mode='lines+markers', name=label, showlegend=False, customdata=hover, hovertemplate='Ring %{x}<br>n=%{y:,}<br>%{customdata}<extra></extra>'), row=1, col=2)
        fig.update_yaxes(type='log', row=1, col=1)
        fig.update_yaxes(type='log', row=1, col=2)
        fig.update_layout(title='Ring discovery', template='plotly_white')
        written.append(plot_write_plotly(fig, output_dir, '04_ring_first_seen.html'))
    running = 0
    cumulative_rings = []
    for ring in rings:
        running += counts[ring]
        cumulative_rings.append(plot_safe_percentage(running, total_rings))
    fig = go.Figure(go.Scatter(x=rings, y=cumulative_rings, mode='lines+markers', name=label, hovertemplate='Ring <= %{x}<br>coverage=%{y:.10g}%<extra></extra>'))
    fig.update_layout(title='Cumulative ring coverage', xaxis_title='Maximum ring', yaxis_title='Coverage (%)', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '05_ring_cumulative.html'))
    written.append(plot_write_plotly(plot_plotly_heatmap(go, state, label), output_dir, '06_pair_heatmap.html'))
    written.append(plot_write_plotly(plot_plotly_top_pairs(go, state, label, top_n), output_dir, '07_top_pairs.html'))
    pairs = plot_pair_counts(state)
    total_pairs = sum(pairs.values())
    a_counts: dict[int, int] = {}
    b_counts: dict[int, int] = {}
    for (a, b), value in pairs.items():
        a_counts[a] = a_counts.get(a, 0) + value
        b_counts[b] = b_counts.get(b, 0) + value
    fig = make_subplots(rows=1, cols=2, subplot_titles=('Distribution of A', 'Distribution of B'))
    fig.add_trace(go.Scatter(x=sorted(a_counts), y=[plot_safe_percentage(a_counts[a], total_pairs) for a in sorted(a_counts)], mode='lines+markers', name=label), row=1, col=1)
    fig.add_trace(go.Scatter(x=sorted(b_counts), y=[plot_safe_percentage(b_counts[b], total_pairs) for b in sorted(b_counts)], mode='lines+markers', name=label, showlegend=False), row=1, col=2)
    fig.update_yaxes(type='log', title_text='Share (%) - log')
    fig.update_layout(title='Normalized coefficient marginal distributions', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '08_coefficient_marginals.html'))
    written.append(plot_write_plotly(plot_plotly_3d_frequency(go, state, label), output_dir, '09_3d_frequency.html'))
    if plot_has_pair_discovery(state):
        written.append(plot_write_plotly(plot_plotly_3d_birth(go, state, label), output_dir, '10_3d_birth.html'))
    ranked_values: list[int] = sorted((int(value) for value in pairs.values()), reverse=True)
    running: int = 0
    cumulative_pairs: list[float] = []
    for value in ranked_values:
        running += value
        cumulative_pairs.append(plot_safe_percentage(running, total_pairs))
    fig = go.Figure(go.Scatter(x=list(range(1, len(cumulative_pairs) + 1)), y=cumulative_pairs, mode='lines+markers', name=label, hovertemplate='top %{x} pairs<br>coverage=%{y:.10g}%<extra></extra>'))
    fig.update_layout(title='Cumulative pair coverage', xaxis_title='Number of most frequent pairs', yaxis_title='Cumulative coverage (%)', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '11_pair_cumulative_coverage.html'))
    survival = [plot_safe_percentage(sum((value for ring_value, value in counts.items() if ring_value >= ring)), total_rings) for ring in rings]
    fig = go.Figure(go.Scatter(x=rings, y=survival, mode='lines+markers+text', text=[f'{value:.8g}%' for value in survival], textposition='top center', name=label, hovertemplate='R >= %{x}<br>share=%{y:.10g}%<extra></extra>'))
    fig.update_layout(title='Ring survival function', xaxis_title='Minimum required ring', yaxis_title='P(R >= r) (%) - log scale', yaxis_type='log', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '12_ring_survival.html'))
    discriminant_rows = []
    for (a, b), value in pairs.items():
        discriminant = abs(-4 * a ** 3 - 27 * b ** 2)
        if discriminant > 0 and value > 0:
            discriminant_rows.append((a, b, discriminant, plot_safe_percentage(value, total_pairs), value))
    fig = go.Figure(go.Scatter(x=[row[2] for row in discriminant_rows], y=[row[3] for row in discriminant_rows], mode='markers+text', text=[f'({row[0]},{row[1]})' for row in discriminant_rows], textposition='top center', name=label, customdata=[row[4] for row in discriminant_rows], hovertemplate='%{text}<br>|Delta|=%{x:,}<br>share=%{y:.9g}%<br>occurrences=%{customdata:,}<extra></extra>'))
    fig.update_layout(title='Discriminant and relative pair frequency', xaxis_title='|-4A^3-27B^2| (log scale)', yaxis_title='Share (%) - log scale', xaxis_type='log', yaxis_type='log', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '13_discriminant_vs_relative_frequency.html'))
    fig = make_subplots(rows=1, cols=2, subplot_titles=('Distinct pairs by ring', 'Effective number exp(H)'))
    distinct_values = []
    effective_values = []
    for ring in rings:
        values = [value for (a, b), value in pairs.items() if max(abs(a), abs(b)) == ring and value > 0]
        distinct_values.append(len(values))
        ring_total = sum(values)
        entropy = -sum((value / ring_total * math.log(value / ring_total) for value in values)) if ring_total else 0.0
        effective_values.append(math.exp(entropy) if values else 0.0)
    fig.add_trace(go.Bar(x=rings, y=distinct_values, name=label, text=distinct_values, textposition='outside'), row=1, col=1)
    fig.add_trace(go.Scatter(x=rings, y=effective_values, mode='lines+markers+text', name=label, showlegend=False, text=[f'{value:.4g}' for value in effective_values], textposition='top center'), row=1, col=2)
    fig.update_xaxes(title_text='Ring')
    fig.update_yaxes(title_text='Number of pairs', row=1, col=1)
    fig.update_yaxes(title_text='Effective diversity', row=1, col=2)
    fig.update_layout(title='Pair diversity and entropy by ring', template='plotly_white')
    written.append(plot_write_plotly(fig, output_dir, '14_ring_pair_diversity_entropy.html'))
    discoveries = sorted(((plot_as_int(record.get('n')), pair) for pair, record in plot_pair_first(state).items() if plot_as_int(record.get('n')) > 0))
    if discoveries:
        fig = go.Figure(go.Scatter(x=[row[0] for row in discoveries], y=list(range(1, len(discoveries) + 1)), mode='lines+markers', name=label, customdata=[f'({row[1][0]},{row[1][1]})' for row in discoveries], hovertemplate='n=%{x:,}<br>pairs discovered=%{y}<br>new pair=%{customdata}<extra></extra>'))
        fig.update_layout(title='Distinct-pair discovery curve', xaxis_title='n value at first occurrence (log scale)', yaxis_title='Cumulative distinct pairs', xaxis_type='log', template='plotly_white')
        written.append(plot_write_plotly(fig, output_dir, '15_pair_discovery_curve.html'))
    if plot_has_pair_data(state):
        written.append(plot_write_plotly_pyramid_3d(go, state, label, output_dir))
    plot_add_html_navigation(output_dir, written)
    return written

def plot_write_dataset_index(
    output_dir: Path,
    index_name: str,
    label: str,
    plotly_names: list[str],
    static_stems: list[str],
    static_formats: tuple[str, ...],
) -> None:
    """Write a dataset-specific index in the common output directory."""
    lines = [
        '<!doctype html>',
        '<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        f'<title>{html.escape(label)} plots</title>',
        '<style>body{font:16px system-ui,sans-serif;max-width:1000px;margin:40px auto;padding:0 20px;color:#111827}a{color:#1d4ed8}.back{display:inline-block;margin-bottom:20px;padding:8px 12px;background:#e8f0fb;border-radius:7px;text-decoration:none}.cards{display:grid;grid-template-columns:repeat(auto-fit,minmax(260px,1fr));gap:14px}.card{border:1px solid #d1d5db;border-radius:10px;padding:14px;background:#fff}.card h3{font-size:16px;margin:0 0 10px}.formats{display:flex;gap:9px;flex-wrap:wrap}</style>',
        '</head><body>',
        '<a class="back" href="index.html">&larr; General index</a>',
        f'<h1>{html.escape(label)} plots</h1>',
    ]
    if plotly_names:
        lines.extend(['<h2>Interactive charts</h2>', '<div class="cards">'])
        for name in plotly_names:
            lines.append(
                f'<div class="card"><h3>{html.escape(name)}</h3>'
                f'<a href="{html.escape(name, quote=True)}">Open interactive chart</a></div>'
            )
        lines.append('</div>')
    if static_stems and static_formats:
        lines.extend(['<h2>Static charts</h2>', '<div class="cards">'])
        for stem in static_stems:
            links = ' '.join(
                f'<a href="{html.escape(stem + "." + extension, quote=True)}">{extension.upper()}</a>'
                for extension in static_formats
                if (output_dir / f'{stem}.{extension}').is_file()
            )
            if links:
                lines.append(
                    f'<div class="card"><h3>{html.escape(stem)}</h3>'
                    f'<div class="formats">{links}</div></div>'
                )
        lines.append('</div>')
    lines.extend(['</body></html>'])
    path = output_dir / index_name
    path.write_text('\n'.join(lines), encoding='utf-8')
    print(f'written: {path}')


def plot_write_index(
    output_dir: Path,
    plotly_names: list[str],
    static_stems: list[str],
    static_formats: tuple[str, ...],
    inputs: dict[str, Path],
    states: list[tuple[str, dict[str, Any]]],
    validations: list[tuple[str, dict[str, Any]]],
) -> None:
    lines = [
        '<!doctype html>',
        '<html lang="en"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">',
        '<title>BRPT plots</title>',
        '<style>body{font:16px system-ui,sans-serif;max-width:1100px;margin:40px auto;padding:0 20px;color:#111827}table{border-collapse:collapse;width:100%;margin:16px 0 28px}th,td{border:1px solid #d1d5db;padding:8px 10px;text-align:right}th:first-child,td:first-child{text-align:left}a{color:#1d4ed8}code{background:#f3f4f6;padding:2px 5px;border-radius:4px}</style>',
        '</head><body>',
        '<h1>BRPT diagnostic plots</h1>',
        f'<p><strong>Generator version:</strong> <code>{plot_SCRIPT_VERSION}</code></p>',
        '<h2>Input</h2><ul>',
    ]
    for key, path in inputs.items():
        lines.append(f'<li><strong>{html.escape(key)}</strong>: <code>{html.escape(str(path))}</code></li>')
    lines.append('</ul>')
    if states:
        lines.extend([
            '<h2>Scan summaries</h2>',
            '<table><thead><tr><th>Dataset</th><th>Tested</th><th>Maximum n reached</th><th>With pair</th><th>Without pair</th><th>Failures</th><th>Distinct pairs</th><th>Maximum ring</th><th>Errors</th></tr></thead><tbody>',
        ])
        for label, state in states:
            kind = 'prime' if label == plot_STATE_LABEL_PRIME else '6k1'
            summary = plot_state_summary(state, kind)
            lines.append(
                f"<tr><td>{html.escape(label)}</td>"
                f"<td>{summary['tested']:,}</td>"
                f"<td>{summary['last_value']:,}</td>"
                f"<td>{summary['with_pair']:,}</td>"
                f"<td>{summary['without_pair']:,}</td>"
                f"<td>{plot_as_int(summary.get('failures')):,}</td>"
                f"<td>{summary['distinct_pairs']}</td>"
                f"<td>{summary['max_ring']}</td>"
                f"<td>{summary['errors']}</td></tr>"
            )
        lines.append('</tbody></table>')
    if validations:
        lines.extend(['<h2>Validation summary</h2>', '<table><thead><tr><th>Dataset</th><th>Tested</th><th>Mismatches</th><th>Errors</th><th>Time (s)</th><th>Speed (n/s)</th><th>Maximum (ms)</th></tr></thead><tbody>'])
        for label, data in validations:
            lines.append(f"<tr><td>{html.escape(label)}</td><td>{plot_as_int(data.get('tested')):,}</td><td>{plot_as_int(data.get('mismatches'))}</td><td>{plot_as_int(data.get('errors'))}</td><td>{plot_as_float(data.get('elapsed_seconds')):,.3f}</td><td>{plot_as_float(data.get('rate_per_second')):,.3f}</td><td>{plot_as_float(data.get('max_elapsed_ms')):,.6f}</td></tr>")
        lines.append('</tbody></table>')
    dataset_indexes: list[tuple[str, str]] = []
    for index_name, label in (
        ('index_ring.html', 'RING — 6k±1 candidates'),
        ('index_prime.html', 'PRIME — exact primes'),
    ):
        if (output_dir / index_name).is_file():
            dataset_indexes.append((index_name, label))
    if dataset_indexes:
        lines.extend(['<h2>Dataset galleries</h2>', '<ul>'])
        for index_name, label in dataset_indexes:
            lines.append(
                f'<li><a href="{html.escape(index_name, quote=True)}">'
                f'{html.escape(label)}</a></li>'
            )
        lines.append('</ul>')
    if plotly_names:
        lines.extend(['<h2>Interactive charts</h2>', '<ol>'])
        lines.extend(f'<li><a href="{html.escape(name)}">{html.escape(name)}</a></li>' for name in plotly_names)
        lines.append('</ol>')
    if static_stems and static_formats:
        lines.extend(['<h2>Static charts</h2>', '<ul>'])
        for stem in static_stems:
            links = ' · '.join(f'<a href="{html.escape(stem + "." + extension)}">{extension.upper()}</a>' for extension in static_formats)
            lines.append(f'<li>{html.escape(stem)} — {links}</li>')
        lines.append('</ul>')
    lines.extend(['</body></html>'])
    path = output_dir / 'index.html'
    path.write_text('\n'.join(lines), encoding='utf-8')
    print(f'written: {path}')


def plot_write_run_summary(
    output_dir: Path,
    inputs: dict[str, Path],
    states: list[tuple[str, dict[str, Any]]],
    validations: list[tuple[str, dict[str, Any]]],
) -> None:
    payload: dict[str, Any] = {
        'inputs': {key: str(path) for key, path in inputs.items()},
        'states': {},
        'validations': {},
    }
    for label, state in states:
        kind = 'prime' if label == plot_STATE_LABEL_PRIME else '6k1'
        payload['states'][label] = plot_state_summary(state, kind)
    for label, data in validations:
        payload['validations'][label.lower()] = {
            'tested': plot_as_int(data.get('tested')),
            'mismatches': plot_as_int(data.get('mismatches')),
            'errors': plot_as_int(data.get('errors')),
            'elapsed_seconds': plot_as_float(data.get('elapsed_seconds')),
            'rate_per_second': plot_as_float(data.get('rate_per_second')),
            'max_elapsed_ms': plot_as_float(data.get('max_elapsed_ms')),
        }
    path = output_dir / 'plot_summary.json'
    path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding='utf-8')
    print(f'written: {path}')


def plot_parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description='BRPT diagnostic charts from a ring state and C21/PSPS summaries')
    parser.add_argument('--ring', type=Path, help='ring state JSON')
    parser.add_argument('--prime', type=Path, help='PRIME state JSON (state_prime_scan.json or summary_prime_scan.json)')
    parser.add_argument('--c21', type=Path, help='C21 summary JSON')
    parser.add_argument('--psps', type=Path, help='PSPS summary JSON')
    parser.add_argument('--output-dir', type=Path, default=Path('results_plots'))
    parser.add_argument('--top-pairs', type=int, default=20)
    parser.add_argument('--backend', choices=('matplotlib', 'plotly', 'both'), default='both', help='chart backend to use (default: both)')
    parser.add_argument('--static-format', choices=('png', 'svg', 'both', 'none'), default='both', help='Matplotlib chart format (default: both)')
    args = parser.parse_args()
    if args.ring is None and args.prime is None and args.c21 is None and args.psps is None:
        parser.error('specify at least one of --ring, --prime, --c21, and --psps')
    if args.top_pairs < 1:
        parser.error('--top-pairs must be >= 1')
    if args.backend == 'matplotlib' and args.static_format == 'none':
        parser.error('--backend matplotlib requires --static-format png, svg, or both')
    return args

def plot_main() -> None:
    args = plot_parse_args()
    print(f'BRPT plot generator {plot_SCRIPT_VERSION}')
    args.output_dir.mkdir(parents=True, exist_ok=True)
    for stale_index_name in ('index.html', 'index_ring.html', 'index_prime.html'):
        stale_index = args.output_dir / stale_index_name
        if stale_index.is_file():
            stale_index.unlink()
    # Remove stale flat dataset charts from an earlier PLOT generation. This
    # prevents a chronology chart from surviving after the new checkpoint no
    # longer contains the data required to regenerate it.
    for pattern in ('ring_*.html', 'ring_*.png', 'ring_*.svg',
                    'prime_*.html', 'prime_*.png', 'prime_*.svg'):
        for stale_chart in args.output_dir.glob(pattern):
            if stale_chart.is_file():
                stale_chart.unlink()
    # Remove the obsolete nested layout produced by earlier versions.
    for obsolete_dir in (args.output_dir / 'ring', args.output_dir / 'prime'):
        if obsolete_dir.is_dir():
            shutil.rmtree(obsolete_dir)
    dataset_temp_dirs: dict[str, Path] = {}

    inputs: dict[str, Path] = {}
    if args.ring is not None:
        inputs['ring'] = args.ring.expanduser()
    if args.prime is not None:
        inputs['prime'] = args.prime.expanduser()
    if args.c21 is not None:
        inputs['C21'] = args.c21.expanduser()
    if args.psps is not None:
        inputs['PSPS'] = args.psps.expanduser()
    missing = [path for path in inputs.values() if not path.is_file()]
    if missing:
        formatted = '\n'.join(f'  - {path}' for path in missing)
        raise SystemExit(f'Input files not found:\n{formatted}')

    ring_state: dict[str, Any] | None = None
    prime_state: dict[str, Any] | None = None
    if 'ring' in inputs:
        ring_state, detailed_path = plot_load_ring_json(inputs['ring'])
        if detailed_path != inputs['ring']:
            inputs['ring state'] = detailed_path
    if 'prime' in inputs:
        prime_state, detailed_path = plot_load_prime_json(inputs['prime'])
        if detailed_path != inputs['prime']:
            inputs['prime state'] = detailed_path

    c21 = plot_load_json(inputs['C21']) if 'C21' in inputs else None
    psps = plot_load_json(inputs['PSPS']) if 'PSPS' in inputs else None
    states: list[tuple[str, dict[str, Any]]] = []
    if ring_state is not None:
        states.append((plot_STATE_LABEL_6K1, ring_state))
    if prime_state is not None:
        states.append((plot_STATE_LABEL_PRIME, prime_state))

    summaries: list[tuple[str, dict[str, Any]]] = []
    if c21 is not None:
        summaries.append((plot_dataset_label(c21, 'C21'), c21))
    if psps is not None:
        summaries.append((plot_dataset_label(psps, 'PSPS'), psps))

    if args.static_format == 'both':
        static_formats: tuple[str, ...] = ('png', 'svg')
    elif args.static_format == 'none':
        static_formats = ()
    else:
        static_formats = (args.static_format,)

    static_stems: list[str] = []
    if args.backend in ('matplotlib', 'both') and static_formats:
        try:
            import matplotlib
            matplotlib.use('Agg')
            import matplotlib.pyplot as plt
        except ImportError as exc:
            raise SystemExit('Matplotlib is not installed: python3 -m pip install matplotlib') from exc

        if summaries:
            plot_static_validation(plt, summaries, args.output_dir, static_formats)
            static_stems.append('01_validation')

        multiple_states = len(states) > 1
        for label, state in states:
            slug = 'prime' if label == plot_STATE_LABEL_PRIME else 'ring'
            target = args.output_dir
            if multiple_states:
                target = args.output_dir / f'.{slug}_plots_tmp'
                if target.is_dir():
                    shutil.rmtree(target)
                target.mkdir(parents=True, exist_ok=True)
                dataset_temp_dirs[slug] = target
            prefix = f'{slug}_' if multiple_states else ''
            singleton = [(label, state)]

            plot_static_ring_counts(plt, singleton, target, static_formats)
            plot_static_ring_shares(plt, singleton, target, static_formats)
            if plot_has_ring_discovery(state):
                plot_static_ring_first_seen(plt, singleton, target, static_formats)
            plot_static_ring_cumulative(plt, singleton, target, static_formats)
            plot_static_pair_heatmap(plt, state, label, '06_pair_heatmap', target, static_formats)
            plot_static_top_pairs(plt, state, label, '07_top_pairs', args.top_pairs, target, static_formats)
            plot_static_coefficient_marginals(plt, singleton, target, static_formats)
            plot_static_3d_frequency(plt, state, label, '09_3d_frequency', target, static_formats)
            if plot_has_pair_discovery(state):
                plot_static_3d_birth(plt, state, label, '10_3d_birth', target, static_formats)
            plot_static_pair_cumulative_coverage(plt, singleton, target, static_formats)
            plot_static_ring_survival(plt, singleton, target, static_formats)
            plot_static_discriminant_vs_relative_frequency(plt, singleton, target, static_formats)
            plot_static_ring_pair_diversity_entropy(plt, singleton, target, static_formats)
            if plot_has_pair_discovery(state):
                plot_static_pair_discovery_curve(plt, singleton, target, static_formats)
            if plot_has_pair_data(state):
                plot_static_3d_pyramid(plt, state, label, target, static_formats)

            stems = ['02_ring_counts', '03_ring_share']
            if plot_has_ring_discovery(state):
                stems.append('04_ring_first_seen')
            stems.extend(['05_ring_cumulative', '06_pair_heatmap', '07_top_pairs', '08_coefficient_marginals', '09_3d_frequency'])
            if plot_has_pair_discovery(state):
                stems.append('10_3d_birth')
            stems.extend(['11_pair_cumulative_coverage', '12_ring_survival', '13_discriminant_vs_relative_frequency', '14_ring_pair_diversity_entropy'])
            if plot_has_pair_discovery(state):
                stems.append('15_pair_discovery_curve')
            if plot_has_pair_data(state):
                stems.append('16_3d_pyramid')
            static_stems.extend(prefix + stem for stem in stems)

        if ring_state is not None and prime_state is not None:
            comparison_states = [(plot_STATE_LABEL_6K1, ring_state), (plot_STATE_LABEL_PRIME, prime_state)]
            plot_static_ring_counts(plt, comparison_states, args.output_dir, static_formats)
            plot_static_ring_shares(plt, comparison_states, args.output_dir, static_formats)
            plot_static_ring_cumulative(plt, comparison_states, args.output_dir, static_formats)
            plot_static_pair_share_comparison(plt, comparison_states, args.top_pairs, args.output_dir, static_formats)
            plot_static_state_outcomes(plt, ring_state, prime_state, args.output_dir, static_formats)
            plot_static_coefficient_marginals(plt, comparison_states, args.output_dir, static_formats)
            plot_static_pair_share_correlation(plt, ring_state, prime_state, args.output_dir, static_formats)
            plot_static_pair_cumulative_coverage(plt, comparison_states, args.output_dir, static_formats)
            plot_static_pair_relative_enrichment(plt, ring_state, prime_state, args.top_pairs, args.output_dir, static_formats)
            plot_static_ring_survival(plt, comparison_states, args.output_dir, static_formats)
            plot_static_pair_rank_stability(plt, ring_state, prime_state, args.output_dir, static_formats)
            if plot_has_pair_discovery(ring_state) and plot_has_pair_discovery(prime_state):
                plot_static_pair_birth_index_comparison(plt, ring_state, prime_state, args.output_dir, static_formats)
                plot_static_pair_first_elapsed_comparison(plt, ring_state, prime_state, args.output_dir, static_formats)
            plot_static_discriminant_vs_relative_frequency(plt, comparison_states, args.output_dir, static_formats)
            plot_static_ring_pair_diversity_entropy(plt, comparison_states, args.output_dir, static_formats)
            plot_static_pair_distribution_difference(plt, ring_state, prime_state, args.top_pairs, args.output_dir, static_formats)
            comparison_stems = [
                '02_ring_counts', '03_ring_share', '05_ring_cumulative',
                '10_pair_share_comparison', '11_state_outcomes', '08_coefficient_marginals',
                '13_pair_share_correlation', '11_pair_cumulative_coverage',
                '19_pair_relative_enrichment', '12_ring_survival',
                '21_pair_rank_stability', '13_discriminant_vs_relative_frequency',
                '14_ring_pair_diversity_entropy', '27_pair_distribution_difference',
            ]
            if plot_has_pair_discovery(ring_state) and plot_has_pair_discovery(prime_state):
                comparison_stems.extend(['22_pair_birth_index_comparison', '23_pair_first_elapsed_comparison'])
            static_stems.extend(comparison_stems)

    plotly_names: list[str] = []
    if args.backend in ('plotly', 'both'):
        try:
            import plotly.graph_objects as go
            from plotly.subplots import make_subplots
        except ImportError as exc:
            raise SystemExit('Plotly is not installed: python3 -m pip install plotly') from exc

        if ring_state is not None and prime_state is not None:
            # Keep comparison charts in the common output directory. Dataset
            # charts are generated in temporary directories and then moved to
            # the same directory with ring_ / prime_ prefixes.
            plotly_names.extend(
                plot_plotly_suite(
                    go,
                    make_subplots,
                    summaries,
                    [(plot_STATE_LABEL_6K1, ring_state), (plot_STATE_LABEL_PRIME, prime_state)],
                    ring_state,
                    prime_state,
                    args.output_dir,
                    args.top_pairs,
                )
            )

            for label, state in (
                (plot_STATE_LABEL_6K1, ring_state),
                (plot_STATE_LABEL_PRIME, prime_state),
            ):
                slug = 'prime' if label == plot_STATE_LABEL_PRIME else 'ring'
                target = dataset_temp_dirs.get(slug, args.output_dir / f'.{slug}_plots_tmp')
                target.mkdir(parents=True, exist_ok=True)
                dataset_temp_dirs[slug] = target
                local_names = plot_plotly_suite_single(
                    go,
                    make_subplots,
                    [],
                    state,
                    label,
                    target,
                    args.top_pairs,
                )
                plotly_names.extend(f'{slug}_{name}' for name in local_names)

                if plot_has_pair_data(state):
                    chart16 = target / '16_3d_pyramid.html'
                    if '16_3d_pyramid.html' not in local_names or not chart16.is_file():
                        raise RuntimeError(
                            f'Chart 16 generation failed before flat rename: {chart16}'
                        )
        elif states:
            label, state = states[0]
            plotly_names = plot_plotly_suite_single(go, make_subplots, summaries, state, label, args.output_dir, args.top_pairs)
            if plot_has_pair_data(state):
                chart16 = args.output_dir / '16_3d_pyramid.html'
                if '16_3d_pyramid.html' not in plotly_names or not chart16.is_file():
                    raise RuntimeError(f'Chart 16 generation failed: {chart16}')
        else:
            plotly_names = plot_plotly_suite_single(go, make_subplots, summaries, None, '', args.output_dir, args.top_pairs)

    if len(states) > 1:
        # Flatten temporary dataset galleries into the common output folder.
        for slug, target in dataset_temp_dirs.items():
            if not target.is_dir():
                continue
            for source in sorted(target.iterdir()):
                if not source.is_file():
                    continue
                destination = args.output_dir / f'{slug}_{source.name}'
                if destination.exists():
                    destination.unlink()
                source.replace(destination)
            shutil.rmtree(target)

        for slug, label in (
            ('ring', plot_STATE_LABEL_6K1),
            ('prime', plot_STATE_LABEL_PRIME),
        ):
            local_static = [stem for stem in static_stems if stem.startswith(f'{slug}_')]
            local_plotly = [name for name in plotly_names if name.startswith(f'{slug}_')]
            if local_static or local_plotly:
                plot_write_dataset_index(
                    args.output_dir,
                    f'index_{slug}.html',
                    label,
                    local_plotly,
                    local_static,
                    static_formats,
                )

    if plotly_names:
        # One uninterrupted sequence: comparisons, RING charts, PRIME charts.
        plot_add_html_navigation(args.output_dir, plotly_names)

    plot_write_run_summary(args.output_dir, inputs, states, summaries)
    plot_write_index(args.output_dir, plotly_names, static_stems, static_formats, inputs, states, summaries)
    print(f'completed: {args.output_dir.resolve()}')


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description=textwrap.dedent('''
            Run BRPT dataset tests, ring scans, exact prime scans, or diagnostic plots.

            --mode FILE reads integers from a text, .gz, or .bz2 dataset and
            compares every BRPT result with the classification selected by
            --expected.

            --mode RING generates integers congruent to 1 or 5 modulo 6,
            compares BRPT with sympy.isprime, and tracks coefficient pairs,
            rings, frequencies, mismatches, and resumable checkpoints.

            --mode PRIME generates only exact primes with a segmented sieve,
            submits them to BRPT, and stops at the first false negative or
            execution error. The scan is checkpointed and resumable.

            --mode PLOT reads completed JSON reports and generates static
            Matplotlib charts, interactive Plotly charts, or both.
        '''),
        epilog=textwrap.dedent('''
            examples:
              C21 dataset (all numbers expected to be composite):
                python brpt_test.py --mode FILE --input c21.gz --output results_c21

              Scan 100,000 candidates of the form 6k-1 or 6k+1:
                python brpt_test.py --mode RING --start 1 --count 100000 --output results_ring

              Test every prime from 2 through 1,000,000:
                python brpt_test.py --mode PRIME --start 2 --stop 1000000 --output results_primes

              Continue the exact prime scan until Ctrl+C:
                python brpt_test.py --mode PRIME --output results_primes

              Generate plots from RING, PRIME, C21, and PSPS reports:
                python brpt_test.py --mode PLOT --ring results_ring/state_ring.json --prime results_primes/state_prime_scan.json --c21 results_c21/summary_c21.json --psps results_psps/summary_psps.json --output results_plots

            PRIME output:
              state_prime_scan.json, summary_prime_scan.json,
              false_negatives.csv, errors.csv and, with --events,
              prime_results.csv.

            FILE, RING, and PRIME automatically load brpt.py from the
            directory containing brpt_test.py and use all logical CPUs.

            Parameters that do not belong to the selected mode are rejected.
        '''),
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    required = parser.add_argument_group('required parameter')
    required.add_argument(
        '--mode', required=True, type=str.upper,
        choices=('FILE', 'RING', 'PRIME', 'PRIMES', 'PLOT'),
        help='operation to run: FILE, RING, PRIME, or PLOT (PRIMES is accepted as an alias)',
    )
    all_modes = parser.add_argument_group('parameter valid in every mode')
    all_modes.add_argument(
        '--output', dest='output_dir', type=Path, metavar='DIR',
        help='directory for generated reports; in resumable modes it also selects the checkpoint to resume',
    )
    test_modes = parser.add_argument_group('parameters valid with --mode FILE, RING, or PRIME')
    test_modes.add_argument(
        '--progress', dest='progress_every', type=int, metavar='N',
        help='print progress after every N completed tests; 0 disables it',
    )
    file_group = parser.add_argument_group('parameters valid only with --mode FILE')
    file_group.add_argument('--input', dest='input_file', type=Path, metavar='PATH', help='input dataset in plain text, gzip (.gz), or bzip2 (.bz2) format')
    file_group.add_argument('--expected', choices=('COMPOSITE', 'PRIME'), help='classification expected for every input number; default: COMPOSITE')
    scan_group = parser.add_argument_group('parameters valid with --mode RING or PRIME')
    scan_group.add_argument('--start', type=int, metavar='N', help='starting bound; exclusive in RING mode and inclusive in PRIME mode')
    scan_group.add_argument('--save', dest='save_every', type=int, metavar='N', help='rewrite the checkpoint after every N completed tests')
    scan_group.add_argument('--restart', action='store_true', help='ignore an existing checkpoint and restart from --start')
    scan_group.add_argument('--events', dest='save_events', action='store_true', help='save one CSV row for every completed test')
    ring_group = parser.add_argument_group('parameters valid only with --mode RING')
    ring_group.add_argument('--count', type=int, metavar='N', help='number of generated 6k+/-1 candidates; omit to continue indefinitely')
    ring_group.add_argument('--probable', dest='allow_probable_reference', action='store_true', help='allow SymPy comparisons for n >= 2**64, marked as probable')
    prime_group = parser.add_argument_group('parameters valid only with --mode PRIME')
    prime_group.add_argument('--stop', type=int, metavar='N', help='inclusive upper bound; 0 or omission means continue until interrupted')
    prime_group.add_argument('--segment', dest='segment_size', type=int, metavar='N', help='width of each exact segmented-sieve interval; default: 2,000,000')
    prime_group.add_argument('--allow-module-change', action='store_true', help='resume even if brpt.py path or SHA-256 differs from the checkpoint')
    plot_group = parser.add_argument_group('parameters valid only with --mode PLOT')
    plot_group.add_argument('--ring', type=Path, metavar='JSON', help='RING state JSON containing ring_counts and pair_counts')
    plot_group.add_argument('--prime', type=Path, metavar='JSON', help='PRIME state or summary JSON; the detailed state is resolved automatically')
    plot_group.add_argument('--c21', type=Path, metavar='JSON', help='summary_c21.json generated by a completed C21 FILE test')
    plot_group.add_argument('--psps', type=Path, metavar='JSON', help='summary_psps.json generated by a completed PSPS FILE test')
    plot_group.add_argument('--pairs', dest='top_pairs', type=int, metavar='N', help='number of highest-frequency coefficient pairs shown in ranked charts; default: 20')
    plot_group.add_argument('--backend', choices=('matplotlib', 'plotly', 'both'), help='chart engine; default: both')
    plot_group.add_argument('--format', dest='static_format', choices=('png', 'svg', 'both', 'none'), help='static Matplotlib output format; default: both')
    return parser


def validate_args(parser: argparse.ArgumentParser, args: argparse.Namespace) -> None:
    plot_options = (
        args.ring is not None, args.prime is not None, args.c21 is not None, args.psps is not None,
        args.top_pairs is not None, args.backend is not None,
        args.static_format is not None,
    )
    file_options = (args.input_file is not None, args.expected is not None)
    scan_options = (args.start is not None, args.save_every is not None, args.restart, args.save_events)
    ring_options = (args.count is not None, args.allow_probable_reference)
    prime_options = (args.stop is not None, args.segment_size is not None, args.allow_module_change)

    if args.mode in ('file', 'ring', 'prime'):
        if args.progress_every is not None and args.progress_every < 0:
            parser.error('--progress must be >= 0')
        if any(plot_options):
            parser.error('PLOT-only parameters require --mode PLOT')

    if args.mode == 'file':
        if args.input_file is None:
            parser.error('--input is required with --mode FILE')
        if any(scan_options) or any(ring_options) or any(prime_options):
            parser.error('scan parameters cannot be used with --mode FILE')
    elif args.mode == 'ring':
        if any(file_options) or any(prime_options):
            parser.error('FILE/PRIME-only parameters cannot be used with --mode RING')
        if args.start is not None and args.start < 0:
            parser.error('--start must be >= 0')
        if args.count is not None and args.count < 1:
            parser.error('--count must be >= 1')
        if args.save_every is not None and args.save_every < 1:
            parser.error('--save must be >= 1')
    elif args.mode == 'prime':
        if any(file_options) or any(ring_options):
            parser.error('FILE/RING-only parameters cannot be used with --mode PRIME')
        start = 2 if args.start is None else args.start
        stop = 0 if args.stop is None else args.stop
        if start < 0:
            parser.error('--start must be >= 0')
        if stop < 0 or (stop and stop < start):
            parser.error('--stop must be 0 or >= --start')
        if args.segment_size is not None and args.segment_size < 1:
            parser.error('--segment must be >= 1')
        if args.save_every is not None and args.save_every < 1:
            parser.error('--save must be >= 1')
    else:
        if any(file_options) or any(scan_options) or any(ring_options) or any(prime_options):
            parser.error('test parameters cannot be used with --mode PLOT')
        if args.progress_every is not None:
            parser.error('--progress cannot be used with --mode PLOT')
        if not any((args.ring, args.prime, args.c21, args.psps)):
            parser.error('--mode PLOT requires at least one of --ring, --prime, --c21, or --psps')
        if args.top_pairs is not None and args.top_pairs < 1:
            parser.error('--pairs must be >= 1')
        if args.backend == 'matplotlib' and args.static_format == 'none':
            parser.error('--backend matplotlib cannot be combined with --format none')


def append_option(target: list[str], name: str, value: str | int | float | Path | None) -> None:
    if value is not None:
        target.extend((name, str(value)))

def automatic_brpt_module() -> Path:
    """Return brpt.py stored beside this unified launcher."""
    return Path(__file__).resolve().with_name('brpt.py')

def available_logical_cpus() -> int:
    """Return the logical CPUs usable by this process, with safe fallbacks."""
    cpu_counter = getattr(os, 'process_cpu_count', os.cpu_count)
    detected = cpu_counter()
    return max(1, detected or 1)

def resolve_parallelism(args: argparse.Namespace) -> tuple[int, int]:
    """Use every logical CPU and choose a throughput-oriented batch size."""
    workers = available_logical_cpus()
    chunksize = max(1, min(64, 2048 // workers))
    known_tasks = args.count if args.mode == 'ring' else None
    if isinstance(known_tasks, int):
        per_worker = max(1, (known_tasks + workers - 1) // workers)
        chunksize = min(chunksize, per_worker)
    return workers, chunksize

def build_forwarded_args(args: argparse.Namespace) -> list[str]:
    forwarded: list[str] = []
    if args.mode == 'file':
        forwarded.append(str(args.input_file))
        append_option(forwarded, '--expected', args.expected)
    elif args.mode == 'ring':
        append_option(forwarded, '--start', args.start)
        append_option(forwarded, '--count', args.count)
        append_option(forwarded, '--save-every', args.save_every)
        for enabled, option in (
            (args.restart, '--restart'),
            (args.save_events, '--save-events'),
            (args.allow_probable_reference, '--allow-probable-reference'),
        ):
            if enabled:
                forwarded.append(option)
    elif args.mode == 'prime':
        append_option(forwarded, '--start', args.start)
        append_option(forwarded, '--stop', args.stop)
        append_option(forwarded, '--segment-size', args.segment_size)
        append_option(forwarded, '--save-every', args.save_every)
        for enabled, option in (
            (args.restart, '--restart'),
            (args.save_events, '--save-events'),
            (args.allow_module_change, '--allow-module-change'),
        ):
            if enabled:
                forwarded.append(option)
    else:
        append_option(forwarded, '--ring', args.ring)
        append_option(forwarded, '--prime', args.prime)
        append_option(forwarded, '--c21', args.c21)
        append_option(forwarded, '--psps', args.psps)
        append_option(forwarded, '--top-pairs', args.top_pairs)
        append_option(forwarded, '--backend', args.backend)
        append_option(forwarded, '--static-format', args.static_format)
    if args.mode in ('file', 'ring', 'prime'):
        append_option(forwarded, '--brpt-module', automatic_brpt_module())
        append_option(forwarded, '--workers', args.workers)
        append_option(forwarded, '--chunksize', args.chunksize)
        append_option(forwarded, '--progress-every', args.progress_every)
    append_option(forwarded, '--output-dir', args.output_dir)
    return forwarded


def main(argv: Sequence[str] | None = None) -> None:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    args.mode = args.mode.lower()
    if args.mode == 'primes':
        args.mode = 'prime'
    validate_args(parser, args)
    if args.mode in ('file', 'ring', 'prime'):
        module_path = automatic_brpt_module()
        if not module_path.is_file():
            parser.error(f'automatic BRPT module not found: {module_path}')
        workers, chunksize = resolve_parallelism(args)
        args.workers = workers
        args.chunksize = chunksize
        print(
            f'[Parallel configuration] workers={workers} '
            f'(all logical CPUs) chunksize={chunksize} (automatic)',
            flush=True,
        )
    forwarded = build_forwarded_args(args)
    sys.argv = [f'{sys.argv[0]} --mode {args.mode}', *forwarded]
    {'file': file_main, 'ring': ring_main, 'prime': prime_main, 'plot': plot_main}[args.mode]()

if __name__ == '__main__':
    mp.freeze_support()
    main()