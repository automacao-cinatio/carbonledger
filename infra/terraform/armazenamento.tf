# Buckets: evidencias (Object Lock; o app grava cada objeto em COMPLIANCE) e logs (acesso do
# balanceador e do bucket de evidencias). Nenhum publico; so TLS.

resource "aws_s3_bucket" "evidencias" {
  bucket              = "${var.prefixo_bucket}-evidencias"
  object_lock_enabled = true

  # Evidencia em COMPLIANCE nao se apaga nem pela conta raiz ate vencer a retencao: o bucket
  # tambem nao sai por engano de um destroy.
  lifecycle {
    prevent_destroy = true
  }
}

resource "aws_s3_bucket_versioning" "evidencias" {
  bucket = aws_s3_bucket.evidencias.id
  versioning_configuration {
    status = "Enabled"
  }
}

resource "aws_s3_bucket_server_side_encryption_configuration" "evidencias" {
  bucket = aws_s3_bucket.evidencias.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm     = "aws:kms"
      kms_master_key_id = aws_kms_key.dados.arn
    }
    bucket_key_enabled = true
  }
}

resource "aws_s3_bucket_public_access_block" "evidencias" {
  bucket                  = aws_s3_bucket.evidencias.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "evidencias" {
  bucket = aws_s3_bucket.evidencias.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_logging" "evidencias" {
  bucket        = aws_s3_bucket.evidencias.id
  target_bucket = aws_s3_bucket.logs.id
  target_prefix = "s3-evidencias/"
}

resource "aws_s3_bucket_policy" "evidencias" {
  bucket = aws_s3_bucket.evidencias.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "SoTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.evidencias.arn, "${aws_s3_bucket.evidencias.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "SoComChaveDaPlataforma"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.evidencias.arn}/*"
        Condition = {
          StringNotEqualsIfExists = {
            "s3:x-amz-server-side-encryption-aws-kms-key-id" = aws_kms_key.dados.arn
          }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.evidencias]
}

# Logs de acesso. O balanceador so grava com SSE-S3 (nao aceita KMS proprio). O proprio
# bucket de logs nao registra acesso nele mesmo (seria circular).
#trivy:ignore:AWS-0089
resource "aws_s3_bucket" "logs" {
  bucket = "${var.prefixo_bucket}-logs"
}

resource "aws_s3_bucket_versioning" "logs" {
  bucket = aws_s3_bucket.logs.id
  versioning_configuration {
    status = "Enabled"
  }
}

# O balanceador so grava log em bucket com SSE-S3; nada de dado pessoal nesses logs.
#trivy:ignore:AWS-0132
resource "aws_s3_bucket_server_side_encryption_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    apply_server_side_encryption_by_default {
      sse_algorithm = "AES256"
    }
  }
}

resource "aws_s3_bucket_public_access_block" "logs" {
  bucket                  = aws_s3_bucket.logs.id
  block_public_acls       = true
  block_public_policy     = true
  ignore_public_acls      = true
  restrict_public_buckets = true
}

resource "aws_s3_bucket_ownership_controls" "logs" {
  bucket = aws_s3_bucket.logs.id
  rule {
    object_ownership = "BucketOwnerEnforced"
  }
}

resource "aws_s3_bucket_lifecycle_configuration" "logs" {
  bucket = aws_s3_bucket.logs.id

  rule {
    id     = "expirar"
    status = "Enabled"
    filter {}
    expiration {
      days = var.log_retencao_dias
    }
    noncurrent_version_expiration {
      noncurrent_days = 30
    }
    abort_incomplete_multipart_upload {
      days_after_initiation = 7
    }
  }
}

data "aws_elb_service_account" "regiao" {}

resource "aws_s3_bucket_policy" "logs" {
  bucket = aws_s3_bucket.logs.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "SoTLS"
        Effect    = "Deny"
        Principal = "*"
        Action    = "s3:*"
        Resource  = [aws_s3_bucket.logs.arn, "${aws_s3_bucket.logs.arn}/*"]
        Condition = { Bool = { "aws:SecureTransport" = "false" } }
      },
      {
        Sid       = "BalanceadorGravaLogs"
        Effect    = "Allow"
        Principal = { AWS = data.aws_elb_service_account.regiao.arn }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.logs.arn}/alb/AWSLogs/${local.conta}/*"
      },
      {
        Sid       = "S3GravaLogsDeAcesso"
        Effect    = "Allow"
        Principal = { Service = "logging.s3.amazonaws.com" }
        Action    = "s3:PutObject"
        Resource  = "${aws_s3_bucket.logs.arn}/s3-evidencias/*"
        Condition = {
          ArnLike      = { "aws:SourceArn" = aws_s3_bucket.evidencias.arn }
          StringEquals = { "aws:SourceAccount" = local.conta }
        }
      },
    ]
  })

  depends_on = [aws_s3_bucket_public_access_block.logs]
}
