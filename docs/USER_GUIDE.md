# User guide

## What this does, and what it deliberately does not

It reads brokerage exports, checks their arithmetic, and keeps your portfolio
state over time. It shows you where your money is, how it is placed for tax, and
how far it has drifted from your own plan.

It does **not** trade, move money, or tell you what to buy. It does not fill
gaps with assumptions: where a figure is missing, it says so rather than
estimating one.

## Importing a document

```bash
portfolio import ~/Downloads/positions.csv
```

Or drag a file onto the **Imports** page.

A few behaviours worth knowing:

- **Re-importing the same file does nothing.** Deduplication is by content, not
  filename, so an accidental double import cannot create a second snapshot.
- **The original file is kept**, under `data/sources/`. That is the only way to
  re-derive a reading later if the extraction improves or a number is disputed.
- **Accounts reporting no value at all are skipped** — and named, so the skip is
  never silent. They come back automatically if a later export gives them a
  value.
- **Nothing is discarded on failure.** An import whose arithmetic does not
  reconcile is kept and marked for review, showing the document's own numbers
  against what they add up to.

By default extraction uses the `mock` provider, which parses CSVs
deterministically and costs nothing. Set `EXTRACTION_PROVIDER=anthropic` and an
`ANTHROPIC_API_KEY` to use Claude, which is needed for PDFs.

## Reading the dashboard

The headline total always appears with the caveats that qualify it. Take them
seriously — they are the difference between a number and a number you can act on.

| Caveat | What it means |
|---|---|
| **stale value** | Value in accounts whose as-of date is unknown, because another institution is reporting them second-hand. This can be badly out of date. |
| **no confirmed tax treatment** | Accounts you have not classified. They are excluded from tax analysis rather than assumed to be taxable. |
| **no reported cost basis** | Gains cannot be computed for this share of the portfolio. The gain figure covers the rest. |
| **incomplete** | An account named in the source document was not extracted. The remaining numbers may all reconcile and still describe a partial portfolio. |

## Account setup

After the first import, some accounts will show **needs confirming**. Confirm
their type and tax treatment on the **Accounts** page.

This is not busywork. Tax treatment drives every placement figure, so an
unconfirmed account is left out of them rather than guessed at. A wrong guess
would quietly distort the analysis while looking complete. Names like
`Corporation` or an opaque plan code genuinely do not say what the tax treatment
is, so the system asks rather than inventing an answer.

Your choice is recorded in an audit log, and a later import will not overwrite
it.

## When two sources describe the same account

If one institution aggregates accounts held elsewhere, and you also import a
statement from the institution that actually holds them, the direct source wins
for those accounts and supplies them **wholesale** — not by patching prices onto
the aggregated holdings.

That is deliberate. Two sources can disagree about what an account *contains*,
not merely what it is worth, and refreshing only the overlap produces a portfolio
of mixed vintage that looks current. When neither source is obviously better —
two direct sources, or two aggregations — you are asked once, and the answer is
remembered.

## Investment policy

A policy is your own statement of intent: target weights, drift bands, and
constraints. Nothing in the app recommends targets for you.

```bash
portfolio policy starter > policy.yaml   # a commented template
$EDITOR policy.yaml
portfolio policy set policy.yaml
portfolio policy check
```

Or edit it on the **Policy** page. It is YAML so your comments and reasoning are
kept verbatim, and saving creates a new version rather than replacing the old
one — so "what was my policy when I made that decision?" stays answerable.

Two labels on the assessment mean different things:

- **out of policy** — a weight is outside its band. A fact about the portfolio.
- **provisional** — the underlying data is too uncertain to act on without
  checking. A portfolio can be perfectly in band and still provisional.

The "to rebalance" figures are arithmetic. They ignore tax, trading costs, and
whether the money should move at all.

## The recommendation journal

What was suggested and what you decided. Nothing here has been acted on.

```bash
portfolio journal list
portfolio journal decide <id> deferred --note "revisit after the stale data is fixed"
portfolio journal decide <id> accepted --note "done"
portfolio journal stats
```

**Deferring is not rejecting.** A deferred item stays open — setting something
aside is not being finished with it, and an analyst may reasonably raise it
again. Decisions append, so a recommendation deferred in March and accepted in
June keeps both facts.

The acceptance rate counts only accepted and rejected. It is blank when nothing
has been decided, rather than a rate computed from no decisions.

## Importing two documents as one portfolio

**Pass both files to one command and there is nothing to know:**

```bash
portfolio import ~/Downloads/aggregation.csv ~/Downloads/direct.pdf
```

The first document filed sets the date and the rest join it, so no date is
typed. The order is chosen for you too, from what the documents turn out to
contain rather than from the order you listed them -- see below for why that
matters. The rest of this section is what happens when you import them one at a
time, which still works and is worth understanding.

