variable "bucket_name" {
  description = "Data lake bucket name — scopes IAM policies to this bucket only"
  type        = string
}

variable "github_repo" {
  description = "GitHub repo in owner/repo format — scopes OIDC trust to this repo only"
  type        = string
}

variable "environment" {
  description = "Environment label"
  type        = string
}
