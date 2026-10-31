# Issue #395 acceptance ambiguities

- The contract names the install subcommands and environment surface, but does not prescribe exact README prose or whether the walkthrough uses `bash deploy/compose/install.sh` versus executing the script directly. The tests therefore inspect fenced shell commands and stable identifiers, not sentence wording.
- “Required env names” is interpreted as the installer's `SETTINGS`, `OPSPILOT_SECRETS_DIR`, the model key, and the Observer credential alternatives documented by `UPGRADE.md`. If the implementation intentionally documents a smaller operator surface, the contract should name that set explicitly.
- “Open the workbench” has no exact command in the supplied evidence. The test accepts `workbench` or `工作台` after the agent upgrade; the contract should specify whether a URL, `install.sh compose`, or a browser action is required.
- The M0 stale wording is tested using the exact approval phrases present on `main` (`.env.example` lines 1–2 and 17). If only part of those sentences is obsolete, the contract should identify the retained wording.
