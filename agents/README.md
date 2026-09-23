# CoordPoison Agent Contracts

The files in this directory describe the contracts used by the target, attack,
and judge agents. They are reference prompts for the automation pipeline, not
standalone benchmark data.

The runtime flow is:

1. Target Agent discovers benign pair behavior in Stage A.
2. Target Agent normalizes the observed relation in Stage B.
3. Attack Agent assigns payloads in Stage C and constructs variants in D1/D4.
4. Target Agent executes variants in D2/D5 without judging them.
5. Judge Agent evaluates runtime evidence in D3/D6.
6. Failed coordinated attempts return to the D4 revision contract.

Runtime paths are resolved by `automation.paths.FrameworkPaths`. Generated
benchmark data belongs under `benchmarks/` and is created by the runner when a
user supplies clean packs and task definitions.
