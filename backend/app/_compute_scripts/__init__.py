"""Scripts baked into the GCP compute-job VM startup script as base64 blobs.

These files are READ from disk (not imported) and embedded into the generated
startup script as base64 blobs. Keeping them as real .py files means:
  - syntax + style checked by every linter
  - directly testable without a running GCP VM
  - no f-string escape mangling (compute_backends.py only substitutes the
    base64 string, not raw Python code)

Any time you're tempted to embed a multi-line heredoc of another language
inside the generated bash, add a file here instead. See CLAUDE.md's
'Generating Code for One Language From Another' section and the task
0bcfd031 / c94d6d9b post-mortem.
"""
