# State is stored locally for Phase 1.
#
# Before account hopping (Phase 6), migrate to Terraform Cloud:
#
#   terraform {
#     cloud {
#       organization = "your-org-name"
#       workspaces {
#         name = "meta-analyst-prod"
#       }
#     }
#   }
#
# Run: terraform login
#      terraform init -migrate-state
# This moves the state file to Terraform Cloud so it survives account deletion.

terraform {
  # local backend is the default — no config needed here
}
