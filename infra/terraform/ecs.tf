# ECS Fargate: servicos api, worker, beat e clamav, e as tarefas avulsas migrar, papeis e
# operacao (linha de comando). Mesma imagem do app para tudo, com tag imutavel.

resource "aws_ecr_repository" "app" {
  name                 = local.nome
  image_tag_mutability = "IMMUTABLE"

  image_scanning_configuration {
    scan_on_push = true
  }

  encryption_configuration {
    encryption_type = "KMS"
    kms_key         = aws_kms_key.dados.arn
  }
}

resource "aws_ecr_lifecycle_policy" "app" {
  repository = aws_ecr_repository.app.name
  policy = jsonencode({
    rules = [{
      rulePriority = 1
      description  = "Guarda as 50 imagens mais novas"
      selection = {
        tagStatus   = "any"
        countType   = "imageCountMoreThan"
        countNumber = 50
      }
      action = { type = "expire" }
    }]
  })
}

resource "aws_ecs_cluster" "principal" {
  name = local.nome

  setting {
    name  = "containerInsights"
    value = "enabled"
  }
}

resource "aws_service_discovery_private_dns_namespace" "interno" {
  name = "carbonledger.interno"
  vpc  = aws_vpc.principal.id
}

resource "aws_service_discovery_service" "clamav" {
  name = "clamav"

  dns_config {
    namespace_id   = aws_service_discovery_private_dns_namespace.interno.id
    routing_policy = "MULTIVALUE"
    dns_records {
      type = "A"
      ttl  = 10
    }
  }

  health_check_custom_config {
    failure_threshold = 1
  }
}

resource "aws_cloudwatch_log_group" "servico" {
  for_each = toset(["api", "worker", "beat", "clamav", "tarefas"])

  name              = "/carbonledger/${var.ambiente}/${each.key}"
  retention_in_days = var.log_retencao_dias
  kms_key_id        = aws_kms_key.dados.arn
}

locals {
  imagem = "${aws_ecr_repository.app.repository_url}:${var.imagem_tag}"

  # Fixadas pela infra: producao com as travas do app ligadas (app/core/config.py).
  variaveis_base = {
    AMBIENTE                 = "producao"
    LOG_NIVEL                = "INFO"
    APP_URL_BASE             = local.url_app
    COOKIE_SEGURO            = "true"
    S3_BUCKET_EVIDENCIAS     = aws_s3_bucket.evidencias.id
    S3_REGIAO                = var.regiao
    EVIDENCIAS_MODO_RETENCAO = "COMPLIANCE"
    PROVEDOR_CHAVES          = "kms"
    KMS_CHAVE_ID             = aws_kms_alias.plataforma.arn
    KMS_REGIAO               = var.regiao
    CLAMAV_HOST              = "clamav.${aws_service_discovery_private_dns_namespace.interno.name}"
    CLAMAV_PORT              = "3310"
    SMTP_HOST                = "email-smtp.${var.regiao}.amazonaws.com"
    SMTP_PORT                = "587"
    SMTP_TLS                 = "true"
    EMAIL_REMETENTE          = var.email_remetente
    # So o balanceador alcanca a API (grupo de seguranca): o X-Forwarded-For dele e confiavel.
    FORWARDED_ALLOW_IPS = "*"
  }

  ambiente_app = [
    for nome, valor in merge(local.variaveis_base, var.variaveis_app) : { name = nome, value = valor }
  ]

  segredo = { for nome, s in aws_secretsmanager_secret.gerado : nome => s.arn }

  segredos_app = concat(
    [for nome in ["DATABASE_URL", "REDIS_URL", "COBRANCA_WEBHOOK_TOKEN"] : { name = nome, valueFrom = local.segredo[nome] }],
    [for nome, s in aws_secretsmanager_secret.externo : { name = nome, valueFrom = s.arn }],
  )

  # Raiz so leitura, usuario do app (10001), sem capacidades; /tmp num volume efemero.
  base_app = {
    image                  = local.imagem
    essential              = true
    user                   = "10001"
    readonlyRootFilesystem = true
    environment            = local.ambiente_app
    secrets                = local.segredos_app
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    linuxParameters = {
      capabilities       = { drop = ["ALL"] }
      initProcessEnabled = true
    }
  }

  log = {
    for nome, grupo in aws_cloudwatch_log_group.servico : nome => {
      logDriver = "awslogs"
      options = {
        "awslogs-group"         = grupo.name
        "awslogs-region"        = var.regiao
        "awslogs-stream-prefix" = nome
      }
    }
  }
}

