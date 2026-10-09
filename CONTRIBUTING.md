# Contributing to HiveMind

Thanks for your interest — it genuinely means a lot.

## How this project is run (please read first)

If several AI helpers will work on a project, read
[docs/WORKING_TOGETHER.md](docs/WORKING_TOGETHER.md) first.
It is the job split we use here: one plans, one builds, one checks.

HiveMind is built and maintained by **one person** as personal infrastructure that
happens to be useful to others. The source is open so you can **read it, audit it
(important for a tool that holds your memory locally), fork it, and self-host it**.

To keep it sustainable, the development model is intentionally lightweight:

- **Questions, ideas, "does it do X?"** → open a [Discussion](https://github.com/projectmentor/hive-mind/discussions), not an Issue.
- **Bugs** → an Issue is welcome, but responses are **best-effort, no SLA**. A clear
  repro helps a lot.
- **Security vulnerabilities** → **don't** open an issue; report privately as described in
  [SECURITY.md](SECURITY.md).
- **Pull requests** → welcome for bug fixes, security fixes and documentation. The current
  contract is 2.4. New features are not added just because a pull request shows up; for anything
  beyond a focused fix, **open a Discussion first**. A change to the contract, to signing, to
  sync, or to the security promises waits until the maintainer says yes. Sweeping refactors will
  likely be declined to keep the project coherent.

None of this is meant to be cold — it's how a solo maintainer stays sane and keeps the
project alive. Open source here means the code is yours to use and learn from, not that
anyone is owed support.

## Contributor License Agreement

Because HiveMind is open source (AGPL v3.0) **and** keeps the option of a commercial
license open, every code contribution requires a one-time **CLA** so the Maintainer
holds the rights needed to relicense and enforce. It's automated:

1. Open your pull request.
2. A bot will comment asking you to sign.
3. Reply with: `I have read the CLA Document and I hereby sign the CLA`.

That's it — you sign once. Full text: [CLA.md](CLA.md).

## Ground rules

- Keep changes small and focused; match the surrounding style.
- The journal is the source of truth — writes go through core's append API.
- Tests must pass offline: `python3 -m pytest -q`.
- Be kind. This is a small project run by a human.

## CI

- **Ubuntu** (`ci.yml`) runs on every pull request and every push to `main`.
- **macOS** (`ci-macos.yml`) runs on every push to `main`. On a pull request it runs only when the diff
  touches a path that can affect macOS: `hv`, the sync daemon, the installer and launchd scripts, and the
  tests that stub them. The full list is in the workflow. A docs-only PR shows no macOS check.
- **Release branches:** a PR into `release/*` runs no macOS check. macOS runs on each push to the release
  branch, after the merge, without gating it, and a red run is fixed forward by an ordinary PR. The
  release PR into `main` runs macOS and waits for it.
- **Merge rule:** CI is green, **and the macOS jobs are green when they ran**. The macOS jobs are not
  required status checks, because a required check that the path filter skipped stays pending and blocks
  the merge.
- If a change outside those paths turns out to break macOS, `main` shows it. The fix is an ordinary PR;
  add the path to the filter if it should have run.

## License

By contributing, you agree your contributions are licensed under the
[GNU AGPL v3.0](LICENSE), subject to the [CLA](CLA.md).
