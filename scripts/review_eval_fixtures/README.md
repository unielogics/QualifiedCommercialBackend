# Review eval fixtures (local only)

`scripts/snapshot_review_fixture.py` copies a live file's per-file analyses,
requested documents and AI context out of the database. That is the client's
own data: names, addresses, statements, tax-return figures. The repository and
the `qcbackend` images are public, so snapshots stay on the machine that made
them: `*.json` in this folder is ignored by git.

To run the eval, snapshot the files you need here first (inside the backend
container or an environment with database access), then run
`python scripts/review_eval.py`. Never commit a snapshot; if one is needed in a
shared test, write a made-up file with the same shape instead.
