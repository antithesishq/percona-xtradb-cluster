"""Detection test for the DDL generator's catalog lookup.

ddl.py used to flip a coin between CREATE and DROP without knowing whether the
object was there. Roughly half of every index/column/FK statement was therefore
invalid on arrival -- ER_DUP_KEYNAME, ER_DUP_FIELDNAME, ER_FK_DUP_NAME,
ER_CANT_DROP_FIELD_OR_KEY -- and PXC replicates the failing statement anyway,
buying a cluster-wide inconsistency vote for each one. property-catalog.md's
un-injected-vote rule counts those votes as divergence evidence, so the noise
destroyed the signal the terminal checksum oracle depends on.

This is a detection test, not a smoke test: it runs the generator against a
model of the server catalog, applies every statement the generator emits to
that model, and fails if any statement was invalid against the state it was
issued into. The last case proves the detector works by feeding it the old
coin-flip generator, which it must reject.
"""
import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parent))
import helper_stubs

helper_stubs.install()

from antithesis import assertions as A  # noqa: E402
from pxcwl import config, ddl, schema  # noqa: E402
from helper_ddl_model import JR, Catalog  # noqa: E402

DROP_CLAIM = "a data-definition statement dropped an object the catalog reported present"

results = []


def check(name, ok, note=""):
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}{('  -- ' + note) if note else ''}")


class Cursor:
    def __init__(self, cat, raise_on_lookup=False):
        self.cat, self.raise_on_lookup, self.rows = cat, raise_on_lookup, []

    def __enter__(self):
        return self

    def __exit__(self, *a):
        return False

    def execute(self, sql, params=()):
        low = sql.lower()
        if "information_schema" in low:
            if self.raise_on_lookup:
                raise RuntimeError("catalog unreadable")
            if "information_schema.columns" in low:
                self.rows = [(c,) for c in sorted(self.cat.cols.get(params[1], ()))]
            elif "information_schema.statistics" in low:
                self.rows = [(i,) for i in sorted(self.cat.idx.get(params[1], ()))]
            else:
                # table_constraints: one row per constraint in the SCHEMA.
                self.rows = sorted(self.cat.fks.items())
            return
        if low.startswith("set session"):
            return
        if low.startswith("select @@session.wsrep_sync_wait"):
            self.rows = [(0,)]
            return
        self.cat.apply(sql)

    def fetchall(self):
        return self.rows


class Conn:
    def __init__(self, cat, raise_on_lookup=False):
        self.cat, self.raise_on_lookup = cat, raise_on_lookup

    def cursor(self, *a, **k):
        return Cursor(self.cat, self.raise_on_lookup)


class S:
    name = "node1"

    def __init__(self, conn):
        self.conn = conn


TABLES = config.SCRATCH_TABLES + ["wl_fk_parent", "wl_fk_child"]
# The three shapes that choose a direction, plus rename_swap -- which chooses
# nothing, but relocates every index, column and constraint to another table
# name and so is the thing the "read the catalog fresh, never cache it"
# rationale exists for. It is excluded from the gating scenarios below because
# it issues no lookup and therefore has nothing to gate.
GATED = [ddl.ddl_index, ddl.ddl_column, ddl.ddl_online_fk]
SHAPES = GATED + [ddl.ddl_rename_swap]


def drive(n=600, raise_on_lookup=False, tables=None, shapes=None):
    A.FIRED.clear()
    cat = Catalog(tables if tables is not None else TABLES)
    jr, s = JR(), S(Conn(cat, raise_on_lookup))
    from pxcwl import rnd
    for _ in range(n):
        rnd.choice(shapes or SHAPES)(jr, s, {})
    return cat, jr


# ---------------------------------------------------------------------------
cat, jr = drive()
check(
    "the generator never emits a statement invalid against the live catalog",
    not cat.invalid,
    f"{len(cat.invalid)} invalid of {len(cat.applied)} emitted"
    + (f"; first: {cat.invalid[0]}" if cat.invalid else ""),
)
check(
    "and it is actually emitting work, not skipping everything",
    len(cat.applied) > 400,
    f"{len(cat.applied)} statements",
)
# _run_ddl catches everything and db.errno_of returns None for a non-MySQL
# exception, so a model that threw on every statement would tally exactly like
# a model that accepted every statement -- and "0 invalid" would mean nothing.
# Counting what the model actually CLASSIFIED is the guard that cannot be
# fooled that way.
check(
    "the model understood every statement it was given",
    cat.classified == len(cat.applied),
    f"classified {cat.classified} of {len(cat.applied)}",
)
bad = [t for t in jr.tallies if t[1] is not None or ":" in t[0]]
check(
    "no statement failed, so the run above really was clean",
    not bad,
    f"{len(bad)} failures, e.g. {bad[:1]}",
)
fk_stmts = [x for x in cat.applied if "FOREIGN KEY" in x or "DROP FOREIGN KEY" in x]
check(
    "the foreign-key shape really ran, so its zero-invalid result is not vacuous",
    len(fk_stmts) > 50,
    f"{len(fk_stmts)} FK statements, 0 invalid",
)
check(
    "both directions are exercised, so this is not a create-only generator",
    cat.drops > 50 and len(cat.applied) - cat.drops > 50,
    f"{cat.drops} drops, {len(cat.applied) - cat.drops} creates",
)
check(
    "the drop reach claim fires",
    sum(1 for r in A.FIRED if r["message"] == DROP_CLAIM) > 0,
    f"{sum(1 for r in A.FIRED if r['message'] == DROP_CLAIM)} hits",
)

