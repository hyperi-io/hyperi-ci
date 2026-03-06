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
