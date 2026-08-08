# External datasets

The BRPT repository does not redistribute the large third-party datasets used
in the validation campaigns. Download them from their original repositories
and retain their original attribution and terms.

## C21 — Carmichael numbers up to 10^21

Reference:

> Pinch, R. (2024). *The Carmichael numbers up to 10^21* [Data set]. Zenodo.
> https://doi.org/10.5281/zenodo.13755236

Upstream file:

- `c21.gz`
- Zenodo record: https://zenodo.org/records/13755236
- MD5 published by Zenodo: `4dd51363fe05cbd54ba109aab86a66fd`

The dataset contains Carmichael numbers and is therefore used with the
expected BRPT classification `COMPOSITE`.

Example:

```bash
python3 brpt_test.py --mode FILE \
  --input data/c21.gz \
  --expected COMPOSITE \
  --output results_c21
```

## PSPS — base-2 Fermat pseudoprimes below 2^64

Reference site:

> *Tables of pseudoprimes and related data*. Computed by Jan Feitsma; arranged
> and edited by William Galway. Centre for Experimental and Constructive
> Mathematics (CECM), Simon Fraser University.
> https://www.cecm.sfu.ca/Pseudoprimes/

The CECM page states that the compressed files contain all base-2 Fermat
pseudoprimes below `2^64`.

Primary upstream file used by BRPT:

- upstream name: `psps-below-2-to-64.txt.bz2`
- optional local short name: `psps.bz2`
- source: https://www.cecm.sfu.ca/Pseudoprimes/

Related upstream files include:

- `factored-psps-below-2-to-64.txt.bz2`
- `annotated-psps-below-2-to-64.txt.bz2`

The pseudoprime dataset is used with the expected BRPT classification
`COMPOSITE`.

Example:

```bash
python3 brpt_test.py --mode FILE \
  --input data/psps.bz2 \
  --expected COMPOSITE \
  --output results_psps
```

## Reproducibility rule

Published results should record at least:

1. the exact BRPT Git commit and/or Zenodo software DOI;
2. the exact upstream dataset filename;
3. the upstream dataset DOI or source URL;
4. a cryptographic checksum when available;
5. the command line used for the campaign;
6. the Python and dependency versions;
7. the operating system and relevant hardware configuration.