# ---------------------------------------------------------------------------
cat, jr = drive(n=60, raise_on_lookup=True, shapes=GATED)
check(
    "an unreadable catalog emits nothing rather than guessing",
    not cat.applied,
    f"{len(cat.applied)} statements",
)
check(
    "and the skipped lookups are tallied for triage",
    all(sh.endswith(":lookup_failed") for sh, _ in jr.tallies) and len(jr.tallies) == 60,
    f"{len(jr.tallies)} tallies",
)

# ---------------------------------------------------------------------------
cat, jr = drive(n=60, tables=[], shapes=GATED)   # every table absent
check(
    "a table that is not there right now is left alone",
    not cat.applied and not cat.invalid,
    f"{len(cat.applied)} statements",
)
check(
    "and the skip is tallied, not silent -- a vanished table must not just "
    "quietly zero out three of the six DDL shapes",
    len(jr.tallies) == 60 and all(t[0].endswith(":table_absent") for t in jr.tallies),
    f"{len(jr.tallies)} tallies, e.g. {jr.tallies[:1]}",
)

# ---------------------------------------------------------------------------
# Detection check: the old coin-flip generator must be REJECTED by the model
# above. Without this, a test that only ever sees the fixed generator cannot
# tell "no invalid statements" from "the model does not notice invalid ones".
from pxcwl import rnd  # noqa: E402

old_cat = Catalog(TABLES)
old_s = S(Conn(old_cat))
for _ in range(300):
    table = rnd.choice(config.SCRATCH_TABLES)
    idx = f"ix_swarm_{rnd.randint(0, 3)}"
    stmt = (
        f"CREATE INDEX `{idx}` ON `{config.SCHEMA}`.`{table}` (`v`)"
        if rnd.chance(0.5)
        else f"DROP INDEX `{idx}` ON `{config.SCHEMA}`.`{table}`"
    )
    old_cat.apply(stmt)
# The same discipline for the foreign-key scope bug specifically. MySQL scopes
# FK constraint names per SCHEMA; a per-TABLE lookup (which is what the first
# cut of this fix shipped) therefore reports a live name as absent whenever it
# sits on another table, and emits an ADD that fails ER_FK_DUP_NAME 1826. That
# is worse than the coin flip it replaced: once a name is taken anywhere, three
# of the four table draws for it are doomed rather than half.
fk_cat = Catalog(TABLES)
for _ in range(400):
    child = rnd.choice(config.SCRATCH_TABLES)
    fk = f"fk_swarm_{rnd.randint(0, 3)}"
    per_table_says_present = fk_cat.fks.get(fk) == child     # the buggy lookup
    fk_cat.apply(
        f"ALTER TABLE `{config.SCHEMA}`.`{child}` DROP FOREIGN KEY `{fk}`"
        if per_table_says_present
        else f"ALTER TABLE `{config.SCHEMA}`.`{child}` ADD CONSTRAINT `{fk}` "
        f"FOREIGN KEY (pref) REFERENCES `{config.SCHEMA}`.`wl_fk_parent` (pid) "
        "ON DELETE CASCADE ON UPDATE CASCADE, ALGORITHM=INPLACE"
    )
check(
    "the model rejects a per-table foreign-key lookup (proves it detects)",
    len(fk_cat.invalid) > 100,
    f"{len(fk_cat.invalid)} of {len(fk_cat.applied)} invalid",
)

check(
    "the model rejects the old coin-flip generator (proves it detects)",
    len(old_cat.invalid) > 50,
    f"{len(old_cat.invalid)} of {len(old_cat.applied)} invalid",
)

print()
print("ALL PASS" if all(results) else "SOME FAILED")
sys.exit(0 if all(results) else 1)
