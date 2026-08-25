---
name: find-mcp
description: "Find candidate MCP servers and assess their production trust boundary."
disable-model-invocation: true
argument-hint: "[service or capability]"
---

# Find MCP

1. Search current primary sources for MCP servers matching the requested capability.
2. Report source, maintenance, authentication, data egress, tool mutability, approval behavior, deployment, and installation requirements.
3. Map the server to a read adapter, action adapter, or rejected out-of-scope capability.
4. Do not install, transmit secrets, or grant production credentials without explicit approval.