A document whose date already has a snapshot **joins** it rather than starting a
competing one, and supplies the accounts it wins precedence for. Import the
aggregation first and the direct source second:

```bash
portfolio import ~/Downloads/aggregation.csv
portfolio import ~/Downloads/direct.pdf
```

The two rarely carry the same date. A brokerage aggregation is downloaded when
you remember to; a holdings page is printed when you happen to be looking at it.
Left alone, documents a day apart open **two snapshots**, and the portfolio then
shows whichever was imported last — each internally consistent, and neither the
whole picture.

`--as-of` files a document on a chosen date instead of the one it states:

```bash
portfolio import ~/Downloads/direct.pdf --as-of 2026-08-26
```

The document's own date is never lost. The import reports it:

```
! the document states an as-of date of 2026-08-25, but it was placed on
  2026-08-26 at your instruction -- 1 day(s) apart
```

**The move is bounded.** Filing two documents together is legitimate; filing a
month-old statement as today is rewriting history, and no arithmetic can tell
the difference — a stale statement reconciles perfectly against itself. Beyond
`SNAPSHOT_DATE_TOLERANCE_DAYS` (default 5) the import is refused and nothing is
written, including the record of the file, so the retry is not mistaken for a
duplicate:

```
Refused: the document states 2026-08-28 but you asked to place it on 2026-06-01,
88 days apart -- more than the 5-day limit
```

Pass `--force-as-of` when the distance really is intended.

### Order matters, which is why the command chooses it

A joining document may only supply accounts the snapshot **already has**.
Accounts it reports that the portfolio does not cover are listed as not added,
and they are not added. So the widest document has to be filed first.

Measured on the fixture pair, both ways round:

| Order | Accounts | Total |
|---|---|---|
| aggregation, then direct source | 10 | $2,048,082.32 |
| direct source, then aggregation | 2 | $1,120,774.93 |

The losing order costs eight accounts and nearly half the value. It is not
silent -- the import lists every account it did not add -- but it is a line of
output against a portfolio that otherwise looks fine.

Passing both files at once removes the trap: the documents are read first, and
the one covering the most accounts is filed first whatever order you typed. When
that changes the order, the import says so:

```
filed widest first, so every account survives the join: aggregation.csv then direct.pdf
```

Importing one at a time, put the aggregation first yourself.

### Doing it one file at a time

A brokerage aggregation states the day you downloaded it; a holdings page
printed from the web tends to state the day before. So give the second document
`--as-of`, naming the date the first one landed on:

```bash
portfolio import ~/Downloads/aggregation_20260903.csv --provider anthropic
portfolio import ~/Downloads/direct_20260903.pdf --provider anthropic --as-of 2026-09-03
```

Forgetting is not silent. A snapshot that opens within the `--as-of` tolerance
of a live one says so, names that date, and names the flag:

```
! this document opened its own 2026-09-02 snapshot, but a snapshot for
  2026-09-03 is already live, 1 day(s) away. If the two documents describe one
  portfolio, re-import this one with --as-of 2026-09-03 ...
```

### Putting it right afterwards

Imports deduplicate by content hash, so importing the same file again with
`--as-of` does nothing until the first attempt is unfiled:

```bash
portfolio snapshots                          # find the stray one
portfolio snapshots remove 2026-09-02        # or an id, or an id prefix
portfolio import ~/Downloads/direct_20260903.pdf --as-of 2026-09-03
```

`remove` prints what it is about to do and asks before doing it. It names the
documents that become importable again, keeps any that another snapshot also
uses, and **never touches accounts, securities, or the preserved documents
themselves** — the database records what a document was read to say, and the
file is the only way to re-derive that later, so removal drops the record and
leaves the bytes on disk.

It refuses, rather than deciding for you, when the snapshot is cited by the
recommendation journal (those entries survive but lose the portfolio they were
written against) or when it superseded an earlier snapshot (which becomes live
again). `--force` proceeds anyway; `--yes` skips the prompt.

## Command reference

Every command carries its own help: `portfolio --help` lists them, and
`portfolio <command> --help` explains what one does, what it refuses to do,
and what each option means.

```
portfolio import <file>... [--provider mock|anthropic] [--as-of YYYY-MM-DD] [--force-as-of]
portfolio summary
portfolio accounts
portfolio holdings [--account NAME] [--limit N]
portfolio snapshots
portfolio snapshots remove <id|date> [--yes] [--force]

portfolio policy starter | set <file> | show | check
portfolio journal list [--status ...] | decide <id> <decision> | stats
portfolio extraction benchmark [--provider ...] [--verbose]
portfolio backup create [dest] | inspect <archive> | restore <archive> --yes
portfolio hermes install [--apply]
portfolio version
```