resource "aws_ecs_task_definition" "api" {
  family                   = "${local.nome}-api"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 1024
  memory                   = 2048
  execution_role_arn       = aws_iam_role.execucao.arn
  task_role_arn            = aws_iam_role.app.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([merge(local.base_app, {
    name             = "api"
    portMappings     = [{ containerPort = 8000, protocol = "tcp" }]
    logConfiguration = local.log["api"]
    healthCheck = {
      command     = ["CMD", "python", "-c", "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/saude', timeout=4).status == 200 else 1)"]
      interval    = 15
      timeout     = 5
      retries     = 4
      startPeriod = 30
    }
  })])
}

resource "aws_ecs_task_definition" "worker" {
  family                   = "${local.nome}-worker"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # O worker gera o PDF com o Chromium: memoria com folga.
  cpu                = 1024
  memory             = 3072
  execution_role_arn = aws_iam_role.execucao.arn
  task_role_arn      = aws_iam_role.app.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([merge(local.base_app, {
    name             = "worker"
    command          = ["celery", "-A", "app.workers.celery_app", "worker", "--loglevel=INFO"]
    logConfiguration = local.log["worker"]
    healthCheck = {
      command     = ["CMD-SHELL", "celery -A app.workers.celery_app inspect ping -d celery@$(hostname)"]
      interval    = 30
      timeout     = 30
      retries     = 6
      startPeriod = 90
    }
  })])
}

resource "aws_ecs_task_definition" "beat" {
  family                   = "${local.nome}-beat"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execucao.arn
  task_role_arn            = aws_iam_role.app.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([merge(local.base_app, {
    name             = "beat"
    command          = ["celery", "-A", "app.workers.celery_app", "beat", "--loglevel=INFO", "--schedule=/tmp/celerybeat-schedule"]
    logConfiguration = local.log["beat"]
    healthCheck = {
      command     = ["CMD-SHELL", "ls /tmp/celerybeat-schedule* > /dev/null 2>&1"]
      interval    = 20
      timeout     = 5
      retries     = 6
      startPeriod = 30
    }
  })])
}

# ClamAV: imagem de terceiro, que escreve a base de assinaturas no disco (sem raiz so leitura).
resource "aws_ecs_task_definition" "clamav" {
  family                   = "${local.nome}-clamav"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  # A base de assinaturas carregada pede uns 3 GB.
  cpu                = 1024
  memory             = 4096
  execution_role_arn = aws_iam_role.execucao.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  container_definitions = jsonencode([{
    name             = "clamav"
    image            = var.imagem_clamav
    essential        = true
    portMappings     = [{ containerPort = 3310, protocol = "tcp" }]
    logConfiguration = local.log["clamav"]
    linuxParameters  = { initProcessEnabled = true }
    healthCheck = {
      command     = ["CMD", "clamdcheck.sh"]
      interval    = 30
      timeout     = 10
      retries     = 5
      startPeriod = 300
    }
  }])
}

# Tarefas avulsas (aws ecs run-task; comandos em docs/runbooks/infra-aws.md).

resource "aws_ecs_task_definition" "migrar" {
  family                   = "${local.nome}-migrar"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execucao.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([{
    name                   = "migrar"
    image                  = local.imagem
    essential              = true
    user                   = "10001"
    readonlyRootFilesystem = true
    command                = ["alembic", "upgrade", "head"]
    secrets                = [{ name = "MIGRACAO_DATABASE_URL", valueFrom = local.segredo["MIGRACAO_DATABASE_URL"] }]
    mountPoints            = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    linuxParameters        = { capabilities = { drop = ["ALL"] }, initProcessEnabled = true }
    logConfiguration       = local.log["tarefas"]
  }])
}

resource "aws_ecs_task_definition" "papeis" {
  family                   = "${local.nome}-papeis"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 256
  memory                   = 512
  execution_role_arn       = aws_iam_role.execucao.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([{
    name                   = "papeis"
    image                  = local.imagem
    essential              = true
    user                   = "10001"
    readonlyRootFilesystem = true
    command                = ["python", "-m", "app.core.papeis_banco"]
    environment = [
      { name = "BANCO_HOST", value = local.host_banco },
      { name = "BANCO_PORTA", value = tostring(aws_db_instance.banco.port) },
      { name = "BANCO_NOME", value = aws_db_instance.banco.db_name },
    ]
    secrets = [
      { name = "BANCO_ADMIN_USUARIO", valueFrom = "${local.arn_admin_rds}:username::" },
      { name = "BANCO_ADMIN_SENHA", valueFrom = "${local.arn_admin_rds}:password::" },
      { name = "APP_OWNER_SENHA", valueFrom = local.segredo["APP_OWNER_SENHA"] },
      { name = "APP_USER_SENHA", valueFrom = local.segredo["APP_USER_SENHA"] },
      { name = "CURADORIA_SENHA", valueFrom = local.segredo["CURADORIA_SENHA"] },
    ]
    mountPoints      = [{ sourceVolume = "tmp", containerPath = "/tmp", readOnly = false }]
    linuxParameters  = { capabilities = { drop = ["ALL"] }, initProcessEnabled = true }
    logConfiguration = local.log["tarefas"]
  }])
}

