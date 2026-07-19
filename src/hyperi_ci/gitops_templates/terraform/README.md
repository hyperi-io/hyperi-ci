# OpenTofu (IaC)

Infrastructure-as-Code for the clusters and cloud resources that host HyperI
deployments. HyperI uses **OpenTofu** (`tofu`), not Terraform - Terraform is
being retired across the org. The `.tf` format is shared, and the directory is
kept named `terraform/` by convention.

## Directory layout

```
terraform/
├── aws/
│   ├── environments/   # Per-environment tfvars (staging.tfvars, production.tfvars)
│   └── modules/        # Reusable modules (EKS cluster, VPC, IAM roles, …)
├── rancher/
│   ├── clusters/       # Rancher cluster registrations
│   └── modules/        # Reusable Rancher modules
└── README.md           # This file
```

## Usage

```bash
# Initialise (first time or after module changes)
tofu -chdir=terraform/aws init

# Preview changes
tofu -chdir=terraform/aws plan -var-file=environments/staging.tfvars

# Apply
tofu -chdir=terraform/aws apply -var-file=environments/staging.tfvars
```

## CI integration

The **Validate** workflow runs `tofu fmt -check` on pull requests. It does NOT
run `plan` or `apply` — those require manual execution with appropriate cloud
credentials. Manifest / IaC security is scanned by `hyperi-ci lint-manifests`
(Checkov covers the `.tf`).

Attach the `tofu plan` output to the pull request body (use the **IaC** scope
checkbox in the PR template).

## State

OpenTofu state is stored remotely. Never commit `*.tfstate`, `*.tfstate.backup`,
or `*.tfplan` files — they are `.gitignore`'d.

## Adding a new environment

1. Copy an existing `environments/*.tfvars` and adjust values.
2. Update `argocd/applicationsets/` to include the new cluster target.
3. Open a PR — CI will validate formatting.
4. After merge, run `tofu apply` with the new var file.
