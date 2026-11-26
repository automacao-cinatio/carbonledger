# E-mail pelo SES: dominio do remetente verificado por DKIM no Route 53. A conta comeca no
# modo sandbox do SES; pedir a saida dele e criar a credencial SMTP estao no runbook.

locals {
  dominio_email = split("@", trimsuffix(regex("[^<\\s]+@[^>\\s]+", var.email_remetente), ">"))[1]
}

resource "aws_sesv2_email_identity" "dominio" {
  email_identity = local.dominio_email
}

resource "aws_route53_record" "dkim" {
  count = 3

  zone_id = var.zona_route53_id
  name    = "${aws_sesv2_email_identity.dominio.dkim_signing_attributes[0].tokens[count.index]}._domainkey.${local.dominio_email}"
  type    = "CNAME"
  ttl     = 1800
  records = ["${aws_sesv2_email_identity.dominio.dkim_signing_attributes[0].tokens[count.index]}.dkim.amazonses.com"]
}
