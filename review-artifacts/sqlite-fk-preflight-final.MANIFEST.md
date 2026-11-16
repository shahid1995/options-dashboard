# SQLite foreign-key preflight — final review diff (published in parts)

The reviewed artifact is `artifacts/sqlite-fk-preflight-final.diff`: the working-tree diff from `dd1d06ec34985e471ed05a21f38afe3d83090e54` (66,879 bytes, 1,351 lines).

The publishing channel could not carry that file in a single request, so it is published here as four byte-exact parts. Concatenating them in the order below reproduces the artifact exactly.

| # | file | bytes | lines | git blob SHA-1 |
|---|------|-------|-------|----------------|
| 1 | sqlite-fk-preflight-final.diff.part1-of-4 | 24988 | 338 | 29b96479f8a5e79e3691aebfeb0f2be5b6bfd78c |
| 2 | sqlite-fk-preflight-final.diff.part2-of-4 | 14777 | 338 | b61062dbe32100f09d383c87643e6d7bceca6609 |
| 3 | sqlite-fk-preflight-final.diff.part3-of-4 | 14055 | 338 | 80d525b35261eebd23084eb6406283dfa8f714e2 |
| 4 | sqlite-fk-preflight-final.diff.part4-of-4 | 13059 | 337 | d778e9dec7dd45d9000cb862b582b8a2de956166 |

Concatenation order: 1, 2, 3, 4 — no separators, no added or removed bytes.

Reproduce:

```sh
cat sqlite-fk-preflight-final.diff.part1-of-4 \
    sqlite-fk-preflight-final.diff.part2-of-4 \
    sqlite-fk-preflight-final.diff.part3-of-4 \
    sqlite-fk-preflight-final.diff.part4-of-4 \
    > sqlite-fk-preflight-final.diff
```

Verify:

```sh
wc -c      # 66879
wc -l      # 1351
sha256sum  # 017ee60081ea6725b479ddfa555b78a310946d652b46225d34d2c3ae4c98c3df
```

Encoding: UTF-8, LF line endings, final byte `0x0a`. The parts are sequential line ranges of the original file: 1-338, 339-676, 677-1014, 1015-1351.

This branch contains only these review artifacts: it does not carry the six implementation, test or documentation files that the diff describes, and no pull request was created.
