# Entrada publica: certificado do ACM, balanceador so com TLS 1.2+ (HTTP so redireciona), WAF
# e o registro no Route 53.

resource "aws_acm_certificate" "portal" {
  domain_name       = var.dominio
  validation_method = "DNS"

  lifecycle {
    create_before_destroy = true
  }
}

resource "aws_route53_record" "validacao" {
  for_each = {
    for opcao in aws_acm_certificate.portal.domain_validation_options : opcao.domain_name => opcao
  }

  zone_id         = var.zona_route53_id
  name            = each.value.resource_record_name
  type            = each.value.resource_record_type
  records         = [each.value.resource_record_value]
  ttl             = 300
  allow_overwrite = true
}

resource "aws_acm_certificate_validation" "portal" {
  certificate_arn         = aws_acm_certificate.portal.arn
  validation_record_fqdns = [for registro in aws_route53_record.validacao : registro.fqdn]
}

# Publico de proposito: e a entrada do portal (com WAF, TLS e grupo de seguranca).
#trivy:ignore:AWS-0053
resource "aws_lb" "portal" {
  name                       = local.nome
  load_balancer_type         = "application"
  internal                   = false
  subnets                    = aws_subnet.publica[*].id
  security_groups            = [aws_security_group.alb.id]
  drop_invalid_header_fields = true
  desync_mitigation_mode     = "strictest"
  enable_deletion_protection = true
  idle_timeout               = 60

  access_logs {
    bucket  = aws_s3_bucket.logs.id
    prefix  = "alb"
    enabled = true
  }

  depends_on = [aws_s3_bucket_policy.logs]
}

resource "aws_lb_target_group" "api" {
  name                 = "${local.nome}-api"
  port                 = 8000
  protocol             = "HTTP"
  target_type          = "ip"
  vpc_id               = aws_vpc.principal.id
  deregistration_delay = 30

  health_check {
    path                = "/saude"
    matcher             = "200"
    interval            = 15
    timeout             = 5
    healthy_threshold   = 2
    unhealthy_threshold = 3
  }
}

resource "aws_lb_listener" "https" {
  load_balancer_arn = aws_lb.portal.arn
  port              = 443
  protocol          = "HTTPS"
  ssl_policy        = "ELBSecurityPolicy-TLS13-1-2-2021-06"
  certificate_arn   = aws_acm_certificate_validation.portal.certificate_arn

  default_action {
    type             = "forward"
    target_group_arn = aws_lb_target_group.api.arn
  }
}

resource "aws_lb_listener" "http" {
  load_balancer_arn = aws_lb.portal.arn
  port              = 80
  protocol          = "HTTP"

  default_action {
    type = "redirect"
    redirect {
      port        = "443"
      protocol    = "HTTPS"
      status_code = "HTTP_301"
    }
  }
}

resource "aws_route53_record" "portal" {
  zone_id = var.zona_route53_id
  name    = var.dominio
  type    = "A"

  alias {
    name                   = aws_lb.portal.dns_name
    zone_id                = aws_lb.portal.zone_id
    evaluate_target_health = true
  }
}

# WAF: regras gerenciadas da AWS e limite por IP. Upload de evidencia passa de 8 KB, entao a
# regra de tamanho do corpo so conta. Com var.waf_so_contar, as regras gerenciadas so contam
# (fase de observacao); o limite por IP bloqueia sempre.

locals {
  regras_gerenciadas = {
    AWSManagedRulesAmazonIpReputationList = 10
    AWSManagedRulesCommonRuleSet          = 20
    AWSManagedRulesKnownBadInputsRuleSet  = 30
  }
}

resource "aws_wafv2_web_acl" "portal" {
  name  = local.nome
  scope = "REGIONAL"

  default_action {
    allow {}
  }

  rule {
    name     = "limite-por-ip"
    priority = 1

    action {
      block {}
    }

    statement {
      rate_based_statement {
        limit              = 2000
        aggregate_key_type = "IP"
      }
    }

    visibility_config {
      cloudwatch_metrics_enabled = true
      metric_name                = "limite-por-ip"
      sampled_requests_enabled   = true
    }
  }

  dynamic "rule" {
    for_each = local.regras_gerenciadas

    content {
      name     = rule.key
      priority = rule.value

      override_action {
        dynamic "count" {
          for_each = var.waf_so_contar ? [1] : []
          content {}
        }
        dynamic "none" {
          for_each = var.waf_so_contar ? [] : [1]
          content {}
        }
      }

      statement {
        managed_rule_group_statement {
          vendor_name = "AWS"
          name        = rule.key

          dynamic "rule_action_override" {
            for_each = rule.key == "AWSManagedRulesCommonRuleSet" ? ["SizeRestrictions_BODY"] : []
            content {
              name = rule_action_override.value
              action_to_use {
                count {}
              }
            }
          }
        }
      }

      visibility_config {
        cloudwatch_metrics_enabled = true
        metric_name                = rule.key
        sampled_requests_enabled   = true
      }
    }
  }

  visibility_config {
    cloudwatch_metrics_enabled = true
    metric_name                = local.nome
    sampled_requests_enabled   = true
  }
}

resource "aws_wafv2_web_acl_association" "portal" {
  resource_arn = aws_lb.portal.arn
  web_acl_arn  = aws_wafv2_web_acl.portal.arn
}

# Log do WAF sem cookie nem cabecalho de autorizacao (sessao e token do webhook).
resource "aws_cloudwatch_log_group" "waf" {
  name              = "aws-waf-logs-${local.nome}"
  retention_in_days = var.log_retencao_dias
  kms_key_id        = aws_kms_key.dados.arn
}

resource "aws_wafv2_web_acl_logging_configuration" "portal" {
  resource_arn            = aws_wafv2_web_acl.portal.arn
  log_destination_configs = [aws_cloudwatch_log_group.waf.arn]

  redacted_fields {
    single_header {
      name = "cookie"
    }
  }

  redacted_fields {
    single_header {
      name = "authorization"
    }
  }

  redacted_fields {
    single_header {
      name = "asaas-access-token"
    }
  }
}
