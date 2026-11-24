# Verificação por terceiro

## Dar acesso

Em Verificação, com a empresa em foco. Só o administrador concede e revoga.

1. Feche o inventário (só inventário fechado vai para verificação).
2. Escolha o inventário, informe o e-mail do verificador e o prazo (1 a 365 dias; padrão 90).
3. O verificador recebe um link de uso único. Ele entra com a conta do provedor (Google, Microsoft) desse mesmo e-mail; o banco confere o e-mail, o prazo, a revogação e o segredo do link (só o SHA-256 dele fica guardado).

Depois do primeiro aceite, o verificador volta pelo login normal: a verificação aparece na escolha de cliente, em "Verificações".

## O que o verificador vê

Só leitura, só o inventário concedido: estado e hash do fechamento, cálculo do fechamento (totais, método, instante dos fatores, hash do resultado, memória em CSV), fontes materiais com os dados aprovados por período, fontes fora da materialidade com a justificativa, e as evidências (download com o SHA-256 conferido).

Ele não vê outros inventários, membros, convites, trilha de auditoria nem outras concessões, e nenhuma tela do cliente: tudo responde 404.

## Como o banco garante

- A sessão do verificador aplica app.concessao_id e SET LOCAL ROLE app_verificador (só SELECT).
- Toda tabela de tenant tem a política restritiva `<tabela>_verificador`, que limita ao inventário da concessão (ou a nada). O teste de cobertura exige essa política em toda tabela nova.
- A concessão só vale aceita, não revogada e dentro do prazo, para a pessoa que aceitou. Revogar ou vencer corta o acesso na próxima requisição: o acesso é relido no banco a cada pedido.

## Revogar

Botão "Revogar acesso" na concessão. O registro fica (nada some), com quem revogou e quando.
