# Trilha de auditoria

## O que é

Cada cliente (tenant) tem uma cadeia própria de eventos em audit_log. O banco calcula a sequência e o hash de cada evento; a aplicação só informa o que aconteceu. Nenhum papel da aplicação pode alterar ou apagar eventos, e uma trigger recusa UPDATE, DELETE e TRUNCATE até do dono da tabela.

A trilha não guarda dado pessoal (e-mail, nome, documento), porque não pode ser apagada. Ela guarda identificadores; o pedido de exclusão da LGPD trata a tabela usuario.

## Eventos registrados hoje

tenant.criado, convite.criado, convite.revogado, convite.aceito, membro.alterado, membro.desativado, membro.reativado, sessao.entrou, sessao.saiu.

## Verificar

- Pelo portal ou pela API (administrador ou gestor): `GET /api/auditoria/verificacao`. A resposta diz se a cadeia está íntegra e, se não estiver, o primeiro evento com problema.
- Para ler os eventos: `GET /api/auditoria?apos=<último seq lido>&limite=100`.

## Como a verificação funciona (formato v1)

Para cada evento, em ordem de seq:

1. seq precisa ser o anterior mais 1, começando em 1.
2. hash_anterior precisa ser o hash do evento anterior (32 bytes zero no primeiro).
3. hash = SHA-256(hash_anterior + conteúdo), com o conteúdo em UTF-8:

   ```
   v1|tenant_id|seq|evento_id|ocorrido_em|usuario_id|acao|entidade|entidade_id|dados
   ```

   ocorrido_em em UTC no formato AAAA-MM-DDTHH:MM:SS.ffffffZ; campos ausentes ficam vazios; dados é o texto do jsonb como o Postgres o devolve.

O verificador em app/core/audit.py refaz esse cálculo em Python, sem usar a função do banco. Assim, quem verifica não depende de quem gravou.

## Se a verificação falhar

1. Não tente corrigir o evento: qualquer alteração aparece de novo na verificação.
2. Registre o incidente com o seq e o motivo informados.
3. Compare com o backup mais recente (P27) para saber o que mudou e quando.
