# Papeis do ECS, com o minimo de permissao:
# - execucao: o agente do ECS puxa a imagem, grava log e le os segredos da tarefa;
# - app: o codigo do app grava e le evidencias (sem apagar) e usa a chave mestra.
# ClamAV, migracao e papeis nao tem papel de tarefa: nao falam com a AWS.

data "aws_iam_policy_document" "assumir_ecs" {
  statement {
    actions = ["sts:AssumeRole"]
    principals {
      type        = "Service"
      identifiers = ["ecs-tasks.amazonaws.com"]
    }
    condition {
      test     = "StringEquals"
      variable = "aws:SourceAccount"
      values   = [local.conta]
    }
  }
}

resource "aws_iam_role" "execucao" {
  name               = "${local.nome}-ecs-execucao"
  assume_role_policy = data.aws_iam_policy_document.assumir_ecs.json
}

resource "aws_iam_role_policy_attachment" "execucao" {
  role       = aws_iam_role.execucao.name
  policy_arn = "arn:${local.part}:iam::aws:policy/service-role/AmazonECSTaskExecutionRolePolicy"
}

resource "aws_iam_role_policy" "execucao_segredos" {
  name = "ler-segredos"
  role = aws_iam_role.execucao.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid    = "SegredosDaPlataforma"
        Effect = "Allow"
        Action = "secretsmanager:GetSecretValue"
        Resource = concat(
          [for s in aws_secretsmanager_secret.gerado : s.arn],
          [for s in aws_secretsmanager_secret.externo : s.arn],
          [local.arn_admin_rds],
        )
      },
      {
        Sid      = "DecifrarSegredos"
        Effect   = "Allow"
        Action   = "kms:Decrypt"
        Resource = aws_kms_key.dados.arn
      },
    ]
  })
}

resource "aws_iam_role" "app" {
  name               = "${local.nome}-ecs-app"
  assume_role_policy = data.aws_iam_policy_document.assumir_ecs.json
}

resource "aws_iam_role_policy" "app" {
  name = "evidencias-e-chaves"
  role = aws_iam_role.app.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        # Sem DeleteObject nem BypassGovernanceRetention: o app so acrescenta.
        Sid    = "Evidencias"
        Effect = "Allow"
        Action = [
          "s3:PutObject",
          "s3:PutObjectRetention",
          "s3:GetObject",
          "s3:GetObjectVersion",
        ]
        Resource = "${aws_s3_bucket.evidencias.arn}/*"
      },
      {
        # HeadBucket da checagem de saude.
        Sid      = "BucketDeEvidencias"
        Effect   = "Allow"
        Action   = "s3:ListBucket"
        Resource = aws_s3_bucket.evidencias.arn
      },
      {
        Sid      = "CifraDoBucket"
        Effect   = "Allow"
        Action   = ["kms:Decrypt", "kms:GenerateDataKey"]
        Resource = aws_kms_key.dados.arn
        Condition = {
          StringEquals = { "kms:ViaService" = "s3.${var.regiao}.amazonaws.com" }
        }
      },
      {
        # O app sempre manda o contexto "carbonledger" (app/core/keys.py); sem ele, recusado.
        Sid      = "ChaveMestraDoApp"
        Effect   = "Allow"
        Action   = ["kms:Encrypt", "kms:Decrypt"]
        Resource = aws_kms_key.plataforma.arn
        Condition = {
          "ForAllValues:StringEquals" = { "kms:EncryptionContextKeys" = ["carbonledger"] }
          Null                        = { "kms:EncryptionContextKeys" = "false" }
        }
      },
    ]
  })
}
