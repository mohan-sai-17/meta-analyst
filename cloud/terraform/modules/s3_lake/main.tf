# ============================================================================
# S3 Data Lake — Bronze (Raw) Layer
# ============================================================================

resource "aws_s3_bucket" "data_lake" {
  bucket = var.bucket_name
}

# Block all public access — data lake is private
resource "aws_s3_bucket_public_access_block" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

# Versioning — enables point-in-time recovery and cross-account migration
resource "aws_s3_bucket_versioning" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  versioning_configuration {
    status = "Enabled"
  }
}

# Server-side encryption at rest
resource "aws_s3_bucket_server_side_encryption_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
    bucket_key_enabled = true
  }
}

# Lifecycle tiering — keeps costs near zero as data grows
resource "aws_s3_bucket_lifecycle_configuration" "data_lake" {
  bucket = aws_s3_bucket.data_lake.id

  rule {
    id     = "raw_data_tiering"
    status = "Enabled"

    filter {
      prefix = "raw/"
    }

    # After 30 days move to infrequent-access (60% cheaper reads)
    transition {
      days          = 30
      storage_class = "STANDARD_IA"
    }

    # After 90 days move to Glacier (historical archive, ~90% cheaper)
    transition {
      days          = 90
      storage_class = "GLACIER"
    }
  }

  rule {
    id     = "delete_old_versions"
    status = "Enabled"

    filter {}

    # Clean up old object versions after 30 days to avoid storage creep
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
  }
}
