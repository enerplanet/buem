# BUEM-EnerPlanET contract

The BUEM-EnerPlanET request/response contract is owned by
`enerplanet/buem-gateway` (`schemas/`, `docs/versioning.md`). The files in
this folder are a pinned copy of **API contract v6-draft** (`contract.txt`
names the exact source: repo, tag or commit SHA, and path). Do not edit them
here. Re-sync from buem-gateway to update:

```bash
REF=ce5d6db51928718c5bd3fb26d7567abc233be998   # a tag or a commit SHA on buem-gateway
git -C ../buem-gateway show $REF:schemas/v6-draft/request_schema.json   > request_schema.json
git -C ../buem-gateway show $REF:schemas/v6-draft/response_schema.json  > response_schema.json
git -C ../buem-gateway show $REF:schemas/v6-draft/example_request.json  > example_request.json
git -C ../buem-gateway show $REF:schemas/v6-draft/example_response.json > example_response.json
```

then update `contract.txt` (`tag=` takes a tag or a commit SHA) and re-add
each schema file's `"$comment"` field naming the new source.

`schema_manager.py` loads `request_schema.json` / `response_schema.json`
from this folder directly (no version subdirectories; there is exactly one
live contract). `geojson_validator.py` validates incoming requests against
`request_schema.json` with `jsonschema`; it does not re-define the
contract's structure.

CI fails if this folder drifts from `contract.txt`'s pinned source (see
`.github/workflows/ci.yml`'s "Contract drift" step).

To propose a contract change, open a PR against buem-gateway's
`schemas/v6-draft/`, not against these files.
