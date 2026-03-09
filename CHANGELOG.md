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
