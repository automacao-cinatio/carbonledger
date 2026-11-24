# LGPD: pedidos dos titulares

## Papéis

- A CINATIO é controladora dos dados de login de quem usa a plataforma (nome, e-mail, vínculo com o provedor de login) e operadora dos dados que cada cliente guarda no inventário.
- Dados dentro do inventário (lançamentos, evidências, comentários) são do cliente. Pedido do titular sobre eles vai para o cliente, que decide como atender.
- A Anthropic é suboperadora só para os clientes que ligaram a IA (tela Usuários e acessos). Desligada por padrão.

## O que a pessoa faz sozinha

Na tela Meus dados (menu do usuário), qualquer pessoa com login:

- vê o nome, o e-mail, os clientes em que tem acesso e os pedidos que fez;
- baixa tudo em JSON (acesso e portabilidade);
- pede a exclusão, digitando o próprio e-mail. Só um pedido fica aberto por vez.

## Executar um pedido de exclusão

Prazo: 15 dias a partir do pedido.

1. Liste os pedidos em aberto (só ids e datas, nenhum nome):

   ```
   python -m app.cli lgpd pendentes
   ```

2. Confira com quem pediu, pelo e-mail que está na conta, antes de executar. A anonimização não tem volta.
3. Execute, com o seu nome:

   ```
   python -m app.cli lgpd anonimizar <id da pessoa> --por "Seu nome"
   ```

4. Responda à pessoa por e-mail confirmando a execução. Depois disso o e-mail original já não está na plataforma: guarde-o só no canal de atendimento, pelo prazo da política de atendimento.

## O que a anonimização faz

| Item | Efeito |
| --- | --- |
| Nome e e-mail | Viram "Pessoa anonimizada" e um código com o id |
| Vínculos de login | Removidos: o mesmo login volta como pessoa nova, sem acesso |
| Acessos aos clientes | Desativados; empresas atribuídas removidas |
| Convites e concessões de verificação | E-mail trocado pelo código; os pendentes são revogados |
| Trilha de auditoria e dados aprovados | Ficam, com o id, que já não identifica ninguém (nada some) |
| Pedido | Marcado como executado, com data e quem executou |

Cada cliente em que a pessoa era membro recebe na trilha o evento `lgpd.titular_anonimizado`.

## O que não é tocado

- Nomes e e-mails dentro de evidências ou textos lançados pelo cliente: o pedido sobre eles é com o cliente.
- Arquivos no S3 com Object Lock: o prazo de retenção (EVIDENCIAS_RETENCAO_DIAS, hoje 1825 dias, provisório) prevalece por obrigação de guarda do inventário.

## Problemas

- "Nada a fazer: a pessoa já foi anonimizada": o pedido já foi executado; confira em `lgpd pendentes` que ele saiu da lista.
- Erro de pessoa inexistente: o id foi copiado errado.
