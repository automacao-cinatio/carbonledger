# Infra de producao do CarbonLedger na AWS (P25). Nada aqui e aplicado sem a conta e sem o
# runbook docs/runbooks/infra-aws.md. Estado remoto no S3 (backend.hcl, fora do git).

terraform {
  required_version = ">= 1.10"

  required_providers {
    aws = {
      source  = "hashicorp/aws"
      version = "~> 5.80"
    }
    random = {
      source  = "hashicorp/random"
      version = "~> 3.6"
    }
  }

  # Configuracao parcial: terraform init -backend-config=backend.hcl (ver backend.hcl.exemplo).
  backend "s3" {}
}

provider "aws" {
  region = var.regiao

  default_tags {
    tags = {
      Projeto    = "CarbonLedger"
      Ambiente   = var.ambiente
      Gerenciado = "terraform"
    }
  }
}

data "aws_caller_identity" "atual" {}
data "aws_partition" "atual" {}

locals {
  nome    = "carbonledger-${var.ambiente}"
  conta   = data.aws_caller_identity.atual.account_id
  part    = data.aws_partition.atual.partition
  url_app = "https://${var.dominio}"
}
