# Releasing

`main` is what `omarchy plugin add` gives new users. It only moves at
release time.

## Branches

| Branch | Purpose |
| --- | --- |
| `main` | Released code. Every commit here is a tested snapshot. Force-push and deletion are disabled. |
| `staging` | Integration. Every fix and feature merges here first and is run on a real Omarchy machine. |
| `feat/…`, `fix/…` | One change each, branched from `staging`, merged back with `--no-ff`. |

## Day to day

```bash
git checkout staging && git pull
git checkout -b fix/whatever
# … work, test in the bar (omarchy restart shell reloads the plugin) …
git checkout staging && git merge --no-ff fix/whatever && git push
```

Running the plugin from `staging` locally is fine, the installed folder is a
git checkout, so `git checkout staging` in
`~/.config/omarchy/plugins/io.github.maplepreneur.omarchy-windscribe`
switches it.

## Cutting a release

1. On `staging`: bump `version` in `manifest.json`, update the README if
   behaviour changed, run `omarchy plugin validate .`, run the parser
   tests (`node tests/model-test.mjs`), test once more in the bar.
2. Merge to `main` and tag:
   ```bash
   git checkout main && git merge --no-ff staging && git push
   git tag -a vX.Y.Z -m "Windscribe X.Y.Z" && git push --tags
   gh release create vX.Y.Z --title "Windscribe X.Y.Z" --notes-file <notes>
   ```

Users on `main` get the new code as soon as it's pushed, via
`omarchy plugin update`. So merge to `main` only what you would ship.

## Commits

Author and committer are the repository owner only. No co-author trailers.