## Backups

The database and the stored documents are **one backup**. The database records
what each document was read to say; the documents are the only way to re-derive
that later. An archive always contains both, and a restore refuses one missing
either.

`pg_dump` and `psql` have to be on your PATH. They ship with the Postgres client,
not with the server container:

```bash
brew install libpq && brew link --force libpq     # macOS
```

### Taking one

```bash
portfolio backup create                  # writes backups/portfolio-backup-<stamp>.tar.gz
portfolio backup create ~/Documents      # or any directory
portfolio backup create /tmp/today.tar.gz  # or an exact filename
```

A destination with no extension is treated as a directory and gets a timestamped
name inside it. Archives are mode 0600 and `backups/` is gitignored, because an
archive contains every original statement — the same data as `test/` and `data/`,
in one convenient file.

**Copy them off this machine.** A backup that lives on the disk it is protecting
is not a backup. Anywhere else will do.

### Looking inside one

```bash
portfolio backup inspect backups/portfolio-backup-….tar.gz
```

Read-only, and it changes nothing:

```
archive   backups/portfolio-backup-20260904T052026Z.tar.gz
created   2026-09-04T05:20:26.167598+00:00
database  portfolio
documents 6 (2.4 MB)
format    version 1 (app 0.1.0)
```

### Restoring

```bash
portfolio backup restore backups/portfolio-backup-….tar.gz --yes
```

**Destructive and not selective.** The database and the whole of `data/sources`
are replaced by the archive's. Anything imported since the backup was taken is
gone. There is no merge and no way to restore one snapshot out of an archive.

Without `--yes` it prints what it would replace and changes nothing, so a dry
run is always available.

The order that avoids regret:

```bash
portfolio backup create ~/before-restore          # 1. keep what you are about to replace
portfolio backup inspect <archive>                # 2. confirm it is the archive you meant
launchctl bootout gui/$(id -u)/dev.portfolio-manager.api   # 3. stop the API, if installed
portfolio backup restore <archive> --yes          # 4. restore
make service-install                              # 5. start it again
portfolio summary                                 # 6. check the portfolio came back
```

Step 1 is the one people skip. Restoring last month's archive to undo one bad
import destroys every good import since, and there is nothing to undo it with.

Step 3 matters because the restore drops and recreates tables (`pg_dump --clean`)
while the API holds an open connection pool. A live server during a restore risks
a half-applied one.

Restore also refuses an archive written by a newer version of the app than you
are running, stops at the first SQL error rather than pressing on, and rejects
any archive member whose path escapes the destination — a restore is exactly the
moment someone might hand you a file from elsewhere.

## Optional: connecting Hermes

```bash
portfolio hermes install          # prints the commands; --apply installs the skill
```

Hermes gets read-only access plus the ability to record a recommendation, your
decision about one, and a view or preference you ask it to remember. It cannot
change holdings. Every reading it receives
carries the same staleness and completeness figures you see. The app works
identically with Hermes absent.

## The advisor

Needs Hermes set up as above, plus its api_server. `portfolio hermes install`
prints the steps; `portfolio advisor status` checks them.

**Tell it who you are first.** On the Advisor page, under *Philosophy &
context*, write your investment philosophy: goals, horizon, risk tolerance, and
what you never want to hold. Each save is a new version. Then add context:

- a **view**, e.g. "I expect rates to fall over the next year";
- a **preference**, e.g. "no individual bank stocks";
- a **page** (URL) or a **document** (PDF, .txt, .md) worth weighing.

Give a view an expiry date if it stops being true. After that date the advisor
asks whether it still holds. A PDF with no text layer is flagged rather than
guessed at. Anything the advisor records from a chat is marked *unconfirmed*
until you confirm it.

**Then ask.** Start a conversation and ask for a rebalance, or attach a page or
a PDF from the chat box. The advisor researches current news, proposes trades
per account (including securities you do not hold yet), and tests them with the
app's own engine. Push back and it revises. Use *Branch* to try a different
direction without losing this one.

**The Current proposal panel shows the app's figures, not the model's.** It
re-runs the latest trades against your current portfolio and policy and shows:
trades by account; the allocation now, after and against target; where sale
proceeds sit as uninvested cash; estimated gains with how much of the sale had
a cost basis; and every caveat. A sale with no cost basis shows "No cost basis",
never a zero gain.

**Keeping it.** *Save to journal* records the proposal with those figures, and
the Journal links back to the conversation. If the advisor proposes different
targets, applying them means editing your policy yourself. Nothing the advisor
does places a trade.

