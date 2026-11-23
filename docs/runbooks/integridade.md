# Integridade

## O que é conferido

Toda noite, às 3h (horário de Brasília), o beat dispara `integridade.disparar`, que enfileira `integridade.verificar_tenant` para cada cliente. Administrador e gestor também podem pedir a verificação na tela Integridade (menu do usuário).

Para cada cliente, sem confiar em quem gravou:

| Item | Como confere | Falha quer dizer |
| --- | --- | --- |
| Trilha de auditoria | Refaz a sequência, o encadeamento e o hash v1 de cada evento | Evento alterado, removido ou inserido fora da cadeia |
| Cálculos | Refaz cada cálculo pela entrada guardada e compara os dois hashes | Entrada alterada, ou o código do método mudou o resultado sem versão nova |
| Fechamentos | Recalcula o hash do fechamento atual de cada inventário fechado | Dado, lançamento ou evidência mudou depois do fechamento |
| Evidências e planilhas | Relê cada objeto do S3 e confere o SHA-256 | Objeto sumiu, ficou inacessível ou foi trocado |

O resultado fica em `integridade_verificacao` (só inserção) e na trilha (`integridade.verificada`, só com as contagens). Falha gera log de erro `integridade_falhou`, com o tenant e os tipos, sem dado pessoal.

## Quando houver falha

Trate como incidente de segurança. Não corrija nada no banco nem no S3 antes de entender o que aconteceu: a correção apagaria a prova.

1. Anote o horário da verificação e a lista de falhas da tela.
2. Compare com a verificação anterior íntegra: a mudança aconteceu entre as duas.
3. Trilha: o runbook trilha-de-auditoria.md mostra como achar o evento quebrado e comparar com o backup.
4. Cálculo: veja se houve deploy que mudou `app/modules/calculo/fator_direto.py` sem subir VERSAO. Se sim, é defeito de código: volte o código, não os dados.
5. Fechamento: veja na trilha quem mexeu no inventário depois do fechamento. As triggers impedem isso pela aplicação; uma falha aqui indica acesso direto ao banco.
6. Arquivo: confira no console do S3 as versões do objeto e a retenção (Object Lock). Em COMPLIANCE, nem o dono apaga; troca indica outro bucket, outra chave ou falha de configuração.
7. Registre o incidente, avise o cliente afetado e, se o inventário estiver com verificador, avise o verificador.

## Custo

A verificação relê todos os arquivos do S3. Com muitos arquivos, ela demora e gera tráfego de leitura; se ficar pesada, o próximo passo é conferir por amostragem nos dias úteis e por completo no fim de semana.
