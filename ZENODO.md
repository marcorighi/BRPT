# GitHub–Zenodo release procedure

This repository is prepared for archiving through Zenodo once a stable version
is ready for citation.

## Before the first release

1. Create the GitHub repository, preferably named `brpt`.
2. Make it public when the authors are ready to publish the source.
3. Add both authors/collaborators with the intended repository permissions.
4. Push this repository to the `main` branch.
5. Connect the GitHub account/repository to Zenodo and enable archiving for the
   repository.
6. Check `CITATION.cff`, `README.md`, `AUTHORS.md`, and the dataset references.
7. Ensure publication-grade tests use a clean Git commit.

## First archived release

When the software version used by the article is frozen:

```bash
git status
git tag -a v1.0.0 -m "BRPT v1.0.0"
git push origin v1.0.0
```

Then create the corresponding GitHub Release for tag `v1.0.0`. With the
repository enabled in Zenodo's GitHub integration, archive that release in
Zenodo.

Zenodo will assign a DOI to the archived software version. Record that exact
version DOI in the article and add it to `CITATION.cff`/`README.md` in the next
commit as appropriate.

Zenodo also maintains a concept DOI for the software record across versions.
For computational reproducibility, a paper should cite the DOI of the specific
software version actually used.

## Later releases

Use a new immutable version tag for every published change, for example:

```text
v1.0.1
v1.1.0
v2.0.0
```

Do not overwrite an already archived release.
