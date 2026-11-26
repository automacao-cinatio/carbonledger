output "url_portal" {
  description = "Endereco publico do portal."
  value       = local.url_app
}

output "repositorio_imagem" {
  description = "Repositorio do ECR para o docker push."
  value       = aws_ecr_repository.app.repository_url
}

output "cluster" {
  description = "Cluster do ECS."
  value       = aws_ecs_cluster.principal.name
}

output "bucket_evidencias" {
  description = "Bucket de evidencias (Object Lock)."
  value       = aws_s3_bucket.evidencias.id
}

output "chave_plataforma" {
  description = "Alias da chave mestra do app (KMS_CHAVE_ID)."
  value       = aws_kms_alias.plataforma.arn
}

output "segredos_externos" {
  description = "Segredos que o operador precisa gravar antes de subir os servicos."
  value       = { for nome, s in aws_secretsmanager_secret.externo : nome => s.name }
}

output "rede_tarefas_avulsas" {
  description = "Rede para aws ecs run-task (migrar, papeis, operacao)."
  value       = "awsvpcConfiguration={subnets=[${join(",", aws_subnet.app[*].id)}],securityGroups=[${aws_security_group.app.id}],assignPublicIp=DISABLED}"
}

output "tarefas_avulsas" {
  description = "Familias das tarefas avulsas."
  value = {
    migrar   = aws_ecs_task_definition.migrar.family
    papeis   = aws_ecs_task_definition.papeis.family
    operacao = aws_ecs_task_definition.operacao.family
  }
}

output "dkim_ses" {
  description = "Dominio do remetente no SES (verificado por DKIM)."
  value       = aws_sesv2_email_identity.dominio.email_identity
}
