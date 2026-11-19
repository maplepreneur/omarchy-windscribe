# Contributing

Thanks for using Windscribe. Feedback is very welcome, **exact code is not.**

## This repository has one managed author, on purpose

This widget handles a VPN. Security and privacy is high concern.
It runs unsandboxed inside your Omarchy shell and it opens the terminal you
type your Windscribe password into. Additionally, AI allows for users to pump
out code at lightning speed, while being very difficult to review and audit.
For that reason, every line in it is written and reviewed by one person,
and only that person can commit:

- There are no unknown collaborators.
- **Pull requests are not merged.** They are closed as a matter of policy.
  Suggestions are given credit, but exact code is not merged. This is a security
  decision, not a judgement on your code: open-source projects now receive a
  steady stream of AI-generated pull requests, some of them plausible-looking
  supply-chain attempts, and the only review process that reliably keeps that
  out of a VPN widget is one where a single accountable person writes every
  line. If a PR describes a real bug or a good idea, it will be re-filed as
  an issue and credited to you when it ships.
- GitHub Actions are disabled on this repository, so nothing runs on a PR.

## What is welcome

**Open an issue** for anything:

- **Bugs**: what you did, what you expected, what happened. The output of
  `omarchy shell io.github.maplepreneur.omarchy-windscribe debug` (it
  contains no account details) and your Omarchy version help a lot.
- **Feature requests**: what you're trying to do, not just the feature.
- **Design / UX feedback**: screenshots welcome. If something felt confusing,
  that's a bug.
- **Docs**: if the README didn't answer your question, say what you looked
  for.

If you've found a **security problem**: please don't open a public issue,
see [SECURITY.md](SECURITY.md).

## Testing your report

The widget's own state, in one command:

```bash
omarchy shell io.github.maplepreneur.omarchy-windscribe debug
```

It deliberately contains no account details — the CLI never prints the
account name, so the panel only ever knows usage and plan.

The plugin's parsers have a test suite; if your bug is about a misread
status or location list, this reproduces it without touching the panel:

```bash
node tests/model-test.mjs
```
