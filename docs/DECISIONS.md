# Design decisions

Short records of the choices that shaped the system: the situation, what was decided, and what it costs.
Earlier modelling decisions (one record for customers and suppliers, no stored totals, lines carry the cost centre...) are in [DATA_MODEL.md](DATA_MODEL.md#design-decisions).

## D1. The change history is written by a database trigger

- **Situation.** Finance data is corrected by hand, by scripts and, eventually, by an API. "Who changed this and when" has to hold for all of them.
- **Decision.** One generic trigger function (`audit.log_change`) is attached to the main tables and writes `audit.change_log`. The caller identifies itself with `SET LOCAL app.actor`; otherwise the database user is recorded.
- **Why not log from the loader.** A history written by the application misses everything that does not go through the application: a manual fix in `psql`, a new tool, a bug.
- **Cost.** A small overhead on every write, and the log stores full rows as JSON. At the scale of this project both are irrelevant.

## D2. An update that changes nothing writes no history

- The trigger compares the old and the new row (ignoring `creado_en` and `actualizado_en`) and returns without writing when they are equal.
- Without this, every re-run of a load would add a "change" per row and the history would be noise. It is also what lets the incremental load be idempotent end to end.

## D3. The change log is append-only

- A trigger raises an error on `UPDATE` and `DELETE` of `audit.change_log`.
- A trace that can be edited is not evidence. This stops accidents; it does not stop a superuser, and it is not presented as tamper-proof.

## D4. Incremental load by content hash

- **Situation.** The first version loaded whole files and skipped orders it already had, so a changed order (for example one that got paid) was never updated.
- **Decision.** Hash the canonical content of each order (header and lines) and store it in `audit.source_row`. New → insert, changed → update, same → skip.
- **Why a hash and not a timestamp watermark.** The sources are plain files with no reliable "last modified" per row. A hash needs no cooperation from the source and detects any change, including a corrected line amount.
- **Cost.** Reading the whole file every time (cheap here), and one extra table. Deletions in the source are not detected.

## D5. Quarantine invalid rows instead of failing the run

- **Decision.** A row that fails validation is stored in `audit.rejected_row` with every reason and the raw values, and the rest loads. Nothing is "fixed" silently.
- **Why.** One bad row out of 500 should not block 499 good ones, and the rejected row must be easy to find and correct. The reasons are data you can query, not lines in a log.

## D6. A reject-rate limit protects against a wrong source

- If more than `--max-reject-rate` (default 20 %) of the rows are rejected, the run is aborted and rolled back.
- Quarantine is right for the occasional bad row, but a file where a third of the rows are rejected is a wrong file (a changed format, the wrong period), and loading the remaining rows would hide that. The rejected rows are still stored so the cause can be seen.

## D7. One transaction per run, and safe re-runs, instead of checkpoints and retries

- **Decision.** The whole run is one transaction. If anything fails, nothing is saved. Because the load is idempotent and compares hashes, running it again after fixing the file is always safe.
- **Rejected alternatives.** Checkpoints to resume half-way, and retries with back-off. Checkpoints add a state machine to solve a problem that does not exist at this size (a full load takes about a second). Retries matter for network sources; these sources are files. Both can be added when the sources change.
- **Cost.** A very large load would pay for a restart from the beginning.

## D8. The run record uses its own connection

- `audit.load_run` is written on a separate autocommit connection.
- A dry-run (the default) and a failed run both roll the main transaction back. If the run record lived in that transaction it would disappear with it, and a failed run is the one that most needs to leave a trace.
- **Consequence.** A dry-run does leave a row in `audit.load_run` and in `audit.rejected_row`. The data itself is not saved.

## D9. The reconciliation accounts for every source row

- After loading, `new + changed + unchanged + rejected` must equal the rows in the file, and the sum of the lines in the database must equal the sum in the file for the orders processed.
- With incremental files the old check ("the database equals the file") no longer holds, because a file may carry only part of the data; this version checks what the run claims to have processed.

## D10. The API reads the existing views

- The API queries the same `pagos.v_*` views that the BI layer uses, so the numbers agree by construction, and the business rules live in one place (SQL) instead of two.
- **Cost.** The API is coupled to those views; a change to a view is a change to the API contract, and the API tests would catch it.

## D11. Read-only and no bank data are enforced by the database role

- **Decision.** The API connects as `api_reader`: `default_transaction_read_only`, SELECT-only grants, a 5-second statement timeout, no access to bank accounts or to the raw change log.
- **Why not rely on the code.** A bug, a new endpoint or a dependency can write or leak; a privilege the database never granted cannot. The tests check the role directly, not only through the API.
- **Detail that mattered.** The raw change log also stores the history of the bank-account table, so its JSON contains the CBU. The role cannot read it; the API reads two views (`audit.v_order_change_log`, `audit.v_rejected_row`) that leave the sensitive parts out.

## D12. Reports take an `as_of` date

- `payables/due` and `payables/aging` accept the reference day (default: today). The same question can be asked for any past day, results are reproducible, and the tests do not depend on the date they run.

## D13. Amounts are exact in the database and numbers in JSON

- Inside the API amounts are `Decimal`; in the JSON they are plain numbers with two decimals, because that is what a client expects and what the BI tools read.
- **Cost.** Most clients read a JSON number as a binary float, which is not exact arithmetic (0.1 cannot be stored exactly). Rounded to two decimals the value shown is right for amounts of this size, but a client must not add up thousands of them as floats and expect cents to agree. A system that needs exactness should send amounts as strings.

## D14. Tests build their own database

- Each integration test creates a throw-away PostgreSQL database from the repository's own SQL files and drops it afterwards.
- Slower than sharing one database (about 20 seconds for the whole suite) but a test can never fail because of what another test left behind, and the suite also proves the schema files work from scratch.
