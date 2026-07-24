"""
Shared guard sets for migration-coverage invariant tests.

When a sibling task in the same PR batch adds a new SQLModel table but its
migration file lands in a separate commit, temporarily add the table name here
to suppress the "column/index missing from migrations" assertion in
test_invariants.py.  Remove the entry once the migration file is merged.

The invariant test ``test_pending_guards_are_eventually_resolved`` asserts this
set is empty on every CI run, so stale entries are caught automatically.
"""

# Tables whose migrations are in-flight (added by a sibling task in the same PR
# batch).  Remove each entry once its migration file lands on the target branch.
PENDING_MIGRATION_TABLES: set[str] = set()
