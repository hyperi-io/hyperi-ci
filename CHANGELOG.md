## [1.0.29](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.28...v1.0.29) (2026-03-09)


### Bug Fixes

* auto-exclude AI agent dirs and org submodules from sdist ([6f7210f](https://github.com/hyperi-io/hyperi-ci/commit/6f7210f422f3ccdec0e0a1396bf3dc944194dce2))

## [1.0.28](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.27...v1.0.28) (2026-03-09)


### Bug Fixes

* remove UV_EXTRA_INDEX_URL from publish install deps step ([74cd699](https://github.com/hyperi-io/hyperi-ci/commit/74cd699a4a38837eaee46826c80439b1980d4098))

## [1.0.27](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.26...v1.0.27) (2026-03-09)


### Bug Fixes

* remove UV_EXTRA_INDEX_URL from dep resolution steps ([5e68a45](https://github.com/hyperi-io/hyperi-ci/commit/5e68a4555a33874de887480c07e94a672e6876a3))

## [1.0.26](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.25...v1.0.26) (2026-03-09)


### Bug Fixes

* use unauthenticated JFrog URL for dep resolution ([1f642b3](https://github.com/hyperi-io/hyperi-ci/commit/1f642b32b719a9e65c00d99659d03145364185f1))

## [1.0.25](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.24...v1.0.25) (2026-03-09)


### Bug Fixes

* restore JFrog index per-step with token guard ([6a4a522](https://github.com/hyperi-io/hyperi-ci/commit/6a4a522da8216a7973ab2160f7f1e3610718b110))

## [1.0.24](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.23...v1.0.24) (2026-03-09)


### Bug Fixes

* use uvx for standalone quality tools and checkout submodules ([9948535](https://github.com/hyperi-io/hyperi-ci/commit/994853566ee7755b3e77658e97e324851bb42039))

## [1.0.23](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.22...v1.0.23) (2026-03-09)


### Bug Fixes

* pass pyproject.toml config to bandit ([d71f134](https://github.com/hyperi-io/hyperi-ci/commit/d71f1341f3428fa54440cb4d0248f98028ab931d))

## [1.0.22](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.21...v1.0.22) (2026-03-09)


### Bug Fixes

* remove UV_EXTRA_INDEX_URL from python-ci workflow ([b15f9f4](https://github.com/hyperi-io/hyperi-ci/commit/b15f9f43520f6830d2ebfe24315d178de03ebd44))

## [1.0.21](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.20...v1.0.21) (2026-03-09)


### Bug Fixes

* always install semantic-release plugins ([c2e2e8c](https://github.com/hyperi-io/hyperi-ci/commit/c2e2e8c0f2a4f2f2ca1c03c53eff9dca434e2c49))

## [1.0.20](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.19...v1.0.20) (2026-03-09)


### Bug Fixes

* package config in wheel, publish to PyPI ([1b536a4](https://github.com/hyperi-io/hyperi-ci/commit/1b536a47ae7a9e59c07cfcabc3830744f932b5b2))
* simplify PyPI publish to use token ([f5ff6f9](https://github.com/hyperi-io/hyperi-ci/commit/f5ff6f96f67bfc509456e558a3d13098c906931f))

## [1.0.19](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.18...v1.0.19) (2026-03-09)


### Bug Fixes

* add semgrep SAST to all language quality pipelines ([a7e9532](https://github.com/hyperi-io/hyperi-ci/commit/a7e95328a10b7ab1e246d51a1399ae71b958b7e4))
* run semgrep via uvx, add --refresh to all workflows ([9f13069](https://github.com/hyperi-io/hyperi-ci/commit/9f13069982138771af83e8421445fb5f3bb4cf2f))

## [1.0.18](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.17...v1.0.18) (2026-03-09)


### Bug Fixes

* add semgrep SAST to Python quality pipeline ([3522a01](https://github.com/hyperi-io/hyperi-ci/commit/3522a01205f32551fe890dae15d22490ea440721))

## [1.0.17](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.16...v1.0.17) (2026-03-09)


### Bug Fixes

* replace pyright with ty for Python type checking ([100fa5d](https://github.com/hyperi-io/hyperi-ci/commit/100fa5d7d2f2a424c7e4e707182f85a3fffcd7d6))

## [1.0.16](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.15...v1.0.16) (2026-03-09)


### Bug Fixes

* add NODE_OPTIONS for pyright OOM in quality job ([250e7c3](https://github.com/hyperi-io/hyperi-ci/commit/250e7c368af6e18a25715bdf261a965afa1326cd))

## [1.0.15](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.14...v1.0.15) (2026-03-09)


### Bug Fixes

* resolve project tools via uv run when not on PATH ([1262da4](https://github.com/hyperi-io/hyperi-ci/commit/1262da441ade635f5adb45899d0286a574f948c0))

## [1.0.14](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.13...v1.0.14) (2026-03-09)


### Bug Fixes

* install all extras in python CI workflow ([2ce0422](https://github.com/hyperi-io/hyperi-ci/commit/2ce0422b709aa9198c87b3f84e7612013b6a6d47))

## [1.0.13](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.12...v1.0.13) (2026-03-09)


### Bug Fixes

* skip JFrog index for OSS projects and add publish-target to workflow ([82e046c](https://github.com/hyperi-io/hyperi-ci/commit/82e046c38f15ec57e487c9d3a56ef2e4d9edeb46))

## [1.0.12](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.11...v1.0.12) (2026-03-09)


### Bug Fixes

* migrate tool auto-fixes releaserc, license, and broken symlinks ([63153a0](https://github.com/hyperi-io/hyperi-ci/commit/63153a0b239346d9fe6fba9dcd3bb1a5472eb514))

## [1.0.11](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.10...v1.0.11) (2026-03-08)


### Bug Fixes

* add @semantic-release/exec to releaserc template ([dcdbae2](https://github.com/hyperi-io/hyperi-ci/commit/dcdbae2616b03483522859a61c519bcb7fd1d71e))

## [1.0.10](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.9...v1.0.10) (2026-03-08)


### Bug Fixes

* complete Go handlers and cross-language publish infra ([038167e](https://github.com/hyperi-io/hyperi-ci/commit/038167e3793ef2e1f08f872094adba526ff6b19e))

## [1.0.9](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.8...v1.0.9) (2026-03-08)


### Bug Fixes

* add post-build verification, binary packaging, and test threading ([a165b4f](https://github.com/hyperi-io/hyperi-ci/commit/a165b4fcafa1e31cd4d55cd2f33fe468d0e7ad20))

## [1.0.8](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.7...v1.0.8) (2026-03-07)


### Bug Fixes

* use .tmp/ for cross-sysroot instead of /tmp ([8d05365](https://github.com/hyperi-io/hyperi-ci/commit/8d053650e2b40c55986cab806e4188237840de67))

## [1.0.7](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.6...v1.0.7) (2026-03-07)


### Bug Fixes

* create g++ wrapper for cross-compilation sysroot ([ce1b8ab](https://github.com/hyperi-io/hyperi-ci/commit/ce1b8ab80fd9a42e63cad61f9198caf7f26061d5))

## [1.0.6](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.5...v1.0.6) (2026-03-07)


### Bug Fixes

* always update apt cache when cross sysroot needs packages ([c13cdfa](https://github.com/hyperi-io/hyperi-ci/commit/c13cdfa321ec7f43193ac68f094d976b32adbb65))

## [1.0.5](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.4...v1.0.5) (2026-03-07)


### Bug Fixes

* add sysroot include paths for cmake cross-compilation ([e9e8413](https://github.com/hyperi-io/hyperi-ci/commit/e9e841314df7d6f5a2477cb7420fc40b35c1ea96))

## [1.0.4](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.3...v1.0.4) (2026-03-07)


### Bug Fixes

* skip pre-installed tools on ARC runners ([a702704](https://github.com/hyperi-io/hyperi-ci/commit/a7027049928c5b14dc4829b80bd99bc9ebb0c2bc))
* wire workflows to use ARC pre-installed tools ([fb61a9f](https://github.com/hyperi-io/hyperi-ci/commit/fb61a9f018aa5a8534bbac74914c67527a50542e))

## [1.0.3](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.2...v1.0.3) (2026-03-06)


### Bug Fixes

* port cross-compilation sysroot from old CI ([770a1d2](https://github.com/hyperi-io/hyperi-ci/commit/770a1d27f5b1251ee777be715afde5d1a78b49af))

## [1.0.2](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.1...v1.0.2) (2026-03-06)


### Bug Fixes

* clear host linker flags for cross-compilation builds ([295e3af](https://github.com/hyperi-io/hyperi-ci/commit/295e3af9b031376d1b3af2b82cc270756f8a2b94))

## [1.0.1](https://github.com/hyperi-io/hyperi-ci/compare/v1.0.0...v1.0.1) (2026-03-06)


### Bug Fixes

* install rust cross-compilation targets before build ([2e083c4](https://github.com/hyperi-io/hyperi-ci/commit/2e083c47d896ab837a1a49a829a4196409b62dc7))

# 1.0.0 (2026-03-06)


### Bug Fixes

* add C/C++ deps to rust test project and cross-compile support ([025bb96](https://github.com/hyperi-io/hyperi-ci/commit/025bb96113baa9d870daeeacb173d8892e2f0a63))
* add CI tooling, publish pipelines, and self-hosting CI ([14693f1](https://github.com/hyperi-io/hyperi-ci/commit/14693f11558eacb8c4c8a522fe65f9b5ebf44332))
* add git credentials for private repo access in CI workflows ([48f2329](https://github.com/hyperi-io/hyperi-ci/commit/48f2329ae0f488b7d51bc10b0978670d4b117136))
* add hyperi-ai standards submodule ([9fe1685](https://github.com/hyperi-io/hyperi-ci/commit/9fe1685419076f5234c93e5f8289e1c27f77d5d9))
* make init existing-project-smart ([2aafae3](https://github.com/hyperi-io/hyperi-ci/commit/2aafae36d956dd1368d91fbc386bf226e8022cdc))
* pin hyperi-pylib to exact version 2.24.1 ([efbcff7](https://github.com/hyperi-io/hyperi-ci/commit/efbcff766ea8f4f051b9df8bf521168468c763cb))
* releaserc indent, optional cargo deny, uv cache ([74b63af](https://github.com/hyperi-io/hyperi-ci/commit/74b63af8edb92b9d7c30e65d8511de9ceccab92d))
* remove git credentials step (hyperi-ci is now public) ([1b24c63](https://github.com/hyperi-io/hyperi-ci/commit/1b24c63c3c78699adf01ea04002031ec7ac9d647))
* remove GITHUB_TOKEN from workflow_call secrets (reserved name) ([2c3f330](https://github.com/hyperi-io/hyperi-ci/commit/2c3f330ee648b09fc8f2380d45f4856d128bc609))
* remove JFrog index from HYPERCI_INSTALL ([f37a2c0](https://github.com/hyperi-io/hyperi-ci/commit/f37a2c0868718432a2fc3a935850aee3cfd6175f))
* ts quality handler tries common tsc script names ([fd9c628](https://github.com/hyperi-io/hyperi-ci/commit/fd9c628576874d3a5512fa625fd72efdb514f712))
* use archive URL to avoid submodule clone during install ([439b8ea](https://github.com/hyperi-io/hyperi-ci/commit/439b8ea759bae3687578075dd9d0fad0240c4614))
* use cross-repo token for private git access and update actions to latest ([b35f80d](https://github.com/hyperi-io/hyperi-ci/commit/b35f80d634bc3bf75128a2f03983ae0f81b316f8))
* use GIT_TOKEN secret (org-wide) for private repo access ([2f3a813](https://github.com/hyperi-io/hyperi-ci/commit/2f3a8133dcb194d73e20bca10105d548562de44f))
* workflow template triggers on all branches and adds workflow_dispatch ([d749632](https://github.com/hyperi-io/hyperi-ci/commit/d7496321e853b7712ae9f07052d9805b77931e47))


### Features

* add init command for project scaffolding ([80fb1f5](https://github.com/hyperi-io/hyperi-ci/commit/80fb1f5509075d59b502295a17343cc7abd20f36))
* add migrate command and per-language runner defaults ([8c370a6](https://github.com/hyperi-io/hyperi-ci/commit/8c370a66c468ab4428522afd98e1bf0465787228))
* add publish handlers for all languages ([6b06477](https://github.com/hyperi-io/hyperi-ci/commit/6b06477db73d58d81c86b6072e0a651507c7b798))
* add reusable CI workflow templates ([0262164](https://github.com/hyperi-io/hyperi-ci/commit/026216415768d904a48123506b383f1fc43f337c))
* add trigger, watch, and logs commands ([72ccd89](https://github.com/hyperi-io/hyperi-ci/commit/72ccd89240fe7a788a22e3afbbb361b3e63e1c56))
* initial hyperi-ci package ([8aeabbe](https://github.com/hyperi-io/hyperi-ci/commit/8aeabbe703b60f651f48c1b5413e1bcced212ead))
