# Reproducibility guide

This document defines a minimal reproducibility protocol for BRPT validation
runs intended to support a scientific publication.

## 1. Record the software revision

Before every publication-grade campaign:

```bash
git status --short
git rev-parse HEAD
python3 --version
```

The working tree should ideally be clean. Record the full Git commit hash in
the experiment notes. For a released experiment, also record the Zenodo DOI
of the exact release.

## 2. Record dataset provenance

Do not treat local aliases such as `psps.bz2` as sufficient provenance.
Record the original filename, source URL/DOI and checksum when available.
See `DATASETS.md`.

For C21, verify the checksum published by Zenodo:

```bash
md5sum data/c21.gz
```

Expected MD5 for the Zenodo `c21.gz` file described in `DATASETS.md`:

```text
4dd51363fe05cbd54ba109aab86a66fd
```

## 3. Record the environment

For runs that use optional Python packages:

```bash
python3 -m pip freeze > environment-pip-freeze.txt
uname -a > environment-uname.txt
lscpu > environment-lscpu.txt 2>/dev/null || true
```

Keep these files with the experimental results or archive them with the data
supporting the paper.

## 4. Keep command lines

Save the exact shell command used for each campaign. Examples:

```bash
python3 brpt_test.py --mode FILE \
  --input data/c21.gz \
  --expected COMPOSITE \
  --output results_c21

python3 brpt_test.py --mode FILE \
  --input data/psps.bz2 \
  --expected COMPOSITE \
  --output results_psps

python3 brpt_test.py --mode RING \
  --start 1 \
  --count 100000 \
  --output results_ring

python3 brpt_test.py --mode PRIME \
  --start 2 \
  --stop 1000000 \
  --output results_primes
```

## 5. Preserve generated summaries

BRPT campaigns produce JSON/CSV checkpoints and summaries. Preserve the files
used to derive tables and figures in the paper. Do not rely only on terminal
output.

## 6. Generate figures from preserved results

Example:

```bash
python3 brpt_test.py --mode PLOT \
  --ring results_ring/state_ring.json \
  --prime results_primes/state_prime_scan.json \
  --c21 results_c21/summary_c21.json \
  --psps results_psps/summary_psps.json \
  --output results_plots
```

## 7. Release discipline

A publication release should correspond to an immutable Git tag, for example
`v1.0.0`. Do not change a tagged release after publication. Corrections should
use a new version and a new Zenodo version DOI.
