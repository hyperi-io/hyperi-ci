# Terraform

Infrastructure-as-Code for the clusters and cloud resources that host HyperI
deployments.

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
terraform -chdir=terraform/aws init

# Preview changes
terraform -chdir=terraform/aws plan -var-file=environments/staging.tfvars

# Apply
terraform -chdir=terraform/aws apply -var-file=environments/staging.tfvars
```

## CI integration

The **Validate** workflow runs `terraform fmt -check` on pull requests. It does
NOT run `plan` or `apply` — those require manual execution with appropriate cloud
credentials.

Attach the `terraform plan` output to the pull request body (use the **Terraform**
scope checkbox in the PR template).

## State

Terraform state is stored remotely. Never commit `*.tfstate`, `*.tfstate.backup`,
or `*.tfplan` files — they are `.gitignore`'d.

## Adding a new environment

1. Copy an existing `environments/*.tfvars` and adjust values.
2. Update `argocd/applicationsets/` to include the new cluster target.
3. Open a PR — CI will validate formatting.
4. After merge, run `terraform apply` with the new var file.
