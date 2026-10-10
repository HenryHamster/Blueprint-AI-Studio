# History preservation and portfolio snapshot

The original repository accumulated a large development and release history. A curated portfolio checkout can present the final source with a single initial commit while preserving that history separately. This is a presentation boundary, not an attempt to reconstruct who wrote every line.

## Prepared artifacts

The cleanup branch retains its ancestry. During the cleanup, the original refs and their complete reachable history were saved to an ignored local bundle at `.scratch/portfolio/original-history.bundle`. Git verified that the bundle records a complete history.

The clean checkout is generated into `.scratch/portfolio/clean-checkout/`. It contains the committed source plus `docs/portfolio/SNAPSHOT.json`, which identifies the original source commit and tree. It has one initial commit, no remote, and no imported local environment files or build artifacts.

Local artifacts are intentionally absent from published source. Preserve the history bundle in durable private storage before discarding the original checkout. A Git bundle preserves committed objects and refs; it does not capture uncommitted files, local configuration, or arbitrary ignored artifacts.

## Reproduce

From a clean, committed source checkout:

```bash
# Preserve the original history privately
mkdir -p .scratch/portfolio
git bundle create .scratch/portfolio/original-history.bundle --all
git bundle verify .scratch/portfolio/original-history.bundle

# The destination must not already exist
python3 scripts/export-portfolio.py .scratch/portfolio/clean-checkout
```

The exporter reads the selected committed revision through `git archive`, initializes a new repository, and commits the exported files. It preserves tracked files even when `.gitignore` also lists them. It refuses a dirty source or existing destination and does not reset, rewrite, or push the source repository. To select another committed revision, pass `--ref`.

The backup and clean checkout have different purposes. The bundle preserves the original development history; the clean checkout provides a concise source presentation. The [export tests](../../scripts/test_export_portfolio.py) exercise provenance, tracked-but-ignored files, ignored configuration exclusion, revision selection, and preservation of other work.

## GitHub publication decision

The local cleanup does not replace the published default branch or release tags. For a portfolio, a separate repository with the curated checkout is the least disruptive publication path. It also keeps original PRs, release provenance, and operational references available in the development repository.

Replacing an existing default branch's history would affect clones, open PRs, release tags, and automation. Make that choice only after deciding which repository remains operational and where the history archive will live. A clean commit history also does not settle the current proprietary license's public visibility or reuse terms; those remain the copyright holder's decision.
