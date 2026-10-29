# Issue #399 contract ambiguities

- The exact strings in `FORBIDDEN_PATHS` are left to the implementation. The
  tests require representatives for the four contracted categories by
  substring (repository checkout, `.env`, lab auth, and kube configuration).
- The complete set of read-only GET paths accepted by the proxy is not listed
  in the seam. The tests pin the paths explicitly named by the contract and
  the two denied examples (`/api/v1/metadata` and `/api/v1/rules`); additional
  proxy-approved GET paths remain implementation-defined.
- `IMAGE_ENV_NAMES` is implementation-defined. Tests derive the caller-provided
  names from the merged `container.run_args` contract and do not prescribe the
  base image/runtime inventory.
- The ignored temporary location and probe command's internal observation
  mechanics are not part of the observable contract; the live test checks only
  the documented command and evidence shape.
