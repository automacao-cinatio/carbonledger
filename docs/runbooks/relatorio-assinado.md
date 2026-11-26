# Relatório assinado

## Como funciona

1. O administrador envia o certificado A1 da empresa (.pfx ou .p12) e a senha na tela Relatórios. O arquivo é conferido e guardado cifrado, sem a senha.
2. Com o inventário fechado, administrador ou gestor clica em "Gerar relatório assinado". A tela confere o inventário, a cobrança e o certificado e grava um pedido (relatorio_pedido) na fila.
3. O worker (tarefa `relatorios.gerar_pedido`) gera o PDF no Chromium, assina (PAdES) com o certificado da empresa, guarda no S3 com Object Lock, registra com o SHA-256 e marca o pedido como pronto. Leva uns 15 segundos na pilha local. A tela consulta a cada 4 segundos e recarrega quando termina.
4. Se algo dá errado, o pedido sai como falhou e a tela mostra o motivo (certificado desativado, carimbo do tempo fora do ar, chave da plataforma ausente ou erro interno). Nada é gravado; basta pedir de novo.
5. Qualquer pessoa com acesso à empresa baixa o PDF. O download confere o SHA-256 antes de entregar.

Eventos na trilha: `certificado.cadastrado`, `certificado.desativado`, `relatorio.pedido`, `relatorio.gerado`.

Só um pedido por inventário fica na fila. Pedido que o worker não pegou em 15 minutos (worker parado, fila perdida, Redis fora no momento de enfileirar) já aparece na tela como falhou e o botão de gerar volta; o próximo pedido grava a falha. Se o worker pegar depois, não gera nada.

## Problemas comuns

| Sintoma | Causa provável | O que fazer |
| --- | --- | --- |
| "Assinatura indisponível" na tela | Chaves do servidor não configuradas | Configure CHAVES_LOCAIS (dev) ou, depois do P25, o KMS |
| "Confira a senha" | Senha errada ou arquivo sem a chave privada | Exportar de novo o certificado com a chave privada |
| "outro CNPJ" | O e-CNPJ não é da empresa cadastrada | Conferir o CNPJ da empresa na organização ou usar o certificado certo |
| "Use o e-CNPJ ICP-Brasil" | Em produção, certificado sem CNPJ | Usar o e-CNPJ A1 da empresa |
| Erro ao gerar, com log de Playwright | Chromium ausente na imagem | Reconstruir a imagem (o Dockerfile instala o Chromium); worker e beat exigem rebuild |
| "Relatório sendo gerado" não termina | Worker parado ou sem a imagem nova | `make logs` (serviço worker); depois de mudar código, reconstruir a imagem do worker, não só reiniciar. Passados 15 minutos, peça de novo |
| "A geração falhou por um erro interno" | Exceção inesperada no worker | O log do worker tem `relatorio.pedido_falhou` com o pedido_id (sem dado pessoal). O relatório não foi registrado, mas o PDF pode ter ficado no armazenamento sem registro |
| Download falha com hash diferente | Objeto do S3 trocado | Tratar como incidente: ver o runbook de integridade |

## Carimbo do tempo

Com TSA_URL configurada (e TSA_USUARIO e TSA_SENHA, se a autoridade exigir), cada relatório sai com carimbo do tempo (PAdES B-T): o token da autoridade fica embutido na assinatura e atesta a hora. A hora aparece na coluna "Carimbo do tempo" da tela Relatórios e do painel do verificador.

Se a autoridade não responder, o relatório não é gerado: o pedido sai como falhou e a tela pede para tentar de novo: nunca sai sem carimbo quando ele está configurado. Sem TSA_URL, a assinatura sai sem carimbo (B-B) e a coluna mostra "Sem carimbo".

Para produção, escolha uma autoridade de carimbo do tempo credenciada na ICP-Brasil (contrato com usuário e senha) ou outra aceita pelo verificador. Teste antes no ensaio de produção.

## Conferir a assinatura fora da plataforma

Abra o PDF no Adobe Acrobat Reader ou no validador do ITI (validar.iti.gov.br). Com e-CNPJ ICP-Brasil, a cadeia é reconhecida. Sem carimbo do tempo (B-B), depois do vencimento do certificado a validação depende do validador aceitar a data declarada; com carimbo (B-T), a hora é atestada pela autoridade.

## Certificado vencido ou comprometido

Desative o certificado na tela Relatórios: a chave cifrada é apagada na hora. Os relatórios já assinados continuam guardados. Se houve comprometimento, o cliente também deve revogar o certificado na autoridade certificadora.
