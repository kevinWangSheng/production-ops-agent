# Issue #413 contract ambiguities

- The contract fixes the lab host ports and loopback binding, but does not specify whether `compose.lab.yaml` should use service-level `ports` entries or YAML extension anchors. The tests check observable published ports and loopback binding only.
- The contract names the fixed metric families and label keys but does not prescribe HELP/TYPE line ordering or whether zero-valued families must be emitted. The tests check names, required bounded labels, and forbidden run/incident labels.
- The contract says install/upgrade must not add `install.env` keys; it does not define a machine-readable schema beyond the existing installer `SETTINGS` list. The test compares the health-related key surface against that list.
- The contract fixes route paths but does not state whether a query string on an otherwise valid path must be rejected; the tests therefore do not require `/readyz?x=1` to return 404.
