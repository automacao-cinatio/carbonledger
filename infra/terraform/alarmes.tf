# Alarmes basicos num topico SNS cifrado. Assinatura por e-mail so se var.email_alertas.

resource "aws_sns_topic" "alertas" {
  name              = "${local.nome}-alertas"
  kms_master_key_id = aws_kms_key.dados.arn
}

resource "aws_sns_topic_subscription" "email" {
  count = var.email_alertas == "" ? 0 : 1

  topic_arn = aws_sns_topic.alertas.arn
  protocol  = "email"
  endpoint  = var.email_alertas
}

locals {
  alarmes = {
    api-5xx = {
      namespace   = "AWS/ApplicationELB"
      metrica     = "HTTPCode_Target_5XX_Count"
      estatistica = "Sum"
      limite      = 10
      comparacao  = "GreaterThanThreshold"
      dimensoes   = { LoadBalancer = aws_lb.portal.arn_suffix }
      descricao   = "Mais de 10 respostas 5xx da API em 5 minutos"
    }
    api-sem-alvo-saudavel = {
      namespace   = "AWS/ApplicationELB"
      metrica     = "HealthyHostCount"
      estatistica = "Minimum"
      limite      = 1
      comparacao  = "LessThanThreshold"
      dimensoes   = { LoadBalancer = aws_lb.portal.arn_suffix, TargetGroup = aws_lb_target_group.api.arn_suffix }
      descricao   = "Nenhuma tarefa da API saudavel"
    }
    banco-cpu = {
      namespace   = "AWS/RDS"
      metrica     = "CPUUtilization"
      estatistica = "Average"
      limite      = 80
      comparacao  = "GreaterThanThreshold"
      dimensoes   = { DBInstanceIdentifier = aws_db_instance.banco.identifier }
      descricao   = "CPU do banco acima de 80%"
    }
    banco-disco = {
      namespace   = "AWS/RDS"
      metrica     = "FreeStorageSpace"
      estatistica = "Minimum"
      limite      = 10 * 1024 * 1024 * 1024
      comparacao  = "LessThanThreshold"
      dimensoes   = { DBInstanceIdentifier = aws_db_instance.banco.identifier }
      descricao   = "Menos de 10 GB livres no banco"
    }
  }

  # O ElastiCache publica a memoria por no (CacheClusterId), nao pelo grupo: um alarme por no.
  # Os nos do grupo se chamam <grupo>-001, <grupo>-002 e assim por diante.
  alarmes_redis = {
    for indice in range(local.redis_nos) :
    format("redis-memoria-%03d", indice + 1) => {
      namespace   = "AWS/ElastiCache"
      metrica     = "DatabaseMemoryUsagePercentage"
      estatistica = "Maximum"
      limite      = 80
      comparacao  = "GreaterThanThreshold"
      dimensoes   = { CacheClusterId = format("%s-%03d", local.nome, indice + 1) }
      descricao   = "Redis acima de 80% da memoria (sem despejo: a fila para quando enche)"
    }
  }
}

resource "aws_cloudwatch_metric_alarm" "alarme" {
  for_each = merge(local.alarmes, local.alarmes_redis)

  alarm_name          = "${local.nome}-${each.key}"
  alarm_description   = each.value.descricao
  namespace           = each.value.namespace
  metric_name         = each.value.metrica
  statistic           = each.value.estatistica
  threshold           = each.value.limite
  comparison_operator = each.value.comparacao
  dimensions          = each.value.dimensoes
  period              = 300
  evaluation_periods  = 1
  treat_missing_data  = "notBreaching"
  alarm_actions       = [aws_sns_topic.alertas.arn]
  ok_actions          = [aws_sns_topic.alertas.arn]
}
