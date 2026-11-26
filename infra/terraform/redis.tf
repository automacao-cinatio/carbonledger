# Redis no ElastiCache: fila do Celery, resultados e limite de login. TLS em transito, senha
# (AUTH) e cifra em repouso; dois nos com troca automatica. Sem despejo de chave
# (noeviction): mensagem da fila nunca some por falta de memoria.

resource "aws_elasticache_subnet_group" "redis" {
  name       = local.nome
  subnet_ids = aws_subnet.dados[*].id
}

resource "aws_elasticache_parameter_group" "redis" {
  name   = "${local.nome}-redis7"
  family = "redis7"

  parameter {
    name  = "maxmemory-policy"
    value = "noeviction"
  }
}

resource "random_password" "redis" {
  length  = 64
  special = false
}

resource "aws_elasticache_replication_group" "redis" {
  replication_group_id = local.nome
  description          = "CarbonLedger: fila do Celery e limites"
  engine               = "redis"
  engine_version       = "7.1"
  node_type            = var.redis_classe
  port                 = 6379
  parameter_group_name = aws_elasticache_parameter_group.redis.name

  num_cache_clusters         = 2
  automatic_failover_enabled = true
  multi_az_enabled           = true

  subnet_group_name  = aws_elasticache_subnet_group.redis.name
  security_group_ids = [aws_security_group.redis.id]

  at_rest_encryption_enabled = true
  kms_key_id                 = aws_kms_key.dados.arn
  transit_encryption_enabled = true
  auth_token                 = random_password.redis.result

  snapshot_retention_limit   = 1
  snapshot_window            = "04:00-05:00"
  maintenance_window         = "sun:07:30-sun:08:30"
  auto_minor_version_upgrade = true
  apply_immediately          = false
}
