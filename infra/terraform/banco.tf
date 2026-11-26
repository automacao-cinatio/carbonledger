# PostgreSQL 16 no RDS: rede privada (sub-redes de dados), TLS obrigatorio, cifrado com a
# chave de dados, Multi-AZ e backup de 35 dias. A senha administrativa fica num segredo que o
# proprio RDS gera e gira; ninguem a ve no Terraform.
#
# Depois do primeiro apply, a tarefa avulsa "papeis" cria app_owner, app_user, app_curadoria,
# app_verificador e app_identidade (docs/runbooks/infra-aws.md).

resource "aws_db_subnet_group" "banco" {
  name       = local.nome
  subnet_ids = aws_subnet.dados[*].id
}

resource "aws_db_parameter_group" "banco" {
  name   = "${local.nome}-pg16"
  family = "postgres16"

  # Sem TLS, nenhuma conexao entra.
  parameter {
    name  = "rds.force_ssl"
    value = "1"
  }

  parameter {
    name  = "password_encryption"
    value = "scram-sha-256"
  }

  parameter {
    name  = "log_connections"
    value = "1"
  }

  parameter {
    name  = "log_disconnections"
    value = "1"
  }

  # Consulta lenta vai para o log so pela duracao; o texto com parametros nao (dado pessoal).
  parameter {
    name  = "log_min_duration_statement"
    value = "2000"
  }

  parameter {
    name  = "log_parameter_max_length"
    value = "0"
  }
}

resource "aws_iam_role" "monitoramento_rds" {
  name = "${local.nome}-monitoramento-rds"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "monitoring.rds.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

resource "aws_iam_role_policy_attachment" "monitoramento_rds" {
  role       = aws_iam_role.monitoramento_rds.name
  policy_arn = "arn:${local.part}:iam::aws:policy/service-role/AmazonRDSEnhancedMonitoringRole"
}

# Login por senha nos papeis do app (RLS depende do papel); IAM auth nao se aplica.
#trivy:ignore:AWS-0176
resource "aws_db_instance" "banco" {
  identifier     = local.nome
  engine         = "postgres"
  engine_version = "16"

  instance_class        = var.banco_classe
  allocated_storage     = var.banco_disco_gb
  max_allocated_storage = var.banco_disco_max_gb
  storage_type          = "gp3"
  storage_encrypted     = true
  kms_key_id            = aws_kms_key.dados.arn

  db_name                       = "carbonledger"
  username                      = "carbonledger_admin"
  manage_master_user_password   = true
  master_user_secret_kms_key_id = aws_kms_key.dados.arn

  multi_az               = var.banco_multi_az
  db_subnet_group_name   = aws_db_subnet_group.banco.name
  vpc_security_group_ids = [aws_security_group.banco.id]
  publicly_accessible    = false
  parameter_group_name   = aws_db_parameter_group.banco.name
  ca_cert_identifier     = "rds-ca-rsa2048-g1"

  backup_retention_period  = var.banco_retencao_backup_dias
  backup_window            = "05:00-06:00"
  maintenance_window       = "sun:06:30-sun:07:30"
  copy_tags_to_snapshot    = true
  delete_automated_backups = false

  deletion_protection       = true
  skip_final_snapshot       = false
  final_snapshot_identifier = "${local.nome}-final"

  auto_minor_version_upgrade  = true
  allow_major_version_upgrade = false

  performance_insights_enabled          = true
  performance_insights_kms_key_id       = aws_kms_key.dados.arn
  performance_insights_retention_period = 7
  monitoring_interval                   = 60
  monitoring_role_arn                   = aws_iam_role.monitoramento_rds.arn
  enabled_cloudwatch_logs_exports       = ["postgresql", "upgrade"]

  lifecycle {
    prevent_destroy = true
  }
}
