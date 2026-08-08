# BRPT

[![DOI](https://zenodo.org/badge/1327508963.svg)](https://doi.org/10.5281/zenodo.21848362)

**Current archived release:** v1.0.1 — DOI: [10.5281/zenodo.21848363](https://doi.org/10.5281/zenodo.21848363)
**BRPT** is a reference implementation and validation suite for a cubic
Frobenius probable-primality test developed by **Marco Righi** and
**Michele Baldi**.

The project is research software for computational number theory. It contains
both the primality-test engine and tooling for reproducible validation against
large external datasets and independently generated prime/composite
references.

> Development status: research software under active development. A number
> reported as `PRIME` by BRPT has passed the implemented probable-primality
> conditions; this repository does not describe that output as a general
> mathematical primality certificate.

## Repository contents

- `brpt.py` — core BRPT engine and single-number command-line interface.
- `brpt_test.py` — validation campaigns, checkpointing, comparison modes and
  diagnostic plots.
- `docs/brpt_command.txt` — single-number command reference.
- `docs/brpt_test_command.txt` — complete test-suite command reference.
- `DATASETS.md` — provenance and use of the external C21 and PSPS datasets.
- `REPRODUCIBILITY.md` — recommended procedure for reproducible experiments.
- `CITATION.cff` — machine-readable software citation metadata.
- `LICENSE` — MIT license.

## Core algorithm interface

The Python API is:

```python
from brpt import brpt

print(brpt(17))   # True
print(brpt(21))   # False
```

For command-line use:

```bash
python3 brpt.py 17
python3 brpt.py "2**127 - 1"
python3 brpt.py "pow(2, 521) - 1"
```

The command prints `PRIME` when the input passes BRPT and `COMPOSITE`
otherwise.

`brpt.py` uses only the Python standard library.

## Validation suite

The unified launcher supports four principal modes:

```text
FILE   test integers from a plain, .gz or .bz2 dataset
RING   scan integers congruent to 1 or 5 modulo 6
PRIME  generate exact primes and test them with BRPT
PLOT   generate diagnostic plots from completed reports
```

Show the complete command reference with:

```bash
python3 brpt_test.py --help
```

### C21 Carmichael campaign

```bash
python3 brpt_test.py --mode FILE \
  --input data/c21.gz \
  --expected COMPOSITE \
  --output results_c21
```

### Base-2 Fermat pseudoprime campaign

```bash
python3 brpt_test.py --mode FILE \
  --input data/psps.bz2 \
  --expected COMPOSITE \
  --output results_psps
```

### Ring scan

```bash
python3 brpt_test.py --mode RING \
  --start 1 \
  --count 100000 \
  --output results_ring
```

### Exact-prime validation

```bash
python3 brpt_test.py --mode PRIME \
  --start 2 \
  --stop 1000000 \
  --output results_primes
```

### Diagnostic plots

```bash
python3 brpt_test.py --mode PLOT \
  --ring results_ring/state_ring.json \
  --prime results_primes/state_prime_scan.json \
  --c21 results_c21/summary_c21.json \
  --psps results_psps/summary_psps.json \
  --output results_plots
```

## Optional dependencies

The core engine has no external dependencies. Some `brpt_test.py` modes use
optional packages:

- `sympy` for comparison in RING mode;
- `matplotlib` for static plots;
- `plotly` for interactive plots.

Install them when needed:

```bash
python3 -m pip install -r requirements-optional.txt
```

## Publish the repository from Linux

If GitHub CLI (`gh`) is installed and authenticated, the repository can be
created and pushed directly from this directory:

```bash
./scripts/create_github_repo.sh YOUR_GITHUB_ACCOUNT/brpt
```

The script deliberately refuses to overwrite an existing GitHub repository.
If you create an empty repository manually on GitHub instead, use:

```bash
./scripts/init_git_repo.sh git@github.com:YOUR_GITHUB_ACCOUNT/brpt.git
```

Both scripts use your existing Git identity and credentials; they do not store
passwords, tokens or personal email addresses in the repository.

## External validation datasets

Large third-party datasets are deliberately excluded from Git. See
[`DATASETS.md`](DATASETS.md) for provenance and download information.

The two principal external datasets currently used are:

1. Richard Pinch, *The Carmichael numbers up to 10^21*, Zenodo,
   DOI: https://doi.org/10.5281/zenodo.13755236 (`c21.gz`).
2. Jan Feitsma / William Galway, *Tables of pseudoprimes and related data*,
   CECM, containing all base-2 Fermat pseudoprimes below `2^64`.

A convenience downloader is provided in `scripts/fetch_datasets.sh`. The PSPS
file is very large; inspect the script before downloading.

## Tests

Run the dependency-free smoke tests with:

```bash
python3 -m unittest discover -s tests -v
```

The smoke suite includes ordinary primes/composites and known base-2
pseudoprime/Carmichael inputs. These tests check basic software regressions;
they are not a substitute for the large validation campaigns used in the
research.

## Citation and DOI

The project uses `CITATION.cff`. After the first archived GitHub release is
published through Zenodo, add the release DOI to the citation metadata and to
this README.

For results reported in a scientific article, cite the **specific Zenodo DOI
of the BRPT version used in the computations**, not merely the moving GitHub
`main` branch.

See [`ZENODO.md`](ZENODO.md) for the release procedure.

## License

BRPT is released under the [MIT License](LICENSE).

The MIT license permits reuse, modification and redistribution provided that
the copyright and license notice are retained. Scientific citation is
requested through `CITATION.cff` and should accompany scholarly use of the
software.

Copyright (c) 2026 Marco Righi and Michele Baldi.
