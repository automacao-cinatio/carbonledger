variable "regiao" {
  description = "Regiao AWS principal."
  type        = string
  default     = "us-east-1"
}

variable "ambiente" {
  description = "Nome do ambiente (vai nos nomes e nas tags)."
  type        = string
  default     = "producao"

  validation {
    condition     = can(regex("^[a-z][a-z0-9-]{1,15}$", var.ambiente))
    error_message = "Use letras minusculas, numeros e hifen (ate 16)."
  }
}

variable "dominio" {
  description = "Endereco publico do portal, por exemplo app.carbonledger.com.br."
  type        = string
}

variable "zona_route53_id" {
  description = "Id da zona hospedada do Route 53 que responde pelo dominio (certificado, DNS e SES)."
  type        = string
}

variable "prefixo_bucket" {
  description = "Prefixo unico no mundo para os buckets (por exemplo cinatio-carbonledger-prod)."
  type        = string

  validation {
    condition     = can(regex("^[a-z0-9][a-z0-9-]{2,40}$", var.prefixo_bucket))
    error_message = "Use letras minusculas, numeros e hifen (3 a 41)."
  }
}

variable "imagem_tag" {
  description = "Tag da imagem do app no ECR (imutavel; o deploy troca so esta tag)."
  type        = string
}

variable "imagem_clamav" {
  description = "Imagem do ClamAV, com versao fixa. Em producao, de preferencia um espelho no ECR."
  type        = string
  default     = "clamav/clamav:1.4"
}

variable "email_remetente" {
  description = "Remetente dos e-mails do portal (dominio verificado no SES)."
  type        = string
}

variable "email_alertas" {
  description = "E-mail que recebe os alarmes (vazio: so o topico SNS, sem assinatura)."
  type        = string
  default     = ""
}

variable "cidr_vpc" {
  description = "Faixa da VPC."
  type        = string
  default     = "10.40.0.0/16"
}

variable "nat_por_zona" {
  description = "Um NAT por zona (true) ou um so para as duas (false, mais barato e menos resiliente)."
  type        = bool
  default     = true
}

variable "endpoints_privados" {
  description = "Endpoints de interface para KMS, Secrets Manager, ECR e CloudWatch Logs (trafego fora do NAT)."
  type        = bool
  default     = true
}

variable "banco_classe" {
  description = "Classe da instancia do RDS."
  type        = string
  default     = "db.m7g.large"
}

variable "banco_disco_gb" {
  description = "Disco inicial do RDS em GB (cresce sozinho ate banco_disco_max_gb)."
  type        = number
  default     = 50
}

variable "banco_disco_max_gb" {
  description = "Limite do crescimento automatico do disco do RDS em GB."
  type        = number
  default     = 500
}

variable "banco_multi_az" {
  description = "RDS com replica em espera noutra zona."
  type        = bool
  default     = true
}

variable "banco_retencao_backup_dias" {
  description = "Dias de backup automatico do RDS (ate 35)."
  type        = number
  default     = 35
}

variable "redis_classe" {
  description = "Classe dos nos do ElastiCache (Redis)."
  type        = string
  default     = "cache.t4g.small"
}

variable "api_quantidade" {
  description = "Tarefas da API."
  type        = number
  default     = 2
}

variable "worker_quantidade" {
  description = "Tarefas do worker Celery."
  type        = number
  default     = 2
}

variable "clamav_quantidade" {
  description = "Tarefas do ClamAV (sem ele, o upload de evidencia e recusado)."
  type        = number
  default     = 2
}

variable "log_retencao_dias" {
  description = "Retencao dos logs no CloudWatch."
  type        = number
  default     = 365
}

variable "waf_so_contar" {
  description = "Regras gerenciadas do WAF so contam (nao bloqueiam). Ligado nas primeiras semanas para achar falso positivo; o limite por IP bloqueia sempre."
  type        = bool
  default     = true
}

variable "variaveis_app" {
  description = "Variaveis de ambiente do app que nao sao segredo (OIDC_*_CLIENT_ID, COBRANCA_TABELA, COBRANCA_GATEWAY, ASAAS_BASE_URL, TSA_URL, ANTHROPIC_MODELO...)."
  type        = map(string)
  default     = {}

  validation {
    condition = alltrue([
      for nome in keys(var.variaveis_app) :
      !contains(["AMBIENTE", "DATABASE_URL", "REDIS_URL", "PROVEDOR_CHAVES", "KMS_CHAVE_ID", "S3_ENDPOINT", "S3_ACCESS_KEY", "S3_SECRET_KEY", "EVIDENCIAS_MODO_RETENCAO", "COOKIE_SEGURO"], nome)
    ])
    error_message = "Essas variaveis sao fixadas pela infra e nao podem ser trocadas aqui."
  }
}

variable "segredos_externos" {
  description = "Segredos que o operador grava no Secrets Manager depois do apply (nome = variavel de ambiente do app). Cada um vira um segredo vazio injetado no app."
  type        = set(string)
  default = [
    "OIDC_GOOGLE_CLIENT_SECRET",
    "OIDC_MICROSOFT_CLIENT_SECRET",
    "SMTP_USUARIO",
    "SMTP_SENHA",
  ]

  validation {
    condition     = alltrue([for nome in var.segredos_externos : can(regex("^[A-Z][A-Z0-9_]+$", nome))])
    error_message = "Use o nome da variavel de ambiente, em maiusculas."
  }
}
