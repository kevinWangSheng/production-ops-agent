# Git Workflow

## Commit Convention

- Format: `{type}: {description} [#{feature-id}]`.
- Types: `feat`, `fix`, `refactor`, `test`, `docs`, `chore`.
- Example: `feat: persist normalized incident events [#F2]`.

## Commit Rules

- Keep one logical change per commit.
- Every commit must leave the project runnable or explicitly specification-only.
- Stage specific files; do not use broad staging that could include secrets or unrelated work.
- Do not rewrite history, force-push, publish a repository, open a PR, or create a release without explicit authorization.

## Branch Strategy

- Keep `main` stable.
- Use `feature/{feature-id}-{short-name}` for non-trivial implementation work.
- Merge only after approved acceptance checks pass.
