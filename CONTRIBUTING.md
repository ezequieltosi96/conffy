# Contributing to Conffy

Every pull request is welcome, and every one will be reviewed. Bug fixes,
docs, new providers and new languages all help conferences become more
accessible.

## Workflow

1. **Branch from `dev`:** `feature/<short-name>` for features,
   `fix/<short-name>` for fixes. `main` only receives releases from `dev`.
2. **Open the pull request against `dev`.** Keep it small and focused on one
   thing, and explain what it changes and how you tested it.
3. **For a large change**, open an issue first, so we can agree on the
   approach before you write the code.

## Guidelines

- `uv run pytest` must pass. Add tests for new behavior; the tests use fakes,
  never real models.
- Code, comments, docs and commit messages are in English. Commits follow
  [Conventional Commits](https://www.conventionalcommits.org/) (`feat:`,
  `fix:`, `docs:`…).
- If you change `src/conffy/contracts.py`, update `docs/CONTRACTS.md` in the
  same pull request.
- A new AI provider is one adapter in `src/conffy/providers/`; see
  `docs/MODELS.md`.
- Never commit secrets, `.env` files or model files.

For bugs or ideas, open an issue.