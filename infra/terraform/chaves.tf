# Duas chaves do KMS, ambas com rotacao anual:
# - plataforma: a chave mestra do app (KMS_CHAVE_ID), que cifra as chaves de dados dos envelopes
#   (certificados A1, backups). So o papel das tarefas do app usa.
# - dados: cifra a infra em repouso (RDS, Redis, S3, segredos, logs, ECR, SNS).

resource "aws_kms_key" "plataforma" {
  description             = "${local.nome}: chave mestra do app (envelopes)"
  enable_key_rotation     = true
  deletion_window_in_days = 30
}

resource "aws_kms_alias" "plataforma" {
  name          = "alias/${local.nome}"
  target_key_id = aws_kms_key.plataforma.key_id
}

resource "aws_kms_key" "dados" {
  description             = "${local.nome}: dados da infra em repouso"
  enable_key_rotation     = true
  deletion_window_in_days = 30

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid       = "ContaAdministraPorIAM"
        Effect    = "Allow"
        Principal = { AWS = "arn:${local.part}:iam::${local.conta}:root" }
        Action    = "kms:*"
        Resource  = "*"
      },
      {
        Sid       = "CloudWatchLogsDaConta"
        Effect    = "Allow"
        Principal = { Service = "logs.${var.regiao}.amazonaws.com" }
        Action    = ["kms:Encrypt", "kms:Decrypt", "kms:ReEncrypt*", "kms:GenerateDataKey*", "kms:Describe*"]
        Resource  = "*"
        Condition = {
          ArnLike = {
            "kms:EncryptionContext:aws:logs:arn" = "arn:${local.part}:logs:${var.regiao}:${local.conta}:log-group:*"
          }
        }
      },
      {
        Sid       = "AlarmesPublicamNoTopico"
        Effect    = "Allow"
        Principal = { Service = "cloudwatch.amazonaws.com" }
        Action    = ["kms:Decrypt", "kms:GenerateDataKey*"]
        Resource  = "*"
        Condition = { StringEquals = { "aws:SourceAccount" = local.conta } }
      },
    ]
  })
}

resource "aws_kms_alias" "dados" {
  name          = "alias/${local.nome}-dados"
  target_key_id = aws_kms_key.dados.key_id
}
