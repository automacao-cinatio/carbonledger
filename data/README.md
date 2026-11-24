# Dados (insumos versionados)

Insumos que os prompts leem; nenhum número é inventado, e sem eles o Claude Code para e pergunta.

| Pasta ou arquivo | Conteúdo esperado | Usado em |
| --- | --- | --- |
| data/templates/ | Ferramenta oficial do Programa Brasileiro GHG Protocol, Ciclo 2027 | P13 |
| data/paridade/<caso>/ | 3 a 5 casos de paridade: entradas e saídas calculadas na ferramenta oficial | P10, P13, P14, P17 |
| data/fatores/ | Fatores de emissão em CSV, com fonte e vigência | P07, P09 |
| data/fatores/sin/ | Fator mensal do SIN (MCTI, SIRENE) | P10 |
| data/gwp/ar5.csv | GWP AR5 de 100 anos (IPCC) | P07 |
| data/misturas.csv | Misturas de combustível com vigência (CNPE e MME) | P07, P09 |
| data/xsd/ | XSD da NF-e 4.00 e do CT-e 4.00 | P15 |
| data/templates/coleta/ | Planilhas de coleta e regra de materialidade (oficiais da CINATIO, quando houver) | P08 |

## Formato dos arquivos de referência (GWP, fatores e misturas)

Regras comuns:

- CSV em UTF-8 (pode ter BOM, como o Excel salva), separador `;`.
- Números no padrão brasileiro: `1.234,56`; números muito pequenos também em notação científica, `1,5E-04`.
- Datas em `dd/mm/aaaa` ou `aaaa-mm-dd`.
- Marcações com `sim` ou `nao`.
- Linhas começando com `#` são comentários.
- Toda linha leva a fonte (documento, edição, tabela). Sem fonte, a linha é recusada.
- Códigos em minúsculas, números, `_` e pontos (`combustao_estacionaria.oleo_diesel`).

Os modelos em data/modelos/ trazem só o cabeçalho. Nenhum número entra em data/ sem vir da fonte oficial.

### GWP (data/gwp/)

| Coluna | Conteúdo |
| --- | --- |
| conjunto | Identificador do conjunto, em maiúsculas: `AR5_100` (AR5, horizonte de 100 anos) |
| gas | Gás: `CO2`, `CH4`, `N2O`, `HFC-134a`, `SF6`... |
| valor | GWP (maior que zero) |
| fonte | Documento de origem (por exemplo, IPCC AR5, tabela e página) |

### Fatores (data/fatores/)

| Coluna | Conteúdo |
| --- | --- |
| codigo | Atividade a que o fator se aplica |
| descricao | Texto para a tela |
| categoria | Categoria do GHG Protocol (`escopo1.combustao_estacionaria`) |
| gas | Gás emitido |
| biogenico | `sim` para CO2 biogênico (relatado fora dos escopos) |
| valor | Fator (zero ou mais) |
| unidade | Massa do gás por unidade da atividade: `kg/L`, `t/MWh`, `kg/GJ`, `g/km` |
| vigencia_inicio | Primeiro dia em que o fator vale |
| vigencia_fim | Último dia (vazio: sem fim) |
| fonte | Documento de origem |
| referencia | Opcional: tabela, link ou número do documento |

No mesmo arquivo, a mesma categoria, código, gás e marcação de biogênico não podem ter vigências sobrepostas. O mesmo código pode aparecer em categorias diferentes (o diesel fóssil tem fatores de CH4 e N2O diferentes na combustão móvel e na estacionária): o cálculo procura o fator pela categoria da fonte.

Como o cálculo usa os fatores (método fator_direto, P09): a fonte de emissão aponta para um código (a "atividade"). Se esse código for um combustível com mistura em data/misturas.csv, a quantidade é dividida entre os componentes, e cada componente precisa ter fatores com o código dele, na categoria da fonte. Sem mistura, o próprio código precisa ter fatores. A unidade do fator precisa medir a mesma grandeza do dado coletado (volume com volume, massa com massa, energia com energia): o método não converte por densidade nem por poder calorífico.

### Misturas (data/misturas.csv)

| Coluna | Conteúdo |
| --- | --- |
| combustivel | Combustível vendido (`diesel_comercial`) |
| componente | Componente da mistura (`biodiesel`, `diesel_fossil`) |
| percentual | Participação de 0 a 100 |
| vigencia_inicio | Primeiro dia em que a mistura vale |
| vigencia_fim | Último dia (vazio: sem fim) |
| fonte | Norma de origem (resolução do CNPE, portaria do MME) |

A soma dos componentes de um combustível com a mesma vigência não passa de 100. A mistura usada no cálculo é a da data da compra.

## Como entra no sistema

Nada em data/ vale sozinho. A curadoria importa cada arquivo como um lote, confere e aprova; só então o cálculo usa. O passo a passo está em docs/runbooks/curadoria-de-fatores.md.

## Coleta (data/templates/coleta/)

A pasta continua vazia. Enquanto as planilhas oficiais e a regra de materialidade não chegam:

- a planilha de coleta é gerada pelo próprio sistema, por inventário (CSV com `;`, uma linha por fonte e período); o formato está em docs/runbooks/coleta-e-evidencias.md;
- a materialidade é decidida fonte a fonte: cada fonte entra ou fica de fora com justificativa registrada. Nenhum limite percentual foi definido.

Quando os arquivos oficiais chegarem, a planilha e a regra passam a segui-los.

## Paridade (data/paridade/<caso>/)

Cada caso é uma pasta com a entrada, a referência e os totais calculados na ferramenta oficial do Programa. O teste tests/parity/test_paridade.py roda cada caso pela mesma montagem e pelo mesmo método do cálculo de verdade e compara os totais com tolerância de PARIDADE_TOLERANCIA_TCO2E (padrão 0,001 tCO2e por total). A tolerância nunca se afrouxa para um caso passar.

| Arquivo | Colunas | Conteúdo |
| --- | --- | --- |
| parametros.csv | chave;valor | ano, abordagem (controle_operacional, controle_financeiro ou participacao_societaria), gwp_conjunto e ferramenta (versão da ferramenta oficial usada) |
| fontes.csv | fonte;categoria;atividade;periodicidade;participacao_societaria;controle_operacional;controle_financeiro | Uma linha por fonte; fonte é um nome curto usado em dados.csv |
| dados.csv | fonte;periodo;quantidade;unidade;data_compra | Período 01 a 12 ou ano; todos os dados contam como aprovados |
| fatores.csv, misturas.csv, gwp.csv | como acima | A referência que a ferramenta usou no caso |
| esperado.csv | indicador;valor | escopo1_tco2e, escopo2_tco2e, biogenico_tco2 e, se quiser, categoria:<codigo> e gas:<gas> |

Os valores de esperado.csv vêm da ferramenta oficial, nunca do CarbonLedger. Há um caso fictício em tests/dados_ficticios/paridade, só para provar que o comparador funciona.

## Fator do SIN (escopo 2)

O fator mensal da rede entra como fator comum em data/fatores/sin/, na categoria escopo2.energia_eletrica, com uma linha por mês (vigência do primeiro ao último dia do mês), gás CO2 e unidade t/MWh. A fonte de energia elétrica aponta para esse código. O cálculo usa o fator do mês do consumo, nunca a data da compra.
