# Contributing

BRPT is research software. Changes that may affect mathematical behavior or
published experimental results should be reviewable and reproducible.

## Recommended workflow

1. Create a branch for the change.
2. Keep algorithmic changes separate from documentation-only changes where
   practical.
3. Run:

   ```bash
   python3 -m py_compile brpt.py brpt_test.py
   python3 -m unittest discover -s tests -v
   ```

4. For changes to `brpt.py`, repeat the relevant external validation campaigns
   before using the modified code in scientific results.
5. Record behavior-changing modifications in `CHANGELOG.md`.
6. Do not commit the large C21/PSPS source datasets or generated result trees.

## Scientific traceability

A commit used to generate a published table or figure should not be rewritten.
Publication releases should be immutable Git tags and should be archived with
Zenodo.
