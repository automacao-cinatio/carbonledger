# Deploy pelo GitHub Actions (P26) sem chave guardada: o workflow deploy assume este papel por
# OIDC, e so a partir do ambiente "producao" do repositorio (onde o GitHub pode exigir
# aprovacao). Pode: enviar imagem ao ECR, registrar revisao de tarefa, rodar a migracao e
# atualizar api, worker e beat. Nao pode mexer em rede, banco, segredo nem chave.

variable "repositorio_github" {
  description = "Repositorio do GitHub que pode fazer deploy (dono/nome)."
  type        = string
  default     = "automacao-cinatio/carbonledger"
}

variable "ambiente_github" {
  description = "Ambiente do GitHub Actions que pode assumir o papel de deploy."
  type        = string
  default     = "producao"
}

resource "aws_iam_openid_connect_provider" "github" {
  url            = "https://token.actions.githubusercontent.com"
  client_id_list = ["sts.amazonaws.com"]
}

resource "aws_iam_role" "deploy" {
  name                 = "${local.nome}-deploy"
  max_session_duration = 3600
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Federated = aws_iam_openid_connect_provider.github.arn }
      Action    = "sts:AssumeRoleWithWebIdentity"
      Condition = {
        StringEquals = {
          "token.actions.githubusercontent.com:aud" = "sts.amazonaws.com"
          "token.actions.githubusercontent.com:sub" = "repo:${var.repositorio_github}:environment:${var.ambiente_github}"
        }
      }
    }]
  })
}

locals {
  familias_deploy = ["migrar", "papeis", "operacao", "api", "worker", "beat"]
}

resource "aws_iam_role_policy" "deploy" {
  name = "deploy"
  role = aws_iam_role.deploy.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Sid      = "LoginNoEcr"
        Effect   = "Allow"
        Action   = "ecr:GetAuthorizationToken"
        Resource = "*"
      },
      {
        Sid    = "EnviarImagem"
        Effect = "Allow"
        Action = [
          "ecr:BatchCheckLayerAvailability",
          "ecr:BatchGetImage",
          "ecr:CompleteLayerUpload",
          "ecr:DescribeImages",
          "ecr:InitiateLayerUpload",
          "ecr:PutImage",
          "ecr:UploadLayerPart",
        ]
        Resource = aws_ecr_repository.app.arn
      },
      {
        # Essas duas acoes do ECS nao aceitam recurso especifico.
        Sid      = "RevisoesDeTarefa"
        Effect   = "Allow"
        Action   = ["ecs:DescribeTaskDefinition", "ecs:RegisterTaskDefinition"]
        Resource = "*"
      },
      {
        Sid    = "RodarMigracao"
        Effect = "Allow"
        Action = "ecs:RunTask"
        Resource = [
          "arn:${local.part}:ecs:${var.regiao}:${local.conta}:task-definition/${local.nome}-migrar:*"
        ]
        Condition = { ArnEquals = { "ecs:cluster" = aws_ecs_cluster.principal.arn } }
      },
      {
        Sid      = "AcompanharTarefas"
        Effect   = "Allow"
        Action   = "ecs:DescribeTasks"
        Resource = "arn:${local.part}:ecs:${var.regiao}:${local.conta}:task/${aws_ecs_cluster.principal.name}/*"
      },
      {
        Sid    = "AtualizarServicos"
        Effect = "Allow"
        Action = ["ecs:UpdateService", "ecs:DescribeServices"]
        Resource = [
          for servico in ["api", "worker", "beat"] :
          "arn:${local.part}:ecs:${var.regiao}:${local.conta}:service/${aws_ecs_cluster.principal.name}/${servico}"
        ]
      },
      {
        Sid      = "PassarPapeisDasTarefas"
        Effect   = "Allow"
        Action   = "iam:PassRole"
        Resource = [aws_iam_role.execucao.arn, aws_iam_role.app.arn]
        Condition = {
          StringEquals = { "iam:PassedToService" = "ecs-tasks.amazonaws.com" }
        }
      },
    ]
  })
}

output "deploy_github" {
  description = "Variaveis do ambiente producao no GitHub (Settings > Environments > producao)."
  value = {
    AWS_DEPLOY_ROLE_ARN = aws_iam_role.deploy.arn
    AWS_REGION          = var.regiao
    ECS_CLUSTER         = aws_ecs_cluster.principal.name
    ECS_PREFIXO         = local.nome
    ECR_REPOSITORIO     = aws_ecr_repository.app.repository_url
    ECS_SUBNETS         = join(",", aws_subnet.app[*].id)
    ECS_GRUPOS          = aws_security_group.app.id
  }
}
