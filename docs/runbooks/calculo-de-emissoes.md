# Cálculo dos escopos 1 e 2

Como o cálculo funciona, como conferir, o que fazer com cada pendência e como rodar a paridade.

## Método fator_direto 1.2.0

Para cada fonte ativa, material e dos escopos 1, 2 e 3, e para cada período com dado aprovado:

1. Data de referência: no escopo 2, o último dia do mês do consumo (o fator do SIN é mensal). Nos escopos 1 e 3, a data da compra; sem ela, o último dia do período (31/12 para fonte anual).
2. Mistura: se a atividade da fonte for um combustível com mistura aprovada na data, a quantidade é dividida entre os componentes pelo percentual. Sem mistura, o próprio combustível entra com 100%.
3. A quantidade de cada componente é convertida pelo pint para a unidade do fator (m**3 para L, MWh para GJ...).
4. Massa do gás = quantidade convertida × fator, em toneladas.
5. A massa entra com o percentual de consolidação da unidade, conforme a abordagem do inventário.
6. tCO2e = massa × GWP do gás no conjunto do inventário (AR5, 100 anos). CO2 biogênico fica fora do escopo e é relatado à parte, só a massa.

Tudo em Decimal, com 50 dígitos de precisão, sem arredondar no caminho. A tela arredonda só na exibição (3 casas); a memória de cálculo em CSV sai sem arredondar.

Fatores, misturas e GWP são lidos "como conhecidos" no instante do cálculo (relógio do banco): um fator aprovado depois não muda um cálculo já feito.

## Reprodutibilidade

Cada execução grava a entrada canônica (jsonb), o hash da entrada e o hash do resultado. Nada é alterado: calcular de novo cria execução nova.

- Conferir o cálculo (botão na tela de Resultados) refaz a conta a partir da entrada guardada e compara os dois hashes. "Conferido" quer dizer que a entrada não mudou e o resultado é o mesmo.
- Mudar qualquer regra do método exige versão nova (app/modules/calculo/fator_direto.py, VERSAO). A conferência refaz execuções das versões em VERSOES_COMPATIVEIS (1.0.0, só escopo 1; 1.1.0, escopos 1 e 2; 1.2.0, escopos 1, 2 e 3) e recusa as outras.

## Paridade

`uv run pytest tests/parity -v` roda cada caso de data/paridade/<caso>/ (formato em data/README.md) e mostra, para cada total fora da tolerância, o valor da ferramenta, o do CarbonLedger e a diferença. Caso que diverge: confira primeiro se os fatores do caso são os que a ferramenta usou; depois, a regra do método. Nunca aumente PARIDADE_TOLERANCIA_TCO2E para um caso passar.

## Pendências

O que não pode ser calculado vira pendência, nunca zero.

| Motivo | O que fazer |
| --- | --- |
| falta_dado | Lançar e aprovar o dado do período na coleta (sem dado, aguardando aprovação ou devolvido). |
| sem_atividade | Editar a fonte e escolher o combustível ou a atividade. |
| sem_fator | A curadoria importa e aprova o fator da atividade (ou dos componentes da mistura) para a categoria e a data. |
| unidade_incompativel | Lançar o dado na grandeza do fator (L com kg/L, kg com kg/kg). |
| sem_gwp | A curadoria importa e aprova o GWP do gás no conjunto do inventário. |

## Worker

A tarefa calculo.calcular_inventario (tenant_id e inventario_id) faz o mesmo cálculo no worker, para recálculos em lote. A tela calcula na própria requisição, com a mesma função.
