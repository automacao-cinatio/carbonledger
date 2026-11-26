# VPC em duas zonas: publica (balanceador e NAT), app (tarefas do ECS, saida pelo NAT) e dados
# (RDS e Redis, sem rota para fora). Banco nunca em sub-rede publica.

data "aws_availability_zones" "disponiveis" {
  state = "available"
}

locals {
  zonas     = slice(data.aws_availability_zones.disponiveis.names, 0, 2)
  total_nat = var.nat_por_zona ? length(local.zonas) : 1
}

resource "aws_vpc" "principal" {
  cidr_block           = var.cidr_vpc
  enable_dns_support   = true
  enable_dns_hostnames = true

  tags = { Name = local.nome }
}

# O grupo padrao da VPC fica sem regra nenhuma: todo recurso usa um grupo proprio.
resource "aws_default_security_group" "padrao" {
  vpc_id = aws_vpc.principal.id
}

resource "aws_internet_gateway" "principal" {
  vpc_id = aws_vpc.principal.id
  tags   = { Name = local.nome }
}

resource "aws_subnet" "publica" {
  count             = length(local.zonas)
  vpc_id            = aws_vpc.principal.id
  availability_zone = local.zonas[count.index]
  cidr_block        = cidrsubnet(var.cidr_vpc, 8, count.index)
  tags              = { Name = "${local.nome}-publica-${local.zonas[count.index]}" }
}

resource "aws_subnet" "app" {
  count             = length(local.zonas)
  vpc_id            = aws_vpc.principal.id
  availability_zone = local.zonas[count.index]
  cidr_block        = cidrsubnet(var.cidr_vpc, 8, 10 + count.index)
  tags              = { Name = "${local.nome}-app-${local.zonas[count.index]}" }
}

resource "aws_subnet" "dados" {
  count             = length(local.zonas)
  vpc_id            = aws_vpc.principal.id
  availability_zone = local.zonas[count.index]
  cidr_block        = cidrsubnet(var.cidr_vpc, 8, 20 + count.index)
  tags              = { Name = "${local.nome}-dados-${local.zonas[count.index]}" }
}

resource "aws_eip" "nat" {
  count  = local.total_nat
  domain = "vpc"
  tags   = { Name = "${local.nome}-nat-${count.index}" }
}

resource "aws_nat_gateway" "principal" {
  count         = local.total_nat
  allocation_id = aws_eip.nat[count.index].id
  subnet_id     = aws_subnet.publica[count.index].id
  tags          = { Name = "${local.nome}-nat-${count.index}" }

  depends_on = [aws_internet_gateway.principal]
}

resource "aws_route_table" "publica" {
  vpc_id = aws_vpc.principal.id

  route {
    cidr_block = "0.0.0.0/0"
    gateway_id = aws_internet_gateway.principal.id
  }

  tags = { Name = "${local.nome}-publica" }
}

resource "aws_route_table_association" "publica" {
  count          = length(local.zonas)
  subnet_id      = aws_subnet.publica[count.index].id
  route_table_id = aws_route_table.publica.id
}

resource "aws_route_table" "app" {
  count  = length(local.zonas)
  vpc_id = aws_vpc.principal.id

  route {
    cidr_block     = "0.0.0.0/0"
    nat_gateway_id = aws_nat_gateway.principal[min(count.index, local.total_nat - 1)].id
  }

  tags = { Name = "${local.nome}-app-${local.zonas[count.index]}" }
}

resource "aws_route_table_association" "app" {
  count          = length(local.zonas)
  subnet_id      = aws_subnet.app[count.index].id
  route_table_id = aws_route_table.app[count.index].id
}

# Dados: so a rota local da VPC.
resource "aws_route_table" "dados" {
  vpc_id = aws_vpc.principal.id
  tags   = { Name = "${local.nome}-dados" }
}

resource "aws_route_table_association" "dados" {
  count          = length(local.zonas)
  subnet_id      = aws_subnet.dados[count.index].id
  route_table_id = aws_route_table.dados.id
}

# S3 pelo endpoint de gateway (gratis): evidencias nao passam pelo NAT.
resource "aws_vpc_endpoint" "s3" {
  vpc_id            = aws_vpc.principal.id
  service_name      = "com.amazonaws.${var.regiao}.s3"
  vpc_endpoint_type = "Gateway"
  route_table_ids   = aws_route_table.app[*].id
  tags              = { Name = "${local.nome}-s3" }
}

resource "aws_vpc_endpoint" "interface" {
  for_each = var.endpoints_privados ? toset(["kms", "secretsmanager", "ecr.api", "ecr.dkr", "logs"]) : toset([])

  vpc_id              = aws_vpc.principal.id
  service_name        = "com.amazonaws.${var.regiao}.${each.key}"
  vpc_endpoint_type   = "Interface"
  subnet_ids          = aws_subnet.app[*].id
  security_group_ids  = [aws_security_group.endpoints.id]
  private_dns_enabled = true
  tags                = { Name = "${local.nome}-${each.key}" }
}

# Registro do trafego da VPC (aceito e recusado), cifrado e com retencao.
resource "aws_cloudwatch_log_group" "fluxo_vpc" {
  name              = "/carbonledger/${var.ambiente}/vpc-fluxo"
  retention_in_days = var.log_retencao_dias
  kms_key_id        = aws_kms_key.dados.arn
}

resource "aws_iam_role" "fluxo_vpc" {
  name = "${local.nome}-fluxo-vpc"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "vpc-flow-logs.amazonaws.com" }
      Action    = "sts:AssumeRole"
      Condition = { StringEquals = { "aws:SourceAccount" = local.conta } }
    }]
  })
}

resource "aws_iam_role_policy" "fluxo_vpc" {
  name = "gravar-logs"
  role = aws_iam_role.fluxo_vpc.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect   = "Allow"
      Action   = ["logs:CreateLogStream", "logs:PutLogEvents", "logs:DescribeLogStreams"]
      Resource = "${aws_cloudwatch_log_group.fluxo_vpc.arn}:*"
    }]
  })
}

resource "aws_flow_log" "vpc" {
  vpc_id          = aws_vpc.principal.id
  traffic_type    = "ALL"
  log_destination = aws_cloudwatch_log_group.fluxo_vpc.arn
  iam_role_arn    = aws_iam_role.fluxo_vpc.arn
}
