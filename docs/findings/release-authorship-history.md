# Release authorship history

<!-- Generated from scripts/release_authorship_report_contract.json; contract_sha256=87bd0966aa301875988e5b11db276412bbc1f88f168e28e108dcfcedcba400ae -->

This table is a deterministic reading of annotated release tags and their immutable merge-parent topology. `non-bot` counts commit authors whose identity is not the Blueprint bot; provenance is accepted only from the documented bot identity, an accepted `Co-authored-by` trailer, or recursive attribution of merged side-parent history.

| Release | Base SHA | Candidate/tag SHA | Tag object SHA | Total | Non-bot | Bot identity | Co-authored-by | Merged parent | Unattributed | Zero unattributed | Force-promote history |
|---|---|---|---|---:|---:|---:|---:|---:|---:|:---:|---|
| v37 | `0830ebb9e56fdeba7f16c2cacef2ff8816bbfdf2` | `8e080d2b7b5df08bf52b5510900c2e5e1357bd93` | `f34fd895ccfe096481955f642c0e1678409950b2` | 3 | 1 | 2 | 1 | 0 | 0 | yes | unrecoverable |
| v38 | `8e080d2b7b5df08bf52b5510900c2e5e1357bd93` | `d869f0cce702c10566b246ca4170101ba8fb2796` | `d4dcaeb0fe6c605d1e24e7918aba9a039c4dab89` | 18 | 6 | 12 | 6 | 0 | 0 | yes | unrecoverable |
| v42 | `4ac0aec933396bd5220b84e466ec5d68b65b3964` | `ad3228ce3c0a5ae0d2e52aed1ec792a09cef54eb` | `0b4dbf00416c8c442e38a2f273f46a52a013e6f5` | 15 | 13 | 2 | 13 | 0 | 0 | yes | unrecoverable |
| v45 | `0c45d166455ad675affede99d7ee1d0cb4e7f9b0` | `354f08269a1ae64487949cedb184757d45cd5c51` | `6882f06869d8fec82335beb833a39ff4efd58a31` | 149 | 118 | 31 | 90 | 28 | 0 | yes | unrecoverable |

The zero-unattributed conclusion is the contract field shown in the table, not an inference from a blank cell. Any nonzero unattributed count is listed by SHA in the JSON contract and makes verification fail until the evidence is resolved.

Per-version release-mechanic force-promote actions are historically unrecoverable: old release records retained only a mutable `last_qualification` slot, so missing historical evidence is reported as `unrecoverable`, never as zero.
