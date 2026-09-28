"""The information_schema catalog model shared by the DDL detection tests.

helper_ prefix: not a test, and ignored by Antithesis by the same convention as
under test/.
"""


class Catalog:
    """A model of the three information_schema views ddl.py reads.

    Applying the emitted statements here is what makes the test a detection
    test: an invalid statement is recorded rather than silently absorbed, the
    way a real server would record it as an errno.
    """

    def __init__(self, tables):
        self.cols = {t: {"sid", "v", "pref"} for t in tables}
        self.idx = {t: {"PRIMARY"} for t in tables}
        # Schema-wide, keyed by constraint name, because that is how MySQL 8
        # scopes foreign key names. Modelling them per-table would reproduce
        # the exact mistake the code under test used to make, and the model
        # would then agree with the bug instead of catching it.
        self.fks = {}          # constraint name -> table it is on
        self.invalid = []      # statements issued against state that rejects them
        self.applied = []      # every statement the generator emitted
        self.classified = 0    # statements the model actually understood
        self.drops = 0

    def _create(self, bag, table, obj, stmt):
        self.classified += 1
        if obj in bag.get(table, set()):
            self.invalid.append(stmt)
        else:
            bag[table].add(obj)

    def _drop(self, bag, table, obj, stmt):
        self.classified += 1
        if obj not in bag.get(table, set()):
            self.invalid.append(stmt)
        else:
            bag[table].discard(obj)
            self.drops += 1

    def _fk_add(self, table, name, stmt):
        self.classified += 1
        if name in self.fks:
            self.invalid.append(stmt)     # ER_FK_DUP_NAME, schema-wide
        else:
            self.fks[name] = table

    def _fk_drop(self, table, name, stmt):
        self.classified += 1
        if self.fks.get(name) != table:
            self.invalid.append(stmt)
        else:
            del self.fks[name]
            self.drops += 1

    def _rename(self, pairs, stmt):
        """Move every object with the table name, the way the server does.

        This is what makes the "read the catalog fresh, never cache it"
        rationale testable rather than merely asserted: a swap issued by one
        shape relocates the indexes, columns and constraints another shape is
        about to reason about.
        """
        self.classified += 1
        for src, dst in pairs:
            if src not in self.cols:
                self.invalid.append(stmt)
                return
            self.cols[dst] = self.cols.pop(src)
            self.idx[dst] = self.idx.pop(src)
            for n, t in list(self.fks.items()):
                if t == src:
                    self.fks[n] = dst

    def apply(self, stmt):
        self.applied.append(stmt)
        parts = [w.strip(",;") for w in stmt.replace("`", "").split()]
        after = lambda w: parts[parts.index(w) + 1].split(".")[-1]  # noqa: E731
        if stmt.startswith("CREATE INDEX"):
            self._create(self.idx, after("ON"), parts[2], stmt)
        elif stmt.startswith("DROP INDEX"):
            self._drop(self.idx, after("ON"), parts[2], stmt)
        elif "ADD COLUMN" in stmt:
            self._create(self.cols, after("TABLE"), after("COLUMN"), stmt)
        elif "DROP COLUMN" in stmt:
            self._drop(self.cols, after("TABLE"), after("COLUMN"), stmt)
        elif "ADD CONSTRAINT" in stmt:
            self._fk_add(after("TABLE"), after("CONSTRAINT"), stmt)
        elif "DROP FOREIGN KEY" in stmt:
            self._fk_drop(after("TABLE"), parts[-1], stmt)
        elif stmt.startswith("RENAME TABLE"):
            names = [w.split(".")[-1] for w in parts[2:] if w != "TO"]
            self._rename(list(zip(names[0::2], names[1::2])), stmt)
        # Anything else leaves `classified` behind `applied`, which a check
        # below asserts cannot happen.


class JR:
    def __init__(self):
        self.tallies = []
        self._id = 0

    def ddl_start(self, node, stmt):
        self._id += 1
        return self._id

    def ddl_end(self, *a, **k):
        pass

    def tally(self, shape, errno):
        self.tallies.append((shape, errno))

    def concurrent_drivers(self):
        return 2
