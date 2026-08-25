# KDA authoritative-report import template

`report-ref.json` must contain exactly `ledger_path` and a one-based
`ledger_row`. The importer validates the authoritative source marker, kernel,
benchmark revision, scoring schema, workload count, correctness/status fields,
and recomputed all/large/small geomeans before copying the bounded ledger row
and per-workload receipt into the run.

KDA speedup-only rows remain valid judge evidence but are not frontier eligible:
the generic Kernel Infra frontier requires candidate and baseline absolute
timings for every task workload. Replace the placeholders and workload list
with the frozen KDA task before use.
