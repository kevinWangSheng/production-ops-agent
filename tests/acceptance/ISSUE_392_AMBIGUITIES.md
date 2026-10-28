# Issue #392 contract ambiguities

These points are intentionally not made stricter by the acceptance tests.

1. The contract does not name the workbench login URL, authentication scheme,
   generated username/password source, or how an operator retrieves the
   plaintext credential corresponding to `OPSPILOT_UI_USERS`. The opt-in E2E
   gate therefore proves installation, migration completion, loopback
   reachability, and the public HTTP boundary; a credentialed login assertion
   needs that operator-facing contract.
2. Secret filenames, Compose secret target paths, and the exact mechanism that
   imports secret-file contents into environment variables are open. Static
   tests require separate file-backed web secrets and reject literal
   credential values, without naming those implementation details.
3. `worker` and `observer` may be declared in this Compose file but are
   exercised by #393; `retention` is an image role in the image contract but
   has no required Compose service in #392. Tests require only the four
   services fixed by the ticket and validate optional product services when
   present.
4. The contract fixes the installer subcommands and state semantics, but not
   shell syntax, progress output, the exact confirmation prompt, or the
   command used to represent a one-shot service. Tests check observable
   subcommands, purge gating, file-mode intent, and migration/runtime phase
   boundaries rather than those spellings.
5. The release workflow filename, action versions, and Dockerfile stage names
   are not part of the public seam. Tests inspect the required behavior (tag
   trigger, permissions, pinned action SHAs, multi-architecture publication,
   and non-root digest-pinned image) without requiring those names.
