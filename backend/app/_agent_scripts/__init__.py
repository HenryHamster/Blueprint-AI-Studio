"""Scripts baked into the VM startup process.

These files are READ from disk (not imported) and embedded into the
generated startup script as base64 blobs. Keeping them as real .py
files means:
  - syntax + style checked by every linter
  - directly testable (run them in pytest with a fake manifest)
  - no f-string escape mangling (the compute.py startup_script f-string
    only substitutes the base64 string, not raw code)

Any time you're tempted to embed a multi-line heredoc of a different
language inside a Python f-string, add a file here instead. See the
task 0bcfd031 / 23889608 / c94d6d9b post-mortem.
"""
