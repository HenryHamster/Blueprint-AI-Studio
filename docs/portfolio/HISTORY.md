# History preservation and branch cleanup

Keep the project's development history and release tags. A presentable repository can retain the work that led to the current system while having a much smaller set of active branch references.

The original `main` contains **8,495 commits** before the presentation cleanup. The clean checkout preserves those commits and the cleanup commits, including their original IDs, authors, dates, messages, and merge relationships. It has one active `main` branch and **49 original release tags**.

## Useful points in the existing history

| Point | Original reference | Date recorded in Git |
| --- | --- | --- |
| Initial repository | `972697d16` | March 12, 2026 |
| Early end-to-end deployment and operator UI | `00f78de3b` | March 30, 2026 |
| First tagged promotion | `v1` | May 23, 2026 |
| Later platform release | `v21` | July 3, 2026 |
| Latest existing release tag | `v53` | August 28, 2026 |
| Latest original main commit | `4d83283bd` | September 1, 2026 |

These are reading entry points taken from the existing history. Commit messages and tags describe what was recorded at the time; they do not substitute for current verification.

## Prepared artifacts

The development checkout and GitHub history are unchanged by export. The original refs and complete reachable history are backed up in ignored `.scratch/portfolio/original-history.bundle`. Git verified the bundle.

The history-preserving checkout is generated into `.scratch/portfolio/clean-checkout/`. Its final commit and tracked tree match the selected source revision exactly. It imports the selected revision's ancestry and reachable release tags, with no remotes, abandoned task branch references, local configuration, environment files, or build outputs.

The previous single-commit exports were superseded and their temporary checkout folders removed. Their source revisions remain in development history; the current export preserves that history.

Superseded plans, handoffs, exploratory notes, and one-off reports are also removed from the current tree. Their contents remain in earlier commits. Use `git log -- <path>` to locate a record and `git show <commit>:<path>` to read it.

## Reproduce

From a clean, committed source checkout:

```bash
# Preserve all existing refs privately; use a new filename for a new backup
git bundle create .scratch/portfolio/original-history.bundle --all
git bundle verify .scratch/portfolio/original-history.bundle

# The destination must not already exist
python3 scripts/export-portfolio.py .scratch/portfolio/clean-checkout
```

The exporter copies original Git objects and checks out the selected revision. It preserves merge ancestry and annotated tag objects. It refuses a dirty source or existing destination. To export a different committed revision and its ancestry, pass `--ref`.

The [export tests](../../scripts/test_export_portfolio.py) check original commit metadata and graph, tracked content and modes, tag preservation, abandoned-ref exclusion, revision selection, and protection of other work.

## GitHub branch cleanup

At the October 9, 2026 audit, GitHub had 1,520 branches, 49 tags, and no open PRs. Branch count and commit count are separate: deleting a merged task branch reference does not delete the commits already reachable from `main`.

A cleanup of the existing GitHub repository should identify merged task branches, preserve integration and release branches still used by automation, and retain unresolved work. The local export does not delete any remote branches or change GitHub PRs, tags, default-branch history, or visibility.

Any selective squashing should be prepared separately for review, with an original-to-curated commit map and identical final source tree. The current checkout keeps the original commits.
