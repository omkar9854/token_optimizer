# Releasing slimctx to PyPI

The publish workflow uses **PyPI trusted publishing** (OIDC) — no API
tokens are created or stored anywhere. One-time setup, then every release
is two commands.

## One-time setup (~5 minutes, done by the repo owner)

1. Create a PyPI account at <https://pypi.org/account/register/> (enable
   2FA — required for new projects).
2. Go to <https://pypi.org/manage/account/publishing/> → **Add a new
   pending publisher** and enter exactly:
   - PyPI project name: `slimctx`
   - Owner: `omkar9854`
   - Repository name: `token_optimizer`
   - Workflow name: `publish.yml`
   - Environment name: `pypi`
3. In GitHub: repo **Settings → Environments → New environment** named
   `pypi` (no secrets needed — the OIDC handshake is the credential).

## Every release

```bash
# 1. bump `version` in pyproject.toml and slimctx/__init__.py, commit, push
# 2. tag and publish a GitHub release — this triggers the workflow
git tag v0.1.0
git push origin v0.1.0
gh release create v0.1.0 --title "v0.1.0" --generate-notes
```

The workflow runs the test suite, builds sdist + wheel, and publishes.
Verify with `pip install slimctx` a minute later.

## After the first successful publish

Add the PyPI badge to README.md:

```markdown
[![PyPI](https://img.shields.io/pypi/v/slimctx.svg)](https://pypi.org/project/slimctx/)
```
