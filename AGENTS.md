# Project workflow

- Commit completed, verified, in-scope changes automatically as work proceeds.
- Keep commits focused and use descriptive commit messages.
- Never stage or commit unrelated or pre-existing changes.
- Treat this repository as the primary implementation repository. Treat `jesse-src` and Memory as integration/reference repositories; do not modify or commit them unless a narrowly scoped change is proven necessary.
- Never read or print credential values.
- Never commit runtime databases, Docker volumes, `.env` files, private strategy source, or generated backtest artifacts.