# Linha de comando da CINATIO (criar-tenant, fatores, lgpd, integridade): o mesmo ambiente do
# app mais a URL da curadoria, que a API nunca recebe.
resource "aws_ecs_task_definition" "operacao" {
  family                   = "${local.nome}-operacao"
  requires_compatibilities = ["FARGATE"]
  network_mode             = "awsvpc"
  cpu                      = 512
  memory                   = 1024
  execution_role_arn       = aws_iam_role.execucao.arn
  task_role_arn            = aws_iam_role.app.arn

  runtime_platform {
    operating_system_family = "LINUX"
    cpu_architecture        = "X86_64"
  }

  volume {
    name = "tmp"
  }

  container_definitions = jsonencode([merge(local.base_app, {
    name             = "operacao"
    command          = ["python", "-m", "app.cli", "--help"]
    secrets          = concat(local.segredos_app, [{ name = "CURADORIA_DATABASE_URL", valueFrom = local.segredo["CURADORIA_DATABASE_URL"] }])
    logConfiguration = local.log["tarefas"]
  })])
}

# Servicos

locals {
  rede_app = {
    subnets          = aws_subnet.app[*].id
    security_groups  = [aws_security_group.app.id]
    assign_public_ip = false
  }
}

resource "aws_ecs_service" "api" {
  name                              = "api"
  cluster                           = aws_ecs_cluster.principal.id
  task_definition                   = aws_ecs_task_definition.api.arn
  desired_count                     = var.api_quantidade
  launch_type                       = "FARGATE"
  health_check_grace_period_seconds = 60
  enable_execute_command            = false
  propagate_tags                    = "SERVICE"

  network_configuration {
    subnets          = local.rede_app.subnets
    security_groups  = local.rede_app.security_groups
    assign_public_ip = false
  }

  load_balancer {
    target_group_arn = aws_lb_target_group.api.arn
    container_name   = "api"
    container_port   = 8000
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  depends_on = [aws_lb_listener.https]

  # A revisao em uso e do workflow de deploy (imagem nova a cada commit); o Terraform so a
  # define na criacao. Mudou variavel ou limite aqui: rode o deploy para entrar em uso.
  lifecycle {
    ignore_changes = [task_definition]
  }
}

resource "aws_ecs_service" "worker" {
  name                   = "worker"
  cluster                = aws_ecs_cluster.principal.id
  task_definition        = aws_ecs_task_definition.worker.arn
  desired_count          = var.worker_quantidade
  launch_type            = "FARGATE"
  enable_execute_command = false
  propagate_tags         = "SERVICE"

  network_configuration {
    subnets          = local.rede_app.subnets
    security_groups  = local.rede_app.security_groups
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # A revisao em uso e do workflow de deploy (imagem nova a cada commit); o Terraform so a
  # define na criacao. Mudou variavel ou limite aqui: rode o deploy para entrar em uso.
  lifecycle {
    ignore_changes = [task_definition]
  }
}

# Beat: nunca dois ao mesmo tempo (tarefa agendada duplicada). O deploy derruba o antigo antes.
resource "aws_ecs_service" "beat" {
  name                               = "beat"
  cluster                            = aws_ecs_cluster.principal.id
  task_definition                    = aws_ecs_task_definition.beat.arn
  desired_count                      = 1
  launch_type                        = "FARGATE"
  enable_execute_command             = false
  propagate_tags                     = "SERVICE"
  deployment_minimum_healthy_percent = 0
  deployment_maximum_percent         = 100

  network_configuration {
    subnets          = local.rede_app.subnets
    security_groups  = local.rede_app.security_groups
    assign_public_ip = false
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }

  # A revisao em uso e do workflow de deploy (imagem nova a cada commit); o Terraform so a
  # define na criacao. Mudou variavel ou limite aqui: rode o deploy para entrar em uso.
  lifecycle {
    ignore_changes = [task_definition]
  }
}

resource "aws_ecs_service" "clamav" {
  name                   = "clamav"
  cluster                = aws_ecs_cluster.principal.id
  task_definition        = aws_ecs_task_definition.clamav.arn
  desired_count          = var.clamav_quantidade
  launch_type            = "FARGATE"
  enable_execute_command = false
  propagate_tags         = "SERVICE"

  network_configuration {
    subnets          = local.rede_app.subnets
    security_groups  = [aws_security_group.clamav.id]
    assign_public_ip = false
  }

  service_registries {
    registry_arn = aws_service_discovery_service.clamav.arn
  }

  deployment_circuit_breaker {
    enable   = true
    rollback = true
  }
}
