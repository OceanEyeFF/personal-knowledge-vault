# W4 semantic vector-index fixture

This directory contains a synthetic, empty, but fully loadable hnswlib index
bundle for the W4 installed-artifact semantic-search scenario.  It has no real
Vault content, credentials, or Provider response data.

The two required product pairs are complete:

- `doc_vectors.idx` + `doc_vectors_metadata.json`
- `chunk_vectors.idx` + `chunk_vectors_metadata.json`

Both indexes use hnswlib 0.8.0, cosine space, dimension 1536, zero elements,
and the production HNSW defaults (`M=16`, `ef_construction=200`, generation
capacity 10000).  hnswlib 0.8.0 serializes an empty index with a loaded
`max_elements` value of 0; the product load path detects this and expands the
in-memory index to 10000 before use.  The artifact test verifies both states.
Their metadata binds the published default Embedding contract:
`https://api.openai.com/v1`, `text-embedding-3-small`, dimension 1536.  This
lets an installed vector/hybrid search load the indexes before it reaches the
deliberately missing-credential Provider seam.

`manifest.v1.json` is the bundle authority.  Its `files` array inventories the
four pair files and records the exact SHA-256 of every file.  The manifest does
not hash itself.

## Reproduction

Generation is offline and fails unless the active environment provides exactly
`hnswlib==0.8.0`:

```powershell
.\scripts\run-test.ps1 -Direct `
  -DataRoot .data-test\w4-semantic-fixture-generate `
  -Command @(
    "python",
    "tests/fixtures/w4/semantic-vector-index.v1/generate_fixture.py",
    "--output",
    "tests/fixtures/w4/semantic-vector-index.v1"
  )
```

The artifact contract test regenerates the bundle twice in isolated temporary
directories, requires byte-identical outputs, compares them with the checked-in
bundle, verifies every manifest digest, and loads both `.idx` files through
hnswlib, including the production-equivalent resize.  A missing or wrong
hnswlib version is a hard test failure, never a skip.
