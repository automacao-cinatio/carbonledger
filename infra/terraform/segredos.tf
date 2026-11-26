# Segredos no Secrets Manager, cifrados com a chave de dados e injetados nas tarefas pelo ECS
# (nunca em variavel de ambiente em texto, nunca no codigo).
#
# Gerados aqui: as senhas dos papeis do banco, a senha do Redis e o token do webhook de
# cobranca, e as URLs montadas com elas. Ficam tambem no estado do Terraform, que mora num
# bucket cifrado e de acesso restrito (backend.hcl.exemplo).
#
# Externos (var.segredos_externos): o Terraform so cria o segredo vazio; o operador grava o
# valor (docs/runbooks/infra-aws.md) antes de subir os servicos.

resource "random_password" "banco" {
  for_each = toset(["app_owner", "app_user", "app_curadoria"])

  length  = 40
  special = false
}

resource "random_password" "webhook_cobranca" {
  length  = 48
  special = false
}

locals {
  host_banco    = aws_db_instance.banco.address
  url_banco     = "${local.host_banco}:${aws_db_instance.banco.port}/${aws_db_instance.banco.db_name}?sslmode=require"
  host_redis    = aws_elasticache_replication_group.redis.primary_endpoint_address
  prefixo_seg   = "carbonledger/${var.ambiente}"
  arn_admin_rds = aws_db_instance.banco.master_user_secret[0].secret_arn

  segredos_gerados = {
    DATABASE_URL           = "postgresql://app_user:${random_password.banco["app_user"].result}@${local.url_banco}"
    MIGRACAO_DATABASE_URL  = "postgresql://app_owner:${random_password.banco["app_owner"].result}@${local.url_banco}"
    CURADORIA_DATABASE_URL = "postgresql://app_curadoria:${random_password.banco["app_curadoria"].result}@${local.url_banco}"
    REDIS_URL              = "rediss://:${random_password.redis.result}@${local.host_redis}:6379/0?ssl_cert_reqs=required"
    APP_OWNER_SENHA        = random_password.banco["app_owner"].result
    APP_USER_SENHA         = random_password.banco["app_user"].result
    CURADORIA_SENHA        = random_password.banco["app_curadoria"].result
    COBRANCA_WEBHOOK_TOKEN = random_password.webhook_cobranca.result
  }
}

# Os nomes ficam numa lista a parte: as chaves de um mapa com senha viram sensiveis e o
# for_each nao aceita valor sensivel.
locals {
  nomes_gerados = toset([
    "DATABASE_URL", "MIGRACAO_DATABASE_URL", "CURADORIA_DATABASE_URL", "REDIS_URL",
    "APP_OWNER_SENHA", "APP_USER_SENHA", "CURADORIA_SENHA", "COBRANCA_WEBHOOK_TOKEN",
  ])
}

resource "aws_secretsmanager_secret" "gerado" {
  for_each = local.nomes_gerados

  name                    = "${local.prefixo_seg}/${each.key}"
  kms_key_id              = aws_kms_key.dados.arn
  recovery_window_in_days = 30
}

resource "aws_secretsmanager_secret_version" "gerado" {
  for_each = local.nomes_gerados

  secret_id     = aws_secretsmanager_secret.gerado[each.key].id
  secret_string = local.segredos_gerados[each.key]
}

resource "aws_secretsmanager_secret" "externo" {
  for_each = var.segredos_externos

  name                    = "${local.prefixo_seg}/${each.key}"
  kms_key_id              = aws_kms_key.dados.arn
  recovery_window_in_days = 30
}
