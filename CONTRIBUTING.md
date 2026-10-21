# Contributing to Stickle

Thank you for helping. Right now the most useful help is trying Stickle on your own
computer and telling us what goes wrong: see [Help test](README.md#help-test). For a
security problem, follow [SECURITY.md](SECURITY.md) instead of opening an issue.

## Ideas and bugs

[Open an issue](https://github.com/linkro-app/Stickle/issues/new), or use **Report a
problem or idea…** in the tray menu. One topic per issue makes it easier to follow.

Some things Stickle will not do, whatever the version, so that it stays what it is:

- no Stickle server, accounts, analytics or telemetry; the app talks only to the storage
  you choose and to the daily update check you can turn off;
- no locked or paid features, ads or bundled offers;
- no recurring pop-ups or banners asking you for anything, donations included;
- nothing extra to install: every library and font ships with the app;
- no settings in the registry or other system-specific stores: everything is in files.

## Translations

The app is translated into English and Korean for now. Other languages will be taken
through a translation platform before 1.0, so please do not send translation files for
them yet; an issue saying you would like to translate is welcome.

## Code

Please open an issue before a larger change, so that we can agree on the approach first.

With [uv](https://docs.astral.sh/uv/) installed:

```
uv sync
uv run pre-commit install
uv run python -m stickle
```

Every commit has to pass the same checks as CI, which `pre-commit` runs for you:

```
uv run ruff check && uv run ruff format --check
uv run pyright
uv run pytest
```

- Keep one change per pull request, with tests for what it changes.
- `core`, `data`, `crypto` and `sync` do not import Qt; code for one operating system goes
  under `platform/`.
- Every string the app shows goes through Qt's translation functions, in English, with the
  Korean translation in `i18n/stickle_ko.ts` if you can (`scripts/update_translations.py`).
- Commit messages follow [Conventional Commits](https://www.conventionalcommits.org)
  (`feat:`, `fix:`, `docs:`, …).

## License

Stickle is licensed under [GPL-3.0-or-later](LICENSE). By contributing, you agree that
your contribution is licensed the same way. There is no contributor agreement to sign.
