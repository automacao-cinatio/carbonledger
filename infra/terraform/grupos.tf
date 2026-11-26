# Grupos de seguranca: cada porta aberta so para quem precisa dela.

resource "aws_security_group" "alb" {
  name        = "${local.nome}-alb"
  description = "Balanceador publico: 80 (redireciona) e 443"
  vpc_id      = aws_vpc.principal.id
}

resource "aws_security_group" "app" {
  name        = "${local.nome}-app"
  description = "API, worker, beat e tarefas avulsas"
  vpc_id      = aws_vpc.principal.id
}

resource "aws_security_group" "clamav" {
  name        = "${local.nome}-clamav"
  description = "ClamAV: so o app fala com ele"
  vpc_id      = aws_vpc.principal.id
}

resource "aws_security_group" "banco" {
  name        = "${local.nome}-banco"
  description = "RDS PostgreSQL: so o app"
  vpc_id      = aws_vpc.principal.id
}

resource "aws_security_group" "redis" {
  name        = "${local.nome}-redis"
  description = "ElastiCache Redis: so o app"
  vpc_id      = aws_vpc.principal.id
}

resource "aws_security_group" "endpoints" {
  name        = "${local.nome}-endpoints"
  description = "Endpoints de interface da VPC"
  vpc_id      = aws_vpc.principal.id
}

# Balanceador
#trivy:ignore:AWS-0107
resource "aws_vpc_security_group_ingress_rule" "alb_https" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTPS da internet"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

#trivy:ignore:AWS-0107
resource "aws_vpc_security_group_ingress_rule" "alb_http" {
  security_group_id = aws_security_group.alb.id
  description       = "HTTP so para redirecionar ao HTTPS"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 80
  to_port           = 80
}

resource "aws_vpc_security_group_egress_rule" "alb_app" {
  security_group_id            = aws_security_group.alb.id
  description                  = "Para a API"
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

# App
resource "aws_vpc_security_group_ingress_rule" "app_alb" {
  security_group_id            = aws_security_group.app.id
  description                  = "Do balanceador"
  referenced_security_group_id = aws_security_group.alb.id
  ip_protocol                  = "tcp"
  from_port                    = 8000
  to_port                      = 8000
}

resource "aws_vpc_security_group_egress_rule" "app_banco" {
  security_group_id            = aws_security_group.app.id
  description                  = "PostgreSQL"
  referenced_security_group_id = aws_security_group.banco.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_egress_rule" "app_redis" {
  security_group_id            = aws_security_group.app.id
  description                  = "Redis"
  referenced_security_group_id = aws_security_group.redis.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

resource "aws_vpc_security_group_egress_rule" "app_clamav" {
  security_group_id            = aws_security_group.app.id
  description                  = "ClamAV"
  referenced_security_group_id = aws_security_group.clamav.id
  ip_protocol                  = "tcp"
  from_port                    = 3310
  to_port                      = 3310
}

# Saida para a internet pelo NAT: HTTPS (S3, KMS, OIDC, Asaas, Anthropic, TSA), HTTP (TSA e
# listas de revogacao da ICP-Brasil) e SMTP com STARTTLS (SES na 587).
# Destinos externos variam (OIDC, Asaas, Anthropic, TSA, SES): saida so nas tres portas.
#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "app_saida" {
  for_each = { https = 443, http = 80, smtp = 587 }

  security_group_id = aws_security_group.app.id
  description       = "Saida ${each.key}"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = each.value
  to_port           = each.value
}

# ClamAV
resource "aws_vpc_security_group_ingress_rule" "clamav_app" {
  security_group_id            = aws_security_group.clamav.id
  description                  = "Do app"
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 3310
  to_port                      = 3310
}

# freshclam baixa as assinaturas de servidores externos (CDN): so HTTPS.
#trivy:ignore:AWS-0104
resource "aws_vpc_security_group_egress_rule" "clamav_atualizacao" {
  security_group_id = aws_security_group.clamav.id
  description       = "freshclam e imagem (HTTPS)"
  cidr_ipv4         = "0.0.0.0/0"
  ip_protocol       = "tcp"
  from_port         = 443
  to_port           = 443
}

# Banco e Redis: so entrada do app; nenhuma saida.
resource "aws_vpc_security_group_ingress_rule" "banco_app" {
  security_group_id            = aws_security_group.banco.id
  description                  = "Do app"
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 5432
  to_port                      = 5432
}

resource "aws_vpc_security_group_ingress_rule" "redis_app" {
  security_group_id            = aws_security_group.redis.id
  description                  = "Do app"
  referenced_security_group_id = aws_security_group.app.id
  ip_protocol                  = "tcp"
  from_port                    = 6379
  to_port                      = 6379
}

# Endpoints de interface
resource "aws_vpc_security_group_ingress_rule" "endpoints" {
  for_each = {
    app    = aws_security_group.app.id
    clamav = aws_security_group.clamav.id
  }

  security_group_id            = aws_security_group.endpoints.id
  description                  = "HTTPS do ${each.key}"
  referenced_security_group_id = each.value
  ip_protocol                  = "tcp"
  from_port                    = 443
  to_port                      = 443
}
