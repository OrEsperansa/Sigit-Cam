# Repository Guidelines

## Project Structure & Module Organization

Application code lives in `app/`. `main.py` defines the FastAPI routes and lifecycle, `ffmpeg.py` manages capture and replay processing, `auth.py` implements password sessions, `backup.py` handles per-file backup copies, and `config.py` loads environment settings. Browser assets are split between `app/templates/` and `app/static/`. Tests live in `tests/` and mirror the main subsystems. Runtime recordings belong under `data/`; bundled Windows FFmpeg binaries belong under `ffmpeg/`. Neither directory should be committed.

## Build, Test, and Development Commands

- `python -m pip install -r requirements.txt` installs Python dependencies.
- `python -m uvicorn app.main:app --host 0.0.0.0 --port 8000` runs Sigit Live locally.
- `python -m unittest discover -s tests -v` runs all unit and mocked FFmpeg integration tests.
- `python -m compileall -q app tests` performs a quick syntax check.
- `docker build -t sigit-live:local .` builds the Linux container image.

Copy `.env.example` to `.env` before running. A usable `APP_PASSWORD` and a `SESSION_SECRET` of at least 32 characters are required.

## Coding Style & Naming Conventions

Use four-space indentation and Python type hints. Follow standard Python naming: `snake_case` for functions and variables, `PascalCase` for classes, and uppercase names for constants. Keep route handlers small and move capture, authentication, or backup logic into their dedicated modules. JavaScript uses `camelCase`, `const` by default, and semicolons. No formatter or linter is currently enforced; match the surrounding code and run `git diff --check` before committing.

## Testing Guidelines

Tests use the standard-library `unittest` framework. Name files `test_*.py`, classes `*Tests`, and methods `test_<behavior>`. Add regression tests for FFmpeg command changes, error classification, authentication behavior, and filesystem copies. Use temporary directories and mocked subprocesses where practical; keep hardware-specific tests optional.

## Commit & Pull Request Guidelines

Recent commits use short, imperative subjects such as `Fix intermittent audio and add microphone meter`. Keep each commit focused and avoid mixing generated media or secrets with source changes. Pull requests should explain the behavior change, list verification commands, identify configuration changes, and include screenshots for visible UI updates. Link relevant issues when available.

## Security & Configuration Tips

Never commit `.env`, credentials, camera URLs, recorded clips, or share paths. Preserve HTTP-only session cookies, validate redirect targets, and pass secrets to containers at runtime rather than baking them into images.
