# Contributing to HiveMind

Thanks for your interest — it genuinely means a lot.

## How this project is run (please read first)

HiveMind is built and maintained by **one person** as personal infrastructure that
happens to be useful to others. The source is open so you can **read it, audit it
(important for a tool that holds your memory locally), fork it, and self-host it**.

To keep it sustainable, the development model is intentionally lightweight:

- **Questions, ideas, "does it do X?"** → open a [Discussion](https://github.com/projectmentor/hive-mind/discussions), not an Issue.
- **Bugs** → an Issue is welcome, but responses are **best-effort, no SLA**. A clear
  repro helps a lot.
- **Pull requests** → welcome for focused bug fixes. For anything larger, **open a
  Discussion first** so we can agree on the approach before you spend time. Sweeping
  refactors or new subsystems will likely be declined to keep the project coherent.

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

## License

By contributing, you agree your contributions are licensed under the
[GNU AGPL v3.0](LICENSE), subject to the [CLA](CLA.md).
