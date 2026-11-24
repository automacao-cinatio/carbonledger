# Fechamento e reapresentação

## Fechar

Em Fechamento, com a empresa em foco. Só administrador e gestor fecham. As condições aparecem na tela e são conferidas de novo no envio, com o inventário travado:

| Condição | Se faltar |
| --- | --- |
| Cálculo feito | Calcule em Resultados. |
| Cálculo com os dados de agora | Algum dado, aprovação ou fonte mudou depois do cálculo (o hash da entrada montada agora, no mesmo instante de conhecimento, não bate). Calcule de novo. |
| Sem pendências | Resolva cada pendência na coleta, ou tire a fonte da materialidade com justificativa, e calcule de novo. |
| Cálculo conferido | Refazendo a conta, o resultado não confere: incidente de integridade, avise o suporte. |

Período sem evidência gera aviso, não bloqueio.

Para fechar, digite o nome da empresa exatamente como cadastrado. O servidor confere o texto.

O fechamento grava um evento com a versão (1 no primeiro fechamento), o cálculo usado e o hash do fechamento, que amarra o hash do resultado do cálculo, os lançamentos usados e as evidências (id e SHA-256). Com o inventário fechado, o banco recusa (erro CL020) qualquer mudança nele, nas fontes, nos lançamentos, nas decisões, nas evidências, nas planilhas e nos cálculos.

## Reapresentar

Também só administrador e gestor. Informe o motivo (20 a 1.000 caracteres): o que mudou e por quê. O inventário volta para "em coleta"; o fechamento anterior continua guardado, com o cálculo e o hash dele. O próximo fechamento vira a versão seguinte.

## Garantias no banco

- O estado do inventário só muda com o evento correspondente (fechamento ou reabertura) gravado na mesma transação (erro CL021). Um UPDATE direto não fecha nem reabre nada.
- inventario_evento é só de inserção, como lançamentos, decisões, evidências e cálculos.
