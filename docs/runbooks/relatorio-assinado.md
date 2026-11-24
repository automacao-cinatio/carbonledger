# Relatório assinado

## Como funciona

1. O administrador envia o certificado A1 da empresa (.pfx ou .p12) e a senha na tela Relatórios. O arquivo é conferido e guardado cifrado, sem a senha.
2. Com o inventário fechado, administrador ou gestor clica em "Gerar relatório assinado". O PDF sai do Chromium, é assinado (PAdES) com o certificado da empresa, guardado no S3 com Object Lock e registrado com o SHA-256.
3. Qualquer pessoa com acesso à empresa baixa o PDF. O download confere o SHA-256 antes de entregar.

Eventos na trilha: `certificado.cadastrado`, `certificado.desativado`, `relatorio.gerado`.

## Problemas comuns

| Sintoma | Causa provável | O que fazer |
| --- | --- | --- |
| "Assinatura indisponível" na tela | Chaves do servidor não configuradas | Configure CHAVES_LOCAIS (dev) ou, depois do P25, o KMS |
| "Confira a senha" | Senha errada ou arquivo sem a chave privada | Exportar de novo o certificado com a chave privada |
| "outro CNPJ" | O e-CNPJ não é da empresa cadastrada | Conferir o CNPJ da empresa na organização ou usar o certificado certo |
| "Use o e-CNPJ ICP-Brasil" | Em produção, certificado sem CNPJ | Usar o e-CNPJ A1 da empresa |
| Erro ao gerar, com log de Playwright | Chromium ausente na imagem | Reconstruir a imagem (o Dockerfile instala o Chromium); worker e beat exigem rebuild |
| Download falha com hash diferente | Objeto do S3 trocado | Tratar como incidente: ver o runbook de integridade |

## Conferir a assinatura fora da plataforma

Abra o PDF no Adobe Acrobat Reader ou no validador do ITI (validar.iti.gov.br). Com e-CNPJ ICP-Brasil, a cadeia é reconhecida. A assinatura é PAdES B-B: ainda sem carimbo do tempo; depois do vencimento do certificado, a validação depende do validador aceitar a data declarada.

## Certificado vencido ou comprometido

Desative o certificado na tela Relatórios: a chave cifrada é apagada na hora. Os relatórios já assinados continuam guardados. Se houve comprometimento, o cliente também deve revogar o certificado na autoridade certificadora.
